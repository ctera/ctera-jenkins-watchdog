#!/usr/bin/env python3
"""Port/instance registry for local multi-worktree dev instances.

Stdlib-only. scripts/dev.sh shells out to this for the one thing bash can't
do safely: atomic allocate-or-reuse of a port set, shared across every
git-worktree checkout of this repo on the machine.

Registry: ~/.local/state/jenkins-watchdog-dev/instances.json, keyed by the
worktree's absolute path. Port formula for offset N (N=0 is today's exact
defaults, so the first/primary checkout is unaffected):
    backend  = 8000 + N*10
    frontend = 3000 + N*10
    valkey   = 6379 + N*10
    postgres = 5432 + N*10
    mailpit SMTP = 1025 + N*10
    mailpit UI   = 8025 + N*10
"""

import argparse
import fcntl
import hashlib
import json
import os
import re
import socket
import sys
from pathlib import Path

STATE_ROOT = Path(os.environ.get("WATCHDOG_DEV_STATE_DIR", Path.home() / ".local/state/jenkins-watchdog-dev"))
REGISTRY_PATH = STATE_ROOT / "instances.json"
LOCK_PATH = STATE_ROOT / "instances.lock"

BACKEND_BASE, FRONTEND_BASE, VALKEY_BASE, STRIDE = 8000, 3000, 6379, 10
POSTGRES_BASE, MAILPIT_SMTP_BASE, MAILPIT_UI_BASE = 5432, 1025, 8025


def instance_id(worktree: str) -> str:
    path = str(Path(worktree).resolve())
    digest = hashlib.sha256(path.encode()).hexdigest()[:8]
    slug = re.sub(r"[^a-z0-9-]+", "-", Path(path).name.lower()).strip("-") or "repo"
    return f"{slug}-{digest}"


def _ports(offset: int) -> dict:
    return {
        "backend_port": BACKEND_BASE + offset * STRIDE,
        "frontend_port": FRONTEND_BASE + offset * STRIDE,
        "valkey_port": VALKEY_BASE + offset * STRIDE,
        "postgres_port": POSTGRES_BASE + offset * STRIDE,
        "mailpit_smtp_port": MAILPIT_SMTP_BASE + offset * STRIDE,
        "mailpit_ui_port": MAILPIT_UI_BASE + offset * STRIDE,
    }


def _port_is_free(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", port))
    except OSError:
        return False
    return True


def _load_registry() -> dict:
    if not REGISTRY_PATH.exists():
        return {"version": 1, "instances": {}}
    with open(REGISTRY_PATH, encoding="utf-8") as f:
        return json.load(f)


def _save_registry(registry: dict) -> None:
    tmp = REGISTRY_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(registry, f, indent=2, sort_keys=True)
    tmp.replace(REGISTRY_PATH)


class _RegistryLock:
    def __enter__(self):
        STATE_ROOT.mkdir(parents=True, exist_ok=True)
        self._fh = open(LOCK_PATH, "a+")
        fcntl.flock(self._fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self._fh, fcntl.LOCK_UN)
        self._fh.close()


def cmd_allocate(worktree: str) -> None:
    path = str(Path(worktree).resolve())
    with _RegistryLock():
        registry = _load_registry()
        existing = registry["instances"].get(path)
        if existing is not None:
            ports = _ports(existing["offset"])
            if any(existing.get(name) != value for name, value in ports.items()):
                existing.update(ports)
                _save_registry(registry)
            print(json.dumps(existing))
            return

        used_offsets = {entry["offset"] for entry in registry["instances"].values()}
        offset = 0
        while True:
            if offset not in used_offsets:
                ports = _ports(offset)
                if all(_port_is_free(p) for p in ports.values()):
                    break
            offset += 1

        entry = {"instance_id": instance_id(path), "offset": offset, "worktree": path, **_ports(offset)}
        registry["instances"][path] = entry
        _save_registry(registry)
        print(json.dumps(entry))


def cmd_lookup(worktree: str) -> None:
    """Look up an existing entry without creating one. Prints `null` if unallocated."""
    path = str(Path(worktree).resolve())
    registry = _load_registry()
    print(json.dumps(registry["instances"].get(path)))


def cmd_list() -> None:
    registry = _load_registry()
    print(json.dumps(list(registry["instances"].values())))


def cmd_release(worktree: str) -> None:
    path = str(Path(worktree).resolve())
    with _RegistryLock():
        registry = _load_registry()
        registry["instances"].pop(path, None)
        _save_registry(registry)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_id = sub.add_parser("instance-id")
    p_id.add_argument("worktree")

    p_alloc = sub.add_parser("allocate")
    p_alloc.add_argument("worktree")

    p_lookup = sub.add_parser("lookup")
    p_lookup.add_argument("worktree")

    sub.add_parser("list")

    p_release = sub.add_parser("release")
    p_release.add_argument("worktree")

    args = parser.parse_args()

    if args.command == "instance-id":
        print(instance_id(args.worktree))
    elif args.command == "allocate":
        cmd_allocate(args.worktree)
    elif args.command == "lookup":
        cmd_lookup(args.worktree)
    elif args.command == "list":
        cmd_list()
    elif args.command == "release":
        cmd_release(args.worktree)


if __name__ == "__main__":
    sys.exit(main())
