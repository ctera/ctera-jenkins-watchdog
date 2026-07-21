#!/usr/bin/env bash
# Local dev orchestrator: API + worker + frontend + isolated dependencies.
#
# Safe to run from multiple `git worktree` checkouts of this repo at once —
# each worktree gets its own ports and containers, tracked in a central
# registry (see scripts/dev_state.py) so instances never collide.
#
# Usage:
#   scripts/dev.sh start [--force]
#   scripts/dev.sh stop [--all] [--purge]
#   scripts/dev.sh restart
#   scripts/dev.sh status [--all]
#   scripts/dev.sh logs [backend|worker|frontend|valkey|postgres|mailpit] [-f]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
DEV_STATE_PY="$REPO_ROOT/scripts/dev_state.py"
STATE_ROOT="${WATCHDOG_DEV_STATE_DIR:-$HOME/.local/state/jenkins-watchdog-dev}"

# --- instance resolution ----------------------------------------------------

# Populates all per-worktree ports and INSTANCE_DIR.
# Creates a registry entry for this worktree if one doesn't exist yet.
resolve_instance() {
  local json
  json=$(python3 "$DEV_STATE_PY" allocate "$REPO_ROOT")
  eval "$(_entry_to_shell_vars "$json")"
  INSTANCE_DIR="$STATE_ROOT/instances/$INSTANCE_ID"
  mkdir -p "$INSTANCE_DIR"
}

# Same, but read-only: fails (returns 1) if this worktree was never started,
# instead of silently reserving a port allocation for it.
resolve_instance_readonly() {
  local json
  json=$(python3 "$DEV_STATE_PY" lookup "$REPO_ROOT")
  [ "$json" = "null" ] && return 1
  eval "$(_entry_to_shell_vars "$json")"
  INSTANCE_DIR="$STATE_ROOT/instances/$INSTANCE_ID"
  return 0
}

_entry_to_shell_vars() {
  printf '%s' "$1" | python3 -c '
import json, sys
d = json.load(sys.stdin)
for k in (
    "instance_id", "backend_port", "frontend_port", "valkey_port",
    "postgres_port", "mailpit_smtp_port", "mailpit_ui_port",
):
    print(f"{k.upper()}={d[k]}")
'
}

is_alive() {
  local pidfile="$1"
  [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null
}

port_owner() {
  lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null | head -n 1 || true
}

stop_pid() {
  local pidfile="$1"
  [ -f "$pidfile" ] || return 0
  local pid
  pid=$(cat "$pidfile")
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 10); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.5
    done
    kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
  fi
  rm -f "$pidfile"
}

compose_project() {
  echo "jwd-$1"
}

# Tears down everything for a given instance_id, addressed purely by state-dir
# path — no need to know or visit the worktree it belongs to (handles orphans).
stop_instance() {
  local iid="$1"
  local idir="$STATE_ROOT/instances/$iid"
  stop_pid "$idir/backend.pid"
  stop_pid "$idir/worker.pid"
  stop_pid "$idir/frontend.pid"
  local project
  project=$(compose_project "$iid")
  docker ps -aq --filter "label=com.docker.compose.project=$project" 2>/dev/null \
    | xargs -r docker rm -f >/dev/null 2>&1 || true
  if [ -f "$idir/meta.json" ]; then
    python3 -c "
import json, sys
p = sys.argv[1]
d = json.load(open(p))
d['status'] = 'stopped'
json.dump(d, open(p, 'w'), indent=2)
" "$idir/meta.json" || true
  fi
}

require_docker() {
  if ! docker info >/dev/null 2>&1; then
    echo "ERROR: Docker is not running (or not installed). Start Docker and retry." >&2
    exit 1
  fi
}

ensure_backend_deps() {
  if [ -n "${VIRTUAL_ENV:-}" ]; then
    VENV_PY="$(command -v python3)"
  else
    if [ ! -x "$REPO_ROOT/.venv/bin/python" ]; then
      echo "==> Creating virtualenv at .venv"
      python3 -m venv "$REPO_ROOT/.venv"
    fi
    VENV_PY="$REPO_ROOT/.venv/bin/python"
  fi
  if ! "$VENV_PY" -c "import jenkins_watchdog" >/dev/null 2>&1; then
    echo "==> Installing backend deps (pip install -e .[dev])"
    "$VENV_PY" -m pip install -q -e "$REPO_ROOT[dev]"
  fi
}

