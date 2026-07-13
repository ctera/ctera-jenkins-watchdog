---
name: dev
description: >
  Start, stop, restart, and check the status/logs of the local jenkins-watchdog
  dev stack (FastAPI backend + Vite frontend + an isolated Valkey container).
  Safe to run from multiple git worktrees of this repo at once — each worktree
  gets its own ports and its own Valkey automatically, no manual bookkeeping.
  Use this whenever asked to run/start/boot the app locally, check whether the
  dev server is running, view backend/frontend/valkey logs, restart it after a
  code change, or see what dev instances are running across worktrees.
allowed-tools: Bash(./scripts/dev.sh *), Bash(scripts/dev.sh *), Bash(cp .env.example .env), Bash(docker info)
user-invocable: true
---

# jenkins-watchdog local dev

Drive the local dev stack through `./scripts/dev.sh` from the repo root —
never hand-roll `export`s, background processes, or `docker run` directly.
The script maintains a shared port/instance registry at
`~/.local/state/jenkins-watchdog-dev/`, so bypassing it will desync worktree
instances from each other.

## Subcommands

| Command | Effect |
|---|---|
| `./scripts/dev.sh start` | Boot backend + frontend + Valkey for this worktree. Auto-creates `.venv`/runs `pip install -e .[dev]` and `npm install` if missing. Idempotent — re-running when already up just prints status. |
| `./scripts/dev.sh start --force` | Restart even if already running. |
| `./scripts/dev.sh stop` | Stop this worktree's instance (backend, frontend, Valkey container). |
| `./scripts/dev.sh stop --all` | Stop every dev instance on the machine, across all worktrees. |
| `./scripts/dev.sh stop --purge` | Stop and also forget this worktree's port allocation (next `start` may get different ports). |
| `./scripts/dev.sh restart` | `stop` + `start` for this worktree, reusing the same ports. |
| `./scripts/dev.sh status` | This worktree's instance: ports, up/down, log paths. |
| `./scripts/dev.sh status --all` | Table of every dev instance across all worktrees (flags orphans whose worktree directory no longer exists). |
| `./scripts/dev.sh logs [backend\|frontend\|valkey] [-f]` | Tail logs; no argument tails both backend and frontend; `-f` follows. |

## Before starting

- Docker must be running — `start` fails fast with a clear message if it isn't.
- `.env` is optional but recommended: `cp .env.example .env` and fill in
  `WATCHDOG_ANTHROPIC_API_KEY` / Jenkins creds for LLM investigation and real
  Jenkins checks. Without it, the app still boots and scans fine — checks that
  need Jenkins/K8s/Prometheus just fail gracefully with zero findings.
- `WATCHDOG_OIDC_ISSUER` should stay unset — that's what disables auth locally.

## Workflow

1. Run `start`.
2. Confirm with `status` before declaring success — `start` already polls
   `/health` briefly, but a slow first boot (litellm's import is slow, ~8-10s)
   can outrun that poll.
3. On any failure, check `logs backend`, `logs frontend`, or `logs valkey`
   before retrying — don't guess.
4. Multiple worktrees (e.g. one per parallel agent/branch) are safe to `start`
   concurrently — each gets isolated ports and its own Valkey, so scan locks
   and findings never leak between them. Use `status --all` to see everything
   running machine-wide.
