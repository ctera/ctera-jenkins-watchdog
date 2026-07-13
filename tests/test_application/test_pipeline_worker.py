from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jenkins_watchdog.application.events import EventService, NullEventNotifier
from jenkins_watchdog.application.incidents import IncidentService
from jenkins_watchdog.application.pipeline import ScanCancelled, ScanPipeline
from jenkins_watchdog.application.types import EnqueueScan
from jenkins_watchdog.application.worker import ScanWorker
from jenkins_watchdog.domain.model import (
    CheckResult,
    CheckStatus,
    FindingObservation,
    ScanMode,
    ScanStage,
    ScanStatus,
    Severity,
)
from jenkins_watchdog.infrastructure.uow import SqlAlchemyUnitOfWork


class Clock:
    def __init__(self) -> None:
        self.value = datetime.now(timezone.utc) + timedelta(seconds=1)

    def __call__(self) -> datetime:
        return self.value


class Runner:
    check_names = ("mixed_check",)

    def __init__(self, outcome: str) -> None:
        self.outcome = outcome
        self.calls = 0

    def checks_for_categories(self, categories: tuple[str, ...]) -> tuple[str, ...]:
        del categories
        return self.check_names

    async def run(self, scan_id: str, check_name: str, mode: ScanMode) -> CheckResult:
        del mode
        self.calls += 1
        now = datetime.now(timezone.utc)
        if self.outcome == "failed":
            return CheckResult(
                scan_id=scan_id,
                check_name=check_name,
                status=CheckStatus.FAILED,
                categories=frozenset({"jenkins_agent", "jenkins_controller"}),
                failure_summary="detector unavailable",
                started_at=now,
                completed_at=now,
            )
        findings = ()
        if self.outcome == "finding":
            findings = (
                FindingObservation(
                    scan_id=scan_id,
                    check_name=check_name,
                    rule_id="agent.connection.v1",
                    resource_id="agent/linux-1",
                    severity=Severity.WARNING,
                    category="jenkins_agent",
                    summary="agent disconnected",
                    observed_at=now,
                    identity_dimensions={"agent_pool": "linux", "symptom_family": "disconnect"},
                ),
                FindingObservation(
                    scan_id=scan_id,
                    check_name=check_name,
                    rule_id="controller.queue.v1",
                    resource_id="controller/main",
                    severity=Severity.WARNING,
                    category="jenkins_controller",
                    summary="controller overloaded",
                    observed_at=now,
                ),
            )
        return CheckResult(
            scan_id=scan_id,
            check_name=check_name,
            status=CheckStatus.SUCCEEDED,
            findings=findings,
            categories=frozenset({"jenkins_agent", "jenkins_controller"}),
            started_at=now,
            completed_at=now,
        )


class CancellingRunner(Runner):
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__("finding")
        self._factory = factory

    async def run(self, scan_id: str, check_name: str, mode: ScanMode) -> CheckResult:
        result = await super().run(scan_id, check_name, mode)
        async with SqlAlchemyUnitOfWork(self._factory) as uow:
            await uow.scans.request_cancel(scan_id, now=datetime.now(timezone.utc))
            await uow.commit()
        return result


class NoReasoning:
    async def investigate_if_needed(self, incident_id: str):
        del incident_id
        return None


class FailingReasoning:
    async def investigate_if_needed(self, incident_id: str):
        del incident_id
        raise RuntimeError("reasoning interrupted")


class NoAutomation:
    async def plan(self, incident_id: str):
        del incident_id
        return ()


class FailingPipeline:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, scan_id: str):
        del scan_id
        self.calls += 1
        raise RuntimeError("worker terminated")


class CancelledPipeline:
    async def execute(self, scan_id: str):
        del scan_id
        raise ScanCancelled


def uow_factory(factory: async_sessionmaker[AsyncSession]):
    return lambda: SqlAlchemyUnitOfWork(factory)


async def enqueue(factory: async_sessionmaker[AsyncSession], categories: tuple[str, ...]) -> str:
    async with SqlAlchemyUnitOfWork(factory) as uow:
        scan = await uow.scans.add(EnqueueScan(mode=ScanMode.REGULAR, categories=categories))
        await uow.commit()
    return scan.id


