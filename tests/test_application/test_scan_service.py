import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jenkins_watchdog.application.events import EventService
from jenkins_watchdog.application.scan_service import (
    EnqueueScanCommand,
    ScanAlreadyActiveError,
    ScanService,
    UnknownScanCategoryError,
)
from jenkins_watchdog.domain.model import ScanMode, ScanStatus
from jenkins_watchdog.infrastructure.memory import InMemoryUnitOfWorkFactory
from jenkins_watchdog.infrastructure.uow import SqlAlchemyUnitOfWork


class RecordingNotifier:
    def __init__(self, *, fail: bool = False):
        self.events = []
        self.fail = fail

    async def publish(self, event):
        self.events.append(event)
        if self.fail:
            raise RuntimeError("Valkey unavailable")


@pytest.mark.asyncio
async def test_enqueue_rejects_unknown_categories():
    service = ScanService(InMemoryUnitOfWorkFactory(), known_categories=frozenset({"k8s_node"}))

    with pytest.raises(UnknownScanCategoryError) as exc:
        await service.enqueue(EnqueueScanCommand(mode=ScanMode.REGULAR, categories=frozenset({"bad"})))

    assert exc.value.categories == {"bad"}


@pytest.mark.asyncio
async def test_enqueue_rejects_active_scan():
    factory = InMemoryUnitOfWorkFactory()
    service = ScanService(factory, known_categories=frozenset({"k8s_node"}))
    await service.enqueue(EnqueueScanCommand(mode=ScanMode.REGULAR, categories=frozenset()))

    with pytest.raises(ScanAlreadyActiveError) as exc:
        await service.enqueue(EnqueueScanCommand(mode=ScanMode.REGULAR, categories=frozenset()))

    assert exc.value.active_scan.status == ScanStatus.QUEUED


@pytest.mark.asyncio
async def test_enqueue_delegates_to_queue():
    factory = InMemoryUnitOfWorkFactory()
    service = ScanService(factory, known_categories=frozenset({"k8s_node"}))

    scan = await service.enqueue(EnqueueScanCommand(mode=ScanMode.DEEP, categories=frozenset({"k8s_node"})))

    assert scan.status == ScanStatus.QUEUED
    assert scan.categories == frozenset({"k8s_node"})


@pytest.mark.asyncio
async def test_enqueue_and_cancel_persist_before_best_effort_notification():
    factory = InMemoryUnitOfWorkFactory()
    notifier = RecordingNotifier(fail=True)
    events = EventService(factory, notifier)
    service = ScanService(factory, events=events, known_categories=frozenset({"k8s_node"}))

    scan = await service.enqueue(EnqueueScanCommand(mode=ScanMode.REGULAR, categories=frozenset()))
    cancelled = await service.cancel(scan.id, now=scan.created_at)
    repeated = await service.cancel(scan.id, now=scan.created_at)

    assert cancelled is not None and cancelled.cancellation_requested
    assert repeated == cancelled
    assert [item.type for item in factory.store.events[scan.id]] == ["scan_queued", "scan_cancel_requested"]
    assert [item.type for item in notifier.events] == ["scan_queued", "scan_cancel_requested"]


@pytest.mark.asyncio
async def test_cancel_unknown_scan_returns_none():
    service = ScanService(InMemoryUnitOfWorkFactory())

    assert await service.cancel("missing", now=datetime.now(timezone.utc)) is None


@pytest.mark.asyncio
async def test_concurrent_postgres_enqueue_returns_active_scan_conflict(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    factory = lambda: SqlAlchemyUnitOfWork(postgres_session_factory)  # noqa: E731
    first = ScanService(factory)
    second = ScanService(factory)
    command = EnqueueScanCommand(mode=ScanMode.REGULAR, categories=frozenset())

    results = await asyncio.gather(first.enqueue(command), second.enqueue(command), return_exceptions=True)

    scans = [result for result in results if not isinstance(result, BaseException)]
    conflicts = [result for result in results if isinstance(result, ScanAlreadyActiveError)]
    assert len(scans) == 1
    assert len(conflicts) == 1
    assert conflicts[0].active_scan.id == scans[0].id
