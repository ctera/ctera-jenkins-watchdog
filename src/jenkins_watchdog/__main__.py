"""Entrypoint for `python -m jenkins_watchdog`."""

import uvicorn

from jenkins_watchdog.config import settings


def main() -> None:
    uvicorn.run(
        "jenkins_watchdog.main:app",
        host="0.0.0.0",
        port=settings.port,
        log_level=settings.log_level,
    )


if __name__ == "__main__":
    main()
