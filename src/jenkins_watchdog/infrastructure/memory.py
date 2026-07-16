"""Small in-memory unit of work used by isolated application/API tests."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from jenkins_watchdog.application.types import CursorPage, EnqueueScan, ScanEvent
from jenkins_watchdog.domain.model import Scan, ScanStatus


class InMemoryStore:
    def __init__(self) -> None:
        self.scans: dict[str, Scan] = {}
        self.events: dict[str, list[ScanEvent]] = {}


class InMemoryScanRepository:
    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    async def lock_enqueue(self) -> None:
        return None

    async def active(self) -> Scan | None:
        return next((scan for scan in self._store.scans.values() if not scan.terminal), None)

    async def add(self, request: EnqueueScan) -> Scan:
        now = datetime.now(timezone.utc)
        scan = Scan(
            id=str(uuid.uuid4()),
            mode=request.mode,
            categories=frozenset(request.categories),
            status=ScanStatus.QUEUED,
            created_at=now,
            triggering_user_email=request.triggering_user_email,
            scheduled=request.scheduled,
            next_attempt_at=now,
            updated_at=now,
        )
        self._store.scans[scan.id] = scan
        return scan

    async def get(self, scan_id: str) -> Scan | None:
        return self._store.scans.get(scan_id)

    async def list(self, *, limit: int, cursor: str | None = None) -> CursorPage:
        del cursor
        scans = sorted(self._store.scans.values(), key=lambda scan: (scan.created_at, scan.id), reverse=True)
        return CursorPage(tuple(scans[:limit]), None)

    async def request_cancel(self, scan_id: str, *, now: datetime) -> tuple[Scan, bool] | None:
        current = self._store.scans.get(scan_id)
        if current is None:
            return None
        scan = current.request_cancel(now=now)
        self._store.scans[scan_id] = scan
        return scan, scan != current


class InMemoryEventRepository:
    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    async def append(self, scan_id: str, event_type: str, payload: dict[str, Any], *, now: datetime) -> ScanEvent:
        events = self._store.events.setdefault(scan_id, [])
        event = ScanEvent(scan_id, len(events) + 1, event_type, now, 1, payload)
        events.append(event)
        return event

    async def after(self, scan_id: str, sequence: int, *, limit: int = 500) -> tuple[ScanEvent, ...]:
        return tuple(event for event in self._store.events.get(scan_id, []) if event.sequence > sequence)[:limit]


class InMemoryScanAnalysisRepository:
    async def for_scan(self, scan_id: str) -> tuple[Any, ...]:
        del scan_id
        return ()


class InMemoryIncidentRepository:
    async def get(self, incident_id: str) -> None:
        del incident_id
        return None


class InMemoryUnitOfWork:
    def __init__(self, store: InMemoryStore) -> None:
        self.scans = InMemoryScanRepository(store)
        self.events = InMemoryEventRepository(store)
        self.analysis_decisions = InMemoryScanAnalysisRepository()
        self.investigation_requests = InMemoryScanAnalysisRepository()
        self.incidents = InMemoryIncidentRepository()

    async def __aenter__(self) -> "InMemoryUnitOfWork":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class InMemoryUnitOfWorkFactory:
    def __init__(self, store: InMemoryStore | None = None) -> None:
        self.store = store or InMemoryStore()

    def __call__(self) -> InMemoryUnitOfWork:
        return InMemoryUnitOfWork(self.store)