def pipeline(
    factory: async_sessionmaker[AsyncSession],
    runner: Runner,
    *,
    reasoning=NoReasoning(),
) -> ScanPipeline:
    factory_port = uow_factory(factory)
    return ScanPipeline(
        uow_factory=factory_port,
        check_runner=runner,
        incident_service=IncidentService(factory_port),
        reasoning_service=reasoning,
        automation_service=NoAutomation(),
        events=EventService(factory_port, NullEventNotifier()),
        now=lambda: datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_pipeline_filters_mixed_results_after_successful_execution(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    scan_id = await enqueue(postgres_session_factory, ("jenkins_agent",))

    completed = await pipeline(postgres_session_factory, Runner("finding")).execute(scan_id)

    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        incident_ids = await uow.incidents.observed_ids_for_scan(scan_id)
        observations = [item for incident_id in incident_ids for item in await uow.incidents.observations(incident_id)]
    assert completed.status is ScanStatus.SUCCEEDED
    assert [item.category for item in observations] == ["jenkins_agent"]


@pytest.mark.asyncio
async def test_failed_responsible_check_does_not_resolve_incident_but_later_success_does(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    first_id = await enqueue(postgres_session_factory, ("jenkins_agent",))
    await pipeline(postgres_session_factory, Runner("finding")).execute(first_id)
    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        incident_id = next(iter(await uow.incidents.observed_ids_for_scan(first_id)))

    failed_id = await enqueue(postgres_session_factory, ("jenkins_agent",))
    await pipeline(postgres_session_factory, Runner("failed")).execute(failed_id)
    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        after_failure = await uow.incidents.get(incident_id)
    assert after_failure is not None and after_failure.status.value == "open"

    success_id = await enqueue(postgres_session_factory, ("jenkins_agent",))
    await pipeline(postgres_session_factory, Runner("empty")).execute(success_id)
    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        resolved = await uow.incidents.get(incident_id)
    assert resolved is not None and resolved.status.value == "resolved"


@pytest.mark.asyncio
async def test_pipeline_resume_does_not_rerun_terminal_check(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    scan_id = await enqueue(postgres_session_factory, ("jenkins_agent",))
    runner = Runner("finding")

    with pytest.raises(RuntimeError, match="reasoning interrupted"):
        await pipeline(postgres_session_factory, runner, reasoning=FailingReasoning()).execute(scan_id)
    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        interrupted = await uow.scans.get(scan_id)
    assert interrupted is not None and interrupted.stage is ScanStage.INVESTIGATING

    completed = await pipeline(postgres_session_factory, runner).execute(scan_id)

    assert completed.status is ScanStatus.SUCCEEDED
    assert runner.calls == 1


@pytest.mark.asyncio
async def test_worker_schedules_two_recoveries_then_fails_third_attempt(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    scan_id = await enqueue(postgres_session_factory, ("jenkins_agent",))
    clock = Clock()
    failing = FailingPipeline()
    factory_port = uow_factory(postgres_session_factory)
    worker = ScanWorker(
        owner="worker-a",
        uow_factory=factory_port,
        pipeline=failing,
        events=EventService(factory_port, NullEventNotifier()),
        now=clock,
        heartbeat_seconds=3600,
    )

    for expected_delay in (timedelta(minutes=1), timedelta(minutes=5), None):
        assert await worker.run_once()
        async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
            scan = await uow.scans.get(scan_id)
        assert scan is not None
        if expected_delay is None:
            assert scan.status is ScanStatus.FAILED
        else:
            assert scan.status is ScanStatus.QUEUED
            assert scan.next_attempt_at == clock.value + expected_delay
            clock.value = scan.next_attempt_at

    assert failing.calls == 3


@pytest.mark.asyncio
async def test_pipeline_persists_requested_cancellation(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    scan_id = await enqueue(postgres_session_factory, ("jenkins_agent",))
    now = datetime.now(timezone.utc)
    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        await uow.scans.request_cancel(scan_id, now=now)
        await uow.commit()

    with pytest.raises(ScanCancelled):
        await pipeline(postgres_session_factory, Runner("empty")).execute(scan_id)

    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        cancelled = await uow.scans.get(scan_id)
    assert cancelled is not None and cancelled.status is ScanStatus.CANCELLED


@pytest.mark.asyncio
async def test_cancellation_after_detection_stops_before_incident_reconciliation(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    scan_id = await enqueue(postgres_session_factory, ("jenkins_agent",))

    with pytest.raises(ScanCancelled):
        await pipeline(postgres_session_factory, CancellingRunner(postgres_session_factory)).execute(scan_id)

    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        cancelled = await uow.scans.get(scan_id)
        incident_ids = await uow.incidents.observed_ids_for_scan(scan_id)
    assert cancelled is not None and cancelled.status is ScanStatus.CANCELLED
    assert incident_ids == frozenset()


@pytest.mark.asyncio
async def test_worker_no_work_health_stopped_loop_and_missing_failure_are_safe(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    factory_port = uow_factory(postgres_session_factory)
    worker = ScanWorker(
        owner="worker-a",
        uow_factory=factory_port,
        pipeline=FailingPipeline(),
        events=EventService(factory_port, NullEventNotifier()),
        now=lambda: datetime.now(timezone.utc),
        poll_interval_seconds=0.001,
    )

    assert not await worker.run_once()
    assert await worker.healthy()
    stop = asyncio.Event()
    stop.set()
    await worker.run_forever(stop)
    await worker._record_failure(str(uuid.uuid4()), RuntimeError("gone"))


@pytest.mark.asyncio
async def test_worker_handles_pipeline_cancellation_and_detects_lost_lease(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    factory_port = uow_factory(postgres_session_factory)
    cancelled_id = await enqueue(postgres_session_factory, ("jenkins_agent",))
    worker = ScanWorker(
        owner="worker-a",
        uow_factory=factory_port,
        pipeline=CancelledPipeline(),
        events=EventService(factory_port, NullEventNotifier()),
        now=lambda: datetime.now(timezone.utc),
        heartbeat_seconds=3600,
    )
    assert await worker.run_once()

    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        cancelled = await uow.scans.get(cancelled_id)
        assert cancelled is not None
        await uow.scans.save(cancelled.cancel(now=datetime.now(timezone.utc)))
        await uow.commit()

    heartbeat_id = await enqueue(postgres_session_factory, ("jenkins_agent",))
    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        claimed = await uow.scans.claim(
            owner="another-worker",
            now=datetime.now(timezone.utc) + timedelta(seconds=1),
            lease_seconds=60,
        )
        await uow.commit()
    assert claimed is not None and claimed.id == heartbeat_id

    lost = ScanWorker(
        owner="worker-a",
        uow_factory=factory_port,
        pipeline=FailingPipeline(),
        events=EventService(factory_port, NullEventNotifier()),
        now=lambda: datetime.now(timezone.utc),
        heartbeat_seconds=0,
    )
    with pytest.raises(RuntimeError, match="lost scan lease"):
        await lost._heartbeat(heartbeat_id)
