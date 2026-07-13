"""Durable scan worker with leases, heartbeats, and bounded recovery."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from jenkins_watchdog.application.events import EventService
from jenkins_watchdog.application.pipeline import ScanCancelled, ScanPipeline
from jenkins_watchdog.application.ports import UnitOfWorkFactory

logger = logging.getLogger(__name__)

SCAN_RECOVERY_DELAYS = (timedelta(minutes=1), timedelta(minutes=5))


class ScanWorker:
    def __init__(
        self,
        *,
        owner: str,
        uow_factory: UnitOfWorkFactory,
        pipeline: ScanPipeline,
        events: EventService,
        now: Callable[[], datetime],
        lease_seconds: int = 60,
        heartbeat_seconds: int = 15,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        self.owner = owner
        self._uow_factory = uow_factory
        self._pipeline = pipeline
        self._events = events
        self._now = now
        self._lease_seconds = lease_seconds
        self._heartbeat_seconds = heartbeat_seconds
        self._poll_interval_seconds = poll_interval_seconds

    async def run_once(self) -> bool:
        async with self._uow_factory() as uow:
            scan = await uow.scans.claim(owner=self.owner, now=self._now(), lease_seconds=self._lease_seconds)
            await uow.commit()
        if scan is None:
            return False
        await self._events.append(
            scan.id,
            "scan_started" if scan.attempt_count == 1 else "scan_recovered",
            {"worker": self.owner, "attempt": scan.attempt_count, "stage": scan.stage.value},
            now=self._now(),
        )
        heartbeat = asyncio.create_task(self._heartbeat(scan.id))
        pipeline = asyncio.create_task(self._pipeline.execute(scan.id))
        try:
            done, _ = await asyncio.wait({pipeline, heartbeat}, return_when=asyncio.FIRST_COMPLETED)
            if heartbeat in done:
                error = heartbeat.exception()
                if error is not None:
                    pipeline.cancel()
                    await asyncio.gather(pipeline, return_exceptions=True)
                    raise error
            await pipeline
        except ScanCancelled:
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("scan %s failed", scan.id)
            await self._record_failure(scan.id, exc)
        finally:
            pipeline.cancel()
            heartbeat.cancel()
            await asyncio.gather(pipeline, heartbeat, return_exceptions=True)
        return True

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        while not stop.is_set():
            worked = await self.run_once()
            if worked:
                continue
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._poll_interval_seconds)
            except TimeoutError:
                pass

    async def healthy(self) -> bool:
        async with self._uow_factory() as uow:
            await uow.scans.active()
        return True

    async def _heartbeat(self, scan_id: str) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_seconds)
            async with self._uow_factory() as uow:
                owned = await uow.scans.heartbeat(
                    scan_id,
                    owner=self.owner,
                    now=self._now(),
                    lease_seconds=self._lease_seconds,
                )
                await uow.commit()
            if not owned:
                raise RuntimeError(f"lost scan lease for {scan_id}")

    async def _record_failure(self, scan_id: str, exc: Exception) -> None:
        now = self._now()
        async with self._uow_factory() as uow:
            current = await uow.scans.get(scan_id)
            if current is None:
                return
            retry_at = None
            if current.attempt_count <= len(SCAN_RECOVERY_DELAYS):
                retry_at = now + SCAN_RECOVERY_DELAYS[current.attempt_count - 1]
            failed = current.fail(summary=_failure_summary(exc), now=now, retry_at=retry_at)
            await uow.scans.save(failed)
            await uow.commit()
        await self._events.append(
            scan_id,
            "scan_retry_scheduled" if retry_at else "scan_failed",
            {"attempt": current.attempt_count, "next_attempt_at": retry_at.isoformat() if retry_at else None},
            now=now,
        )


def _failure_summary(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:500]
