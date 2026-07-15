# Jenkins Watchdog v2

Jenkins Watchdog runs durable Jenkins and Kubernetes scans, correlates every observation into an incident, investigates material changes, and plans idempotent notifications and provider actions.

## Architecture

```text
React SPA -> FastAPI /api/v2 -> PostgreSQL
                 |                 ^
                 v                 |
              Valkey SSE       scan/action/investigation worker
                                   |
                  Jenkins + Kubernetes + LiteLLM + Jira/GitHub/GitLab/SMTP
```

- PostgreSQL is the source of truth for scans, checks, findings, incidents, Jenkins job/build history, investigation requests/results, actions, delivery attempts, and event replay.
- Valkey carries bounded per-scan event streams for low-latency SSE only. It is not business-state storage.
- The API validates and enqueues work. A separate worker claims scans, investigations, and actions with leases and heartbeats.
- `domain` and `application` contain the dependency-free core and ports. `infrastructure` contains adapters, `entrypoints` contains HTTP/CLI adapters, and `bootstrap.py` is the composition root.
- External automation is disabled by default.

The Jenkins monitor indexes every discoverable job and its retained build history. Completed non-propagated failures are deterministically enriched, correlated by stable failure signature or logical execution, and linked to an incident. Every material unique incident is queued for eventual agent investigation; an active request is deduplicated instead of dropping work above a per-scan cap. Deep scans also enqueue the active incident backlog.

The agent can read Jenkins build logs, parameters, stages, tests, job history, queue and agents; Kubernetes resources, events, pod logs and metrics; Prometheus; and GitHub/GitLab change metadata and diffs. Tool calls are read-only and persisted in the investigation result. Per-mode output limits, compacted prior tool results, round limits, and token budgets bound each investigation. Only the final assessment and explicit tool trace are retained. Build evidence status and agent-analysis status are separate API/UI fields.

Finding identity is a full SHA-256 over canonical compact JSON containing `[rule_id, resource_id, identity_dimensions]`. Correlation is deterministic and never discards an observation.

## Local Development

Prerequisites are Python 3.12+, Node.js 20+, Docker, and Helm for chart validation.

```bash
./scripts/dev.sh start
```

Each worktree receives isolated API, frontend, PostgreSQL, Valkey, SMTP, and Mailpit ports. The command migrates PostgreSQL, starts the API and worker separately, and prints all local URLs.

```bash
./scripts/dev.sh status
./scripts/dev.sh logs worker -f
./scripts/dev.sh stop
```

Without Jenkins, Kubernetes, LLM, or delivery credentials, the local services still start. Detector failures are persisted as check results and cannot resolve existing incidents.

## Runtime Commands

```bash
python -m jenkins_watchdog                         # API
python -m jenkins_watchdog worker                  # scan and delivery worker
python -m jenkins_watchdog enqueue-scheduled --mode regular
python -m jenkins_watchdog enqueue-scheduled --mode deep
python -m jenkins_watchdog migrate
python -m jenkins_watchdog schema-check --wait
python -m jenkins_watchdog worker-health
```

Scheduled overlap is a successful no-op. Interactive overlap returns `409 scan_active` with links to the active scan.

## API

All business routes are under `/api/v2`.

- `POST /api/v2/scans`, scan collection/detail, cancellation, and resumable SSE events.
- Jenkins workspace, failure/build detail, and durable regular/deep `Analyze Build` requests.
- Incident collection/detail, suppression with actor and reason, durable reinvestigation, and incident chat.
- Action collection/detail and manual retry for permanently failed actions.
- Global operational chat with streamed read-only tool activity through `POST /api/v2/chat/stream`.

Collections use opaque `(created_at,id)` cursors with a default limit of 25 and maximum of 100. The checked-in contract is [frontend/openapi.json](frontend/openapi.json), and frontend API types are generated from it.

## Tests

Start test dependencies, then run the same core gates used by CI:

```bash
WATCHDOG_POSTGRES_PORT=55432 docker compose -p jwd-test -f docker-compose.dev.yml up -d --wait
export WATCHDOG_TEST_DATABASE_URL=postgresql+asyncpg://watchdog:watchdog@localhost:55432/watchdog

uv run ruff check src tests scripts
uv run mypy
uv run pytest --cov=jenkins_watchdog --cov-fail-under=80

npm --prefix frontend ci
npm --prefix frontend run build
npm --prefix frontend run test:e2e

helm dependency build helm
helm lint helm
```

CI also verifies migration from empty, Alembic model drift, PostgreSQL/Valkey/Mailpit integration, OpenAPI and generated TypeScript drift, browser workflows, Helm structure, container commands, dependency changes, and secrets.

## Deployment

The chart deploys PostgreSQL chart `18.7.12` (PostgreSQL `18.4.0`), the API, worker, post-install/post-upgrade migration Job, schema-gate init containers, and regular/deep CronJobs in `Asia/Jerusalem`. PostgreSQL persistence is enabled.

Release images use one exact tag everywhere: `2.0.0-<sha7>`. The migration hook runs before new API/worker pods can pass their schema gates. Rollback restores the prior chart/image and retains PostgreSQL for diagnosis.

Valkey remains an external dependency. See [docs/valkey-deployment.md](docs/valkey-deployment.md). All settings use the `WATCHDOG_` prefix; defaults and integration flags are defined in [config.py](src/jenkins_watchdog/config.py).