ensure_frontend_deps() {
  if [ ! -d "$REPO_ROOT/frontend/node_modules" ]; then
    echo "==> Installing frontend deps (npm install)"
    (cd "$REPO_ROOT/frontend" && npm install --no-fund --no-audit)
  fi
}

write_meta() {
  local status="$1"
  python3 -c "
import json, sys
instance_id, worktree, backend_port, frontend_port, valkey_port, postgres_port, smtp_port, mailpit_port, status, out_path = sys.argv[1:11]
json.dump({
    'instance_id': instance_id,
    'worktree': worktree,
    'backend_port': int(backend_port),
    'frontend_port': int(frontend_port),
    'valkey_port': int(valkey_port),
    'postgres_port': int(postgres_port),
    'mailpit_smtp_port': int(smtp_port),
    'mailpit_ui_port': int(mailpit_port),
    'status': status,
}, open(out_path, 'w'), indent=2)
" "$INSTANCE_ID" "$REPO_ROOT" "$BACKEND_PORT" "$FRONTEND_PORT" "$VALKEY_PORT" "$POSTGRES_PORT" "$MAILPIT_SMTP_PORT" "$MAILPIT_UI_PORT" "$status" "$INSTANCE_DIR/meta.json"
}

# --- subcommands -------------------------------------------------------------

cmd_start() {
  local force=0
  [ "${1:-}" = "--force" ] && force=1

  resolve_instance

  if [ "$force" -ne 1 ] && is_alive "$INSTANCE_DIR/backend.pid" && is_alive "$INSTANCE_DIR/worker.pid" && is_alive "$INSTANCE_DIR/frontend.pid"; then
    echo "Already running for this worktree ($INSTANCE_ID):"
    cmd_status
    return 0
  fi

  require_docker
  ensure_backend_deps
  ensure_frontend_deps

  local project
  project=$(compose_project "$INSTANCE_ID")
  echo "==> Starting dependencies ($project)"
  WATCHDOG_VALKEY_PORT="$VALKEY_PORT" \
    WATCHDOG_POSTGRES_PORT="$POSTGRES_PORT" \
    WATCHDOG_MAILPIT_SMTP_PORT="$MAILPIT_SMTP_PORT" \
    WATCHDOG_MAILPIT_UI_PORT="$MAILPIT_UI_PORT" \
    docker compose -p "$project" -f "$REPO_ROOT/docker-compose.dev.yml" up -d --wait

  local database_url="postgresql+asyncpg://watchdog:watchdog@localhost:$POSTGRES_PORT/watchdog"
  echo "==> Migrating PostgreSQL"
  WATCHDOG_DATABASE_URL="$database_url" "$VENV_PY" -m jenkins_watchdog migrate

  echo "==> Starting backend on :$BACKEND_PORT"
  (
    cd "$REPO_ROOT"
    WATCHDOG_PORT="$BACKEND_PORT" \
      WATCHDOG_VALKEY_HOST=localhost \
      WATCHDOG_VALKEY_PORT="$VALKEY_PORT" \
      WATCHDOG_DATABASE_URL="$database_url" \
      WATCHDOG_SMTP_HOST=localhost \
      WATCHDOG_SMTP_PORT="$MAILPIT_SMTP_PORT" \
      WATCHDOG_RELOAD=true \
      nohup "$VENV_PY" -m jenkins_watchdog >"$INSTANCE_DIR/backend.log" 2>&1 &
    echo $! >"$INSTANCE_DIR/backend.pid"
  )

  echo "==> Starting worker"
  (
    cd "$REPO_ROOT"
    WATCHDOG_VALKEY_HOST=localhost \
      WATCHDOG_VALKEY_PORT="$VALKEY_PORT" \
      WATCHDOG_DATABASE_URL="$database_url" \
      WATCHDOG_SMTP_HOST=localhost \
      WATCHDOG_SMTP_PORT="$MAILPIT_SMTP_PORT" \
      nohup "$VENV_PY" -m jenkins_watchdog worker >"$INSTANCE_DIR/worker.log" 2>&1 &
    echo $! >"$INSTANCE_DIR/worker.pid"
  )

  echo "==> Starting frontend on :$FRONTEND_PORT"
  (
    cd "$REPO_ROOT/frontend"
    WATCHDOG_DEV_BACKEND_URL="http://localhost:$BACKEND_PORT" \
      nohup npm run dev -- --port "$FRONTEND_PORT" --strictPort >"$INSTANCE_DIR/frontend.log" 2>&1 &
    echo $! >"$INSTANCE_DIR/frontend.pid"
  )

  write_meta "running"

  echo "==> Waiting for backend health..."
  local healthy=0
  for _ in $(seq 1 20); do
    if curl -sf "http://localhost:$BACKEND_PORT/health" >/dev/null 2>&1; then
      healthy=1
      break
    fi
    sleep 1
  done
  [ "$healthy" -eq 1 ] || echo "WARNING: backend did not report healthy in time — check: $0 logs backend"

  cat <<EOF

jenkins-watchdog dev instance '$INSTANCE_ID' is up:
  Frontend:  http://localhost:$FRONTEND_PORT
  Backend:   http://localhost:$BACKEND_PORT
  Mailpit:   http://localhost:$MAILPIT_UI_PORT
  Logs:      $0 logs [backend|worker|frontend|valkey|postgres|mailpit]
  Status:    $0 status
EOF
}

