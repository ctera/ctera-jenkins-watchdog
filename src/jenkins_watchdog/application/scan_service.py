"""v2 scan enqueue use case."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from jenkins_watchdog.application.events import EventService
from jenkins_watchdog.application.ports import UnitOfWorkFactory
from jenkins_watchdog.application.types import EnqueueScan
from jenkins_watchdog.domain.model import Scan, ScanMode

KNOWN_CATEGORIES = frozenset(
    {
        "jenkins_controller",
        "jenkins_agent",
        "jenkins_queue",
        "jenkins_pipeline_pattern",
        "jenkins_failed_build",
        "jenkins_build",
        "k8s_workload",
        "k8s_event",
        "k8s_node",
    }
)


class ScanAlreadyActiveError(Exception):
    def __init__(self, active_scan: Scan) -> None:
        super().__init__("scan already active")
        self.active_scan = active_scan


class UnknownScanCategoryError(ValueError):
    def __init__(self, categories: set[str]) -> None:
        super().__init__("unknown scan categories")
        self.categories = categories


@dataclass(frozen=True, slots=True)
class EnqueueScanCommand:
    mode: ScanMode
    categories: frozenset[str]
    triggering_user_email: str | None = None
    scheduled: bool = False


class ScanService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        *,
        events: EventService | None = None,
        known_categories: frozenset[str] = KNOWN_CATEGORIES,
    ) -> None:
        self._uow_factory = uow_factory
        self._events = events
        self._known_categories = known_categories

    async def enqueue(self, command: EnqueueScanCommand) -> Scan:
        unknown = set(command.categories - self._known_categories)
        if unknown:
            raise UnknownScanCategoryError(unknown)
        async with self._uow_factory() as uow:
            await uow.scans.lock_enqueue()
            active = await uow.scans.active()
            if active is not None:
                raise ScanAlreadyActiveError(active)
            scan = await uow.scans.add(
                EnqueueScan(
                    mode=command.mode,
                    categories=tuple(sorted(command.categories)),
                    triggering_user_email=command.triggering_user_email,
                    scheduled=command.scheduled,
                )
            )
            event = await uow.events.append(
                scan.id,
                "scan_queued",
                {"mode": scan.mode.value, "categories": list(scan.categories)},
                now=scan.created_at,
            )
            await uow.commit()
        if self._events is not None:
            await self._events.notify(event)
        return scan

    async def cancel(self, scan_id: str, *, now: datetime) -> Scan | None:
        async with self._uow_factory() as uow:
            result = await uow.scans.request_cancel(scan_id, now=now)
            if result is None:
                return None
            scan, changed = result
            event = await uow.events.append(scan_id, "scan_cancel_requested", {}, now=now) if changed else None
            await uow.commit()
        if event is not None and self._events is not None:
            await self._events.notify(event)
        return scan
