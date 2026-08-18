# Deploying jenkins-watchdog to production

Namespace and release are both `jenkins-watchdog`. Registry is
`cteraacrdevops.azurecr.io/jenkins-watchdog`, tagged `<pyproject version>-<7-char sha>`.

## Two things that will bite you

**1. The `deploy` job in `release.yml` cannot reach the cluster.** The k3s API is not
reachable from GitHub-hosted runners, so that job fails on every release. A red pipeline
does *not* mean the build failed — check whether `build-and-push` succeeded. **Production
ships by hand.**

**2. Never run `helm upgrade` from the working tree.** Helm renders whatever sits in
`helm/charts/`, and a `postgresql-*.tgz` has been observed there — untracked, and not
declared as a dependency in `Chart.yaml`. Rendering the working tree with that file
present injects a **PostgreSQL StatefulSet, extra Services, a NetworkPolicy and a second
PodDisruptionBudget** into the release. Measured, not hypothetical: 57 postgres objects in
the rendered manifest. `helm/charts/` is now gitignored, but a clean export is the
guarantee:

```bash
EXPORT=$(mktemp -d)
git archive main | tar -x -C "$EXPORT"
```

Everything below runs from `$EXPORT`.

## Deploy

```bash
cd "$EXPORT"
TAG=<the tag release.yml pushed>     # see the "ci: update image tag" commit on main

# 1. Diff the render against what is actually running. Any unexpected DELETION —
#    a PVC, the TLS secret, the ingress — means stop.
helm get manifest jenkins-watchdog -n jenkins-watchdog > /tmp/live.yaml
helm template jenkins-watchdog ./helm -n jenkins-watchdog --set image.tag="$TAG" > /tmp/next.yaml
diff -u /tmp/live.yaml /tmp/next.yaml | less

# 2. The token must be in the Secret BEFORE the new image rolls, or the first
#    scan after rollout investigates nothing. See claude-oauth-rotation.md.
kubectl -n jenkins-watchdog get secret jenkins-watchdog-secrets \
  -o jsonpath='{.data.WATCHDOG_CLAUDE_CODE_OAUTH_TOKEN}' | base64 -d | \
  awk '{ printf "prefix_ok=%s len=%d\n", ($0 ~ /^sk-ant-oat01-/), length($0) }'

# 3. Mirror what CI would have run.
helm upgrade --install jenkins-watchdog ./helm \
  --namespace jenkins-watchdog \
  --set image.tag="$TAG"

kubectl -n jenkins-watchdog rollout status deploy/jenkins-watchdog --timeout=300s
```

## Verify

```bash
# The credential works — the only check that detects a revoked token.
kubectl -n jenkins-watchdog exec deploy/jenkins-watchdog -- python -m jenkins_watchdog llm-health

# The system prompt actually loaded. Before WATCHDOG_PROMPTS_DIR existed this resolved
# into site-packages and silently fell back to a one-line prompt, so it is worth
# asserting rather than assuming.
kubectl -n jenkins-watchdog exec deploy/jenkins-watchdog -- python -c \
  "from jenkins_watchdog.reasoning.prompt_files import prompts_dir, read_prompt; \
   t=read_prompt('system.md'); print(prompts_dir(), len(t), '## Known normal behaviors' in t)"

# Then one real investigation through the UI or POST /scan, and watch memory.
kubectl -n jenkins-watchdog get events --field-selector reason=OOMKilling
kubectl -n jenkins-watchdog top pod
```

Memory deserves 48h of attention after this release: the deployment had already been
OOMKilled hundreds of times at the old 1Gi limit, and each investigation now also spawns
a Claude Code subprocess. The limit is 3Gi and `llmMaxConcurrentAgents` (2) bounds how
many run at once — if OOMKills continue, lower the concurrency before raising the limit.

## Rollback

| Symptom | Action |
|---|---|
| Investigations 401 / return nothing | Token problem, **not** a release problem. Fix the Secret and `rollout restart` — no Helm involved. See `claude-oauth-rotation.md`. |
| The release itself is bad | `helm rollback jenkins-watchdog -n jenkins-watchdog` |
| Repeated OOMKills | Lower `config.llmMaxConcurrentAgents` to 1 and `helm upgrade`; the subprocesses are the new memory cost |