cmd_stop() {
  local target_all=0 purge=0
  for arg in "$@"; do
    case "$arg" in
    --all) target_all=1 ;;
    --purge) purge=1 ;;
    esac
  done

  if [ "$target_all" -eq 1 ]; then
    local entries
    entries=$(python3 "$DEV_STATE_PY" list)
    printf '%s' "$entries" | python3 -c "import json,sys; [print(e['instance_id']) for e in json.load(sys.stdin)]" |
      while read -r iid; do
        echo "==> Stopping $iid"
        stop_instance "$iid"
      done
    if [ "$purge" -eq 1 ]; then
      printf '%s' "$entries" | python3 -c "import json,sys; [print(e['worktree']) for e in json.load(sys.stdin)]" |
        while read -r wt; do python3 "$DEV_STATE_PY" release "$wt"; done
    fi
    echo "Stopped all instances."
    return 0
  fi

  if ! resolve_instance_readonly; then
    echo "No dev instance recorded for this worktree." >&2
    exit 1
  fi

  echo "==> Stopping $INSTANCE_ID"
  stop_instance "$INSTANCE_ID"
  [ "$purge" -eq 1 ] && python3 "$DEV_STATE_PY" release "$REPO_ROOT"
  echo "Stopped."
}

cmd_restart() {
  if resolve_instance_readonly; then
    cmd_stop
  fi
  cmd_start
}

