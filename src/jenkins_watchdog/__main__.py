"""Entrypoint for `python -m jenkins_watchdog`."""

from pathlib import Path

import uvicorn

from jenkins_watchdog.config import settings


def main() -> None:
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


if __name__ == "__main__":
    main()
