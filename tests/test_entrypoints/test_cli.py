from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest
import uvicorn
from alembic.script import ScriptDirectory

from alembic import command
from jenkins_watchdog.application.scan_service import ScanAlreadyActiveError, UnknownScanCategoryError
from jenkins_watchdog.entrypoints import cli


def settings(**overrides):
    values = {
        "database_url": "postgresql+asyncpg://user:password@db/watchdog",
        "log_level": "info",
        "reload": False,
        "port": 8000,
        "jenkins_monitor_enabled": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_parser_defaults_to_api_and_accepts_command_options() -> None:
    assert cli.parser().parse_args([]).command is None
    scheduled = cli.parser().parse_args(["enqueue-scheduled", "--mode", "deep", "--category", "k8s_node"])
    assert scheduled.mode == "deep"
    assert scheduled.category == ["k8s_node"]
    schema = cli.parser().parse_args(["schema-check", "--wait", "--timeout", "3"])
    assert schema.wait and schema.timeout == 3


def test_api_and_migration_commands_delegate(monkeypatch) -> None:
    uvicorn_calls = []
    upgrades = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: uvicorn_calls.append((args, kwargs)))
    monkeypatch.setattr(command, "upgrade", lambda config, revision: upgrades.append((config, revision)))

    cli._api(settings(reload=True))
    cli._migrate("postgresql+asyncpg://user:pass%word@db/watchdog")

    assert uvicorn_calls[0][0] == ("jenkins_watchdog.main:app",)
    assert uvicorn_calls[0][1]["reload"] is True
    assert upgrades[0][1] == "head"
    assert upgrades[0][0].get_main_option("sqlalchemy.url") == "postgresql+asyncpg://user:pass%word@db/watchdog"


def test_main_dispatches_worker_with_loaded_settings(monkeypatch) -> None:
    configured = settings()
    received = []

    async def run_worker(value):
        received.append(value)
        return 0

    monkeypatch.setattr(sys, "argv", ["jenkins-watchdog", "worker"])
    monkeypatch.setattr(cli, "load_settings", lambda: configured)
    monkeypatch.setattr(cli, "_worker", run_worker)

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    assert received == [configured]


class ScanService:
    def __init__(self, outcome) -> None:
        self.outcome = outcome
        self.command = None

    async def enqueue(self, command):
        self.command = command
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class Container:
    def __init__(self, scan_service=None) -> None:
        self.scan_service = scan_service
        self.closed = 0
        self.ready_calls = 0
        self.worker = SimpleNamespace(healthy=self.healthy, run_forever=self.run_forever)
        self.delivery_worker = SimpleNamespace(run_forever=self.run_forever)
        self.jenkins_worker = SimpleNamespace(run_forever=self.run_forever)
        self.investigation_worker = SimpleNamespace(run_forever=self.run_forever)

    async def close(self):
        self.closed += 1

    async def ready(self):
        self.ready_calls += 1

    async def healthy(self):
        return True

    async def run_forever(self, stop):
        del stop

    def make_worker(self, owner=None):
        del owner
        return self.worker

    def make_delivery_worker(self):
        return self.delivery_worker

    def make_jenkins_worker(self):
        return self.jenkins_worker

    def make_investigation_worker(self):
        return self.investigation_worker


@pytest.mark.asyncio
async def test_scheduled_enqueue_success_skip_and_invalid_categories(monkeypatch, capsys) -> None:
    successful = Container(ScanService(SimpleNamespace(id="scan-1")))
    monkeypatch.setattr(cli, "build_container", lambda value: successful)
    assert await cli._enqueue_scheduled(settings(), mode="deep", categories=("k8s_node",)) == 0
    assert successful.scan_service.command.scheduled
    assert '"scan_id": "scan-1"' in capsys.readouterr().out
    assert successful.closed == 1

    active = SimpleNamespace(id="active-1")
    skipped = Container(ScanService(ScanAlreadyActiveError(active)))
    monkeypatch.setattr(cli, "build_container", lambda value: skipped)
    assert await cli._enqueue_scheduled(settings(), mode="regular", categories=()) == 0
    assert '"status": "skipped"' in capsys.readouterr().out

    invalid = Container(ScanService(UnknownScanCategoryError({"bad"})))
    monkeypatch.setattr(cli, "build_container", lambda value: invalid)
    assert await cli._enqueue_scheduled(settings(), mode="regular", categories=("bad",)) == 2


class Connection:
    def __init__(self, revision: str) -> None:
        self.revision = revision

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def scalar(self, statement):
        del statement
        return self.revision


class Engine:
    def __init__(self, revision: str) -> None:
        self.revision = revision

    def connect(self):
        return Connection(self.revision)


@pytest.mark.asyncio
async def test_schema_check_success_and_mismatch(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        ScriptDirectory,
        "from_config",
        lambda config: SimpleNamespace(get_current_head=lambda: "head-revision"),
    )
    current = Container()
    current.engine = Engine("head-revision")
    monkeypatch.setattr(cli, "build_container", lambda value: current)

    assert await cli._schema_check(settings(), wait=False, timeout_seconds=0) == 0
    assert "head-revision" in capsys.readouterr().out

    stale = Container()
    stale.engine = Engine("old-revision")
    monkeypatch.setattr(cli, "build_container", lambda value: stale)
    assert await cli._schema_check(settings(), wait=False, timeout_seconds=0) == 1
    assert stale.closed == 1


@pytest.mark.asyncio
async def test_worker_and_health_commands_check_dependencies_and_close(monkeypatch, capsys) -> None:
    container = Container()
    monkeypatch.setattr(cli, "build_container", lambda value: container)

    assert await cli._worker(settings()) == 0
    assert container.ready_calls == 1
    assert container.closed == 1

    health = Container()
    monkeypatch.setattr(cli, "build_health_check", lambda value: health)
    assert await cli._worker_health(settings()) == 0
    assert '"status": "ok"' in capsys.readouterr().out

    async def fail():
        raise RuntimeError("database unavailable")

    unhealthy = Container()
    unhealthy.ready = fail
    monkeypatch.setattr(cli, "build_health_check", lambda value: unhealthy)
    assert await cli._worker_health(settings()) == 1
    assert unhealthy.closed == 1