cmd_status() {
  if [ "${1:-}" = "--all" ]; then
    local entries
    entries=$(python3 "$DEV_STATE_PY" list)
    python3 - "$STATE_ROOT" "$entries" <<'PY'
import json, os, subprocess, sys
from pathlib import Path

state_root = Path(sys.argv[1])
entries = json.loads(sys.argv[2])


def pid_alive(pidfile):
    if not pidfile.exists():
        return False
    try:
        os.kill(int(pidfile.read_text().strip()), 0)
        return True
    except (OSError, ValueError):
        return False


def valkey_up(instance_id):
    out = subprocess.run(
        ["docker", "ps", "-q", "--filter", f"label=com.docker.compose.project=jwd-{instance_id}"],
        capture_output=True, text=True,
    )
    return bool(out.stdout.strip())


if not entries:
    print("No dev instances recorded.")
    sys.exit(0)

fmt = "{:<34} {:<9} {:<12} {:<9} {:<12} {:<7}  {}"
print(fmt.format("INSTANCE", "WORKTREE", "BACKEND", "WORKER", "FRONTEND", "DEPS", "PATH"))
for e in sorted(entries, key=lambda x: x["offset"]):
    idir = state_root / "instances" / e["instance_id"]
    backend = ("up:" if pid_alive(idir / "backend.pid") else "down:") + str(e["backend_port"])
    frontend = ("up:" if pid_alive(idir / "frontend.pid") else "down:") + str(e["frontend_port"])
    worker = "up" if pid_alive(idir / "worker.pid") else "down"
    valkey = "up" if valkey_up(e["instance_id"]) else "down"
    exists = "ok" if Path(e["worktree"]).is_dir() else "ORPHANED"
    print(fmt.format(e["instance_id"], exists, backend, worker, frontend, valkey, e["worktree"]))
PY
    return 0
  fi

  if ! resolve_instance_readonly; then
    echo "No dev instance recorded for this worktree. Run: $0 start"
    return 0
  fi

  local backend_state worker_state frontend_state valkey_state project
  is_alive "$INSTANCE_DIR/backend.pid" && backend_state="up" || backend_state="down"
  is_alive "$INSTANCE_DIR/worker.pid" && worker_state="up" || worker_state="down"
  is_alive "$INSTANCE_DIR/frontend.pid" && frontend_state="up" || frontend_state="down"
  local frontend_owner
  frontend_owner=$(port_owner "$FRONTEND_PORT")
  if [ "$frontend_state" = "down" ] && [ -n "$frontend_owner" ]; then
    frontend_state="conflict (pid $frontend_owner)"
  fi
  project=$(compose_project "$INSTANCE_ID")
  if [ -n "$(docker ps -q --filter "label=com.docker.compose.project=$project" 2>/dev/null)" ]; then
    valkey_state="up"
  else
    valkey_state="down"
  fi

  cat <<EOF
Instance:  $INSTANCE_ID
Worktree:  $REPO_ROOT
Backend:   $backend_state (:$BACKEND_PORT)   log: $INSTANCE_DIR/backend.log
Worker:    $worker_state                    log: $INSTANCE_DIR/worker.log
Frontend:  $frontend_state (:$FRONTEND_PORT)  log: $INSTANCE_DIR/frontend.log
Valkey:    $valkey_state (:$VALKEY_PORT)
Postgres:  $valkey_state (:$POSTGRES_PORT)
Mailpit:   $valkey_state (:$MAILPIT_UI_PORT, SMTP :$MAILPIT_SMTP_PORT)
EOF
}

cmd_logs() {
  if ! resolve_instance_readonly; then
    echo "No dev instance recorded for this worktree. Run: $0 start" >&2
    exit 1
  fi

  local target="" follow=0
  for arg in "$@"; do
    case "$arg" in
    -f) follow=1 ;;
    backend | worker | frontend | valkey | postgres | mailpit) target="$arg" ;;
    esac
  done

  case "$target" in
  backend)
    [ "$follow" -eq 1 ] && tail -f "$INSTANCE_DIR/backend.log" || tail -n 50 "$INSTANCE_DIR/backend.log"
    ;;
  frontend)
    [ "$follow" -eq 1 ] && tail -f "$INSTANCE_DIR/frontend.log" || tail -n 50 "$INSTANCE_DIR/frontend.log"
    ;;
  worker)
    [ "$follow" -eq 1 ] && tail -f "$INSTANCE_DIR/worker.log" || tail -n 50 "$INSTANCE_DIR/worker.log"
    ;;
  valkey | postgres | mailpit)
    local project
    project=$(compose_project "$INSTANCE_ID")
    if [ "$follow" -eq 1 ]; then
      docker compose -p "$project" -f "$REPO_ROOT/docker-compose.dev.yml" logs -f "$target"
    else
      docker compose -p "$project" -f "$REPO_ROOT/docker-compose.dev.yml" logs --tail 50 "$target"
    fi
    ;;
  *)
    echo "== backend =="
    tail -n 50 "$INSTANCE_DIR/backend.log" 2>/dev/null || true
    echo
    echo "== frontend =="
    tail -n 50 "$INSTANCE_DIR/frontend.log" 2>/dev/null || true
    echo
    echo "== worker =="
    tail -n 50 "$INSTANCE_DIR/worker.log" 2>/dev/null || true
    ;;
  esac
}

# --- entrypoint ---------------------------------------------------------

main() {
  local cmd="${1:-start}"
  [ $# -gt 0 ] && shift
  case "$cmd" in
  start) cmd_start "$@" ;;
  stop) cmd_stop "$@" ;;
  restart) cmd_restart "$@" ;;
  status) cmd_status "$@" ;;
  logs) cmd_logs "$@" ;;
  *)
    echo "Usage: $0 {start|stop [--all] [--purge]|restart|status [--all]|logs [backend|worker|frontend|valkey|postgres|mailpit] [-f]}" >&2
    exit 1
    ;;
  esac
}

main "$@"
