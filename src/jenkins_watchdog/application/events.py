"""Persist-first scan event publishing."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from jenkins_watchdog.application.ports import EventNotifier, UnitOfWorkFactory
from jenkins_watchdog.application.types import ScanEvent

logger = logging.getLogger(__name__)


class EventService:
    def __init__(self, uow_factory: UnitOfWorkFactory, notifier: EventNotifier) -> None:
        self._uow_factory = uow_factory
        self._notifier = notifier

    async def append(self, scan_id: str, event_type: str, payload: dict[str, Any], *, now: datetime) -> ScanEvent:
        async with self._uow_factory() as uow:
            event = await uow.events.append(scan_id, event_type, payload, now=now)
            await uow.commit()
        await self.notify(event)
        return event

    async def notify(self, event: ScanEvent) -> None:
        try:
            await self._notifier.publish(event)
        except Exception:
            logger.warning(
                "scan event %s:%s was persisted but could not be published",
                event.scan_id,
                event.sequence,
                exc_info=True,
            )


class NullEventNotifier:
    async def publish(self, event: ScanEvent) -> None:
        del event
