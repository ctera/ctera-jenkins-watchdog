"""Entrypoint for `python -m jenkins_watchdog`."""

import sys
from pathlib import Path

import uvicorn

from jenkins_watchdog.config import settings

# Must stay in sync with cli.py's `mode` choices — the two lists are separate because
# the CLI module is imported lazily, so server startup never pays for the agent stack.
_CLI_MODES = frozenset({"dry-run", "quick", "normal", "deep", "llm-health"})


def _run_server() -> None:
    reload_kwargs = {}
    if settings.reload:
        reload_kwargs = {"reload": True, "reload_dirs": [str(Path(__file__).resolve().parent)]}

    uvicorn.run(
        "jenkins_watchdog.main:app",
        host="0.0.0.0",
        port=settings.port,
        log_level=settings.log_level,
        **reload_kwargs,
    )


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] in _CLI_MODES:
        from jenkins_watchdog.cli import main as cli_main

        cli_main()
        return
    _run_server()


if __name__ == "__main__":
    main()
