# Jenkins Watchdog

Autonomous Jenkins agent monitor for k3s clusters. Detects agent issues across health checks, investigates root causes via Claude tool-use, and streams findings to a real-time dashboard.

## Architecture

```
React SPA ──SSE──► FastAPI (uvicorn)
                     ├── 7 detection checks (parallel, async)
                     ├── Valkey: lock, findings, investigations, chat sessions, history
                     ├── LiteLLM tool-use (K8s, Prometheus, Jenkins API)
                     └── DEX OIDC auth (group-gated)
```

**Scan flow:** acquire lock → detect → diff (new/ongoing/resolved) → gate/dedupe/correlate → investigate (budget-capped, priority-sorted) → merge-store → release lock.

## Local Development

### Prerequisites

- Python 3.12+
- Node.js 20+ (frontend)
- Access to a k3s cluster (kubeconfig)
- Jenkins API access (URL + credentials)
- Anthropic API key

### Setup

```bash
# Backend
pip install -e ".[dev]"

# Set required env vars (or create .env)
export WATCHDOG_ANTHROPIC_API_KEY="sk-ant-..."
export WATCHDOG_JENKINS_URL="https://jenkins.example.com"
export WATCHDOG_JENKINS_USER="admin"
export WATCHDOG_JENKINS_TOKEN="your-api-token"
export WATCHDOG_VALKEY_SSL="false"
export WATCHDOG_VALKEY_HOST="localhost"

# Run
python -m jenkins_watchdog

# Frontend (separate terminal)
cd frontend && npm install && npm run dev
```

The app serves at `http://localhost:8000`. Without OIDC config, auth is bypassed for local dev.

### Running Tests

```bash
pytest
ruff check src/
```

## Authentication

Uses DEX (OIDC) with group-based access. Only members of configured groups can access the UI and API.

| Setting | Default | Description |
|---------|---------|-------------|
| `WATCHDOG_OIDC_ISSUER` | DEX dev endpoint | OIDC discovery URL |
| `WATCHDOG_OIDC_CLIENT_ID` | `jenkins-watchdog` | Client registered in DEX |
| `WATCHDOG_OIDC_CLIENT_SECRET` | (empty = auth disabled) | From ExternalSecret |
| `WATCHDOG_OIDC_ALLOWED_GROUPS` | `DevOps Team` | Comma-separated groups |

When `OIDC_CLIENT_SECRET` is empty, all routes are unauthenticated (local dev only).

## Scan Behavior

### Detection (7 checks)

| Check | Source | What it detects |
|-------|--------|-----------------|
| `jenkins_agent_pods` | K8s API | Agent pods: OOMKilled, CrashLoopBackOff, high restarts, stuck terminating |
| `jenkins_agent_resources` | K8s API + Prometheus | CPU/memory pressure on agent pods, resource limit violations |
| `jenkins_agent_errors` | K8s API | Container errors, log error patterns in agent pods |
| `jenkins_agent_connectivity` | Jenkins API + K8s | Agents offline/disconnected from Jenkins controller |
| `jenkins_jobs` | Jenkins API | Stuck/failing builds, queue congestion, executor starvation |
| `k8s_nodes` | K8s API | Worker node NotReady, MemoryPressure, DiskPressure |
| `k8s_workloads` | K8s API | Jenkins-related workload unavailability, stuck rollouts |

### Investigation Gate (default mode)

Only investigates:
- **New** findings with severity critical or warning
- **Ongoing** critical findings (not yet high-confidence)
- Skips findings already investigated with high confidence

### Cost Controls

| Control | Default | Configurable |
|---------|---------|-------------|
| Max investigations per scan | 10 | `WATCHDOG_MAX_INVESTIGATIONS_PER_SCAN` |
| Max tool rounds per investigation | 10 | `WATCHDOG_MAX_TOOL_ROUNDS` |
| Default UI mode | Smart (gate-filtered) | `investigate_all: false` |
| Model | Claude Sonnet 4 | `WATCHDOG_LLM_MODEL` |
| Fallback | Claude Opus 4 | `WATCHDOG_LLM_FALLBACK_MODELS` |

## Deployment

Deployed to k3s cluster via Helm chart.

Helm chart in `helm/` with:
- ExternalSecrets for API keys
- Read-only ClusterRole for K8s API access
- PDB (minAvailable: 1)

### Valkey Dependency

Jenkins Watchdog requires a Valkey (Redis-compatible) instance for distributed locking, findings storage, and chat sessions. Valkey is **not** included in the jenkins-watchdog Helm chart — it must be deployed separately.

See [docs/valkey-deployment.md](docs/valkey-deployment.md) for Helm commands, service endpoint, and verification steps.

The chart configures the connection via `config.valkeyHost` and `config.valkeySsl` in `helm/values.yaml` (currently `valkey-primary.valkey.svc.cluster.local:6379`, no TLS).

## Environment Variables

All prefixed with `WATCHDOG_`. See `src/jenkins_watchdog/config.py` for full list.
