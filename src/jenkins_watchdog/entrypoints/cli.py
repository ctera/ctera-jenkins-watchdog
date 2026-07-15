"""Process entrypoints for API, workers, schedules, and schema gates."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import time
from pathlib import Path

from jenkins_watchdog.bootstrap import build_container, build_health_check, load_settings

logger = logging.getLogger(__name__)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="jenkins-watchdog")
    subcommands = root.add_subparsers(dest="command")
    subcommands.add_parser("api", help="run the HTTP API")
    subcommands.add_parser("worker", help="run scan and delivery workers")
    sync_jenkins = subcommands.add_parser("sync-jenkins", help="synchronize the durable Jenkins build index once")
    sync_jenkins.add_argument("--window-hours", type=int, default=None)

    scheduled = subcommands.add_parser("enqueue-scheduled", help="enqueue a scheduled scan")
    scheduled.add_argument("--mode", choices=("regular", "deep"), default="regular")
    scheduled.add_argument("--category", action="append", default=[])

    schema = subcommands.add_parser("schema-check", help="verify the database is at Alembic head")
    schema.add_argument("--wait", action="store_true")
    schema.add_argument("--timeout", type=float, default=120.0)

    subcommands.add_parser("migrate", help="upgrade the database to Alembic head")
    subcommands.add_parser("worker-health", help="check worker dependencies")
    return root


def main() -> None:
    args = parser().parse_args()
    settings = load_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    command_name = args.command or "api"
    if command_name == "api":
        _api(settings)
        return
    if command_name == "migrate":
        _migrate(settings.database_url)
        return
    runners = {
        "worker": lambda: _worker(settings),
        "sync-jenkins": lambda: _sync_jenkins(settings, window_hours=args.window_hours),
        "enqueue-scheduled": lambda: _enqueue_scheduled(settings, mode=args.mode, categories=tuple(args.category)),
        "schema-check": lambda: _schema_check(settings, wait=args.wait, timeout_seconds=args.timeout),
        "worker-health": lambda: _worker_health(settings),
    }
    try:
        exit_code = asyncio.run(runners[command_name]())
    except KeyboardInterrupt:
        exit_code = 130
    raise SystemExit(exit_code)


def _api(settings) -> None:
    import uvicorn

    reload_kwargs = {}
    if settings.reload:
        reload_kwargs = {
            "reload": True,
            "reload_dirs": [str(Path(__file__).resolve().parents[1])],
        }
    uvicorn.run(
        "jenkins_watchdog.main:app",
        host="0.0.0.0",
        port=settings.port,
        log_level=settings.log_level,
        **reload_kwargs,
    )


async def _worker(settings) -> int:
    container = build_container(settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(name, stop.set)
        except NotImplementedError:
            pass
    scan_worker = container.make_worker()
    delivery_worker = container.make_delivery_worker()
    jenkins_worker = container.make_jenkins_worker()
    investigation_worker = container.make_investigation_worker()
    try:
        await container.ready()
        tasks = [
            scan_worker.run_forever(stop),
            delivery_worker.run_forever(stop),
            investigation_worker.run_forever(stop),
        ]
        if settings.jenkins_monitor_enabled:
            tasks.append(jenkins_worker.run_forever(stop))
        await asyncio.gather(*tasks)
        return 0
    finally:
        await container.close()


async def _sync_jenkins(settings, *, window_hours: int | None) -> int:
    from dataclasses import asdict

    container = build_container(settings)
    try:
        await container.ready()
        stats = await container.jenkins_monitor.sync(
            owner=f"cli-{os.getpid()}",
            window_hours=window_hours,
        )
        if stats is None:
            print(json.dumps({"status": "skipped", "reason": "sync_active"}))
            return 0
        print(json.dumps({"status": "completed", **asdict(stats)}, default=str))
        return 0
    finally:
        await container.close()


async def _enqueue_scheduled(settings, *, mode: str, categories: tuple[str, ...]) -> int:
    from jenkins_watchdog.application.scan_service import (
        EnqueueScanCommand,
        ScanAlreadyActiveError,
        UnknownScanCategoryError,
    )
    from jenkins_watchdog.domain.model import ScanMode

    container = build_container(settings)
    try:
        try:
            scan = await container.scan_service.enqueue(
                EnqueueScanCommand(
                    mode=ScanMode(mode),
                    categories=frozenset(categories),
                    scheduled=True,
                )
            )
        except ScanAlreadyActiveError as exc:
            logger.info("scheduled scan skipped; active scan is %s", exc.active_scan.id)
            print(json.dumps({"status": "skipped", "active_scan_id": exc.active_scan.id}))
            return 0
        except UnknownScanCategoryError as exc:
            logger.error("unknown scheduled categories: %s", sorted(exc.categories))
            return 2
        print(json.dumps({"status": "queued", "scan_id": scan.id}))
        return 0
    finally:
        await container.close()


def _migrate(database_url: str) -> None:
    from alembic import command

    config = _alembic_config(database_url)
    command.upgrade(config, "head")


async def _schema_check(settings, *, wait: bool, timeout_seconds: float) -> int:
    from alembic.script import ScriptDirectory
    from sqlalchemy import text

    deadline = time.monotonic() + timeout_seconds
    expected = ScriptDirectory.from_config(_alembic_config(settings.database_url)).get_current_head()
    while True:
        container = build_container(settings)
        try:
            async with container.engine.connect() as connection:
                current = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            if current == expected:
                print(json.dumps({"status": "ok", "revision": current}))
                return 0
        except Exception as exc:
            current = type(exc).__name__
        finally:
            await container.close()
        if not wait or time.monotonic() >= deadline:
            logger.error("database revision %s does not match head %s", current, expected)
            return 1
        await asyncio.sleep(min(2.0, max(0.0, deadline - time.monotonic())))


async def _worker_health(settings) -> int:
    health = build_health_check(settings)
    try:
        await health.ready()
        print(json.dumps({"status": "ok"}))
        return 0
    except Exception as exc:
        logger.error("worker health failed: %s", type(exc).__name__)
        return 1
    finally:
        await health.close()


def _alembic_config(database_url: str):
    from alembic.config import Config

    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


if __name__ == "__main__":
    main()
