# Rotating the Claude Code OAuth token

The service authenticates to Claude with a **`CLAUDE_CODE_OAUTH_TOKEN`**, not an
Anthropic API key. An API key will not work: an OAuth token has no quota against the raw
Messages API, so the Claude Agent SDK is the only path it authenticates on.

## The one thing to know first

**Nothing passive detects a revoked token.** `/health`, `/ready`, the container
HEALTHCHECK, and `claude auth status` all pass on a revoked credential — `auth status`
only checks that the variable is *set*. The credential is exercised only when a
subprocess runs a turn, so the pod stays Ready while every investigation 401s.

The only detector is a real call:

```bash
kubectl -n jenkins-watchdog exec deploy/jenkins-watchdog -- \
  python -m jenkins_watchdog llm-health
```

| Exit | Meaning | Action |
|---|---|---|
| 0 | The token works | none |
| 2 | `auth_failed` or `unconfigured` | rotate or set the token (below) |
| 1 | Broken image or unreachable network | do **not** rotate; investigate the pod |

`--shallow` checks presence only and spends nothing. It cannot detect revocation — it
exists so CI can prove the bundled CLI survived into the image without needing a
credential.

## Minting a token

```bash
claude setup-token      # on a machine where you are logged in to the right account
```

The value starts `sk-ant-oat01-`. Two failure modes worth checking before you paste it
anywhere:

- **A trailing newline gives a 401 that looks exactly like revocation.** `ClaudeCredentials`
  strips whitespace, but a newline baked into a Kubernetes Secret value is easy to
  introduce and hard to see.
- **Never `export` it.** The app injects it into the agent subprocess itself. Exported
  globally, it retargets every other `claude` on that machine to this service's identity.

## Rotating in production

The token lives in the hand-created Secret `jenkins-watchdog-secrets`, which the chart
references by a hardcoded name but does not manage.

**Patch the single key. Do not `create --dry-run=client | apply`** — that rewrites the
whole Secret and would drop its five siblings (Jenkins, OIDC, Jira credentials).

```bash
TOKEN='sk-ant-oat01-...'          # leading space keeps it out of shell history
kubectl -n jenkins-watchdog patch secret jenkins-watchdog-secrets --type=merge \
  -p "{\"data\":{\"WATCHDOG_CLAUDE_CODE_OAUTH_TOKEN\":\"$(printf %s "$TOKEN" | base64 -w0)\"}}"
```

`printf %s` matters: `echo` would append the newline described above.

Then restart to pick it up — env comes from `envFrom`, which is read at container start:

```bash
kubectl -n jenkins-watchdog rollout restart deploy/jenkins-watchdog
kubectl -n jenkins-watchdog rollout status deploy/jenkins-watchdog --timeout=180s
kubectl -n jenkins-watchdog exec deploy/jenkins-watchdog -- python -m jenkins_watchdog llm-health
```

Verify the stored value's shape without printing it:

```bash
kubectl -n jenkins-watchdog get secret jenkins-watchdog-secrets \
  -o jsonpath='{.data.WATCHDOG_CLAUDE_CODE_OAUTH_TOKEN}' | base64 -d | \
  awk '{ printf "prefix_ok=%s len=%d\n", ($0 ~ /^sk-ant-oat01-/), length($0) }'
```

A `len` one larger than the token you pasted means a stray newline.

## Rotation is not a deploy

A bad token is fixed by patching the Secret and restarting. It needs no `helm upgrade`
and no new image — keep the two failure modes separate when debugging.

## Why a bad token fails loudly

`CLAUDE_CONFIG_DIR` is pinned to a private, credential-free directory (an emptyDir at
`/home/watchdog`). Without that pin the CLI would resolve a missing or revoked token
against whatever `~/.claude` login exists in the image or on the node, and answer under
the wrong identity — succeeding, which is the failure mode hardest to notice. The empty
config dir leaves the token as the only way in.

Verified: on a developer machine with a populated `~/.claude`, a deliberately invalid
token exits 2 with `401 OAuth access token is invalid` rather than answering.

## Token lifetime

Undocumented. Treat `llm-health` as the detection story rather than a calendar reminder:
run it after any auth-shaped incident, and consider it the first check when
investigations start coming back empty while scans still report findings.

## Related

- Scans degrade gracefully without a token: detection still runs and reports findings;
  only investigation and triage are skipped. A missing credential shows up as
  "no investigation", not as a failed scan.
- `docs/operations/production-deploy.md` covers shipping a new image.
