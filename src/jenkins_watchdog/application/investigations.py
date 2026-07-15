"""Durable investigation queue and worker orchestration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import uuid4

from jenkins_watchdog.application.automation import AutomationService
from jenkins_watchdog.application.events import EventService
from jenkins_watchdog.application.ports import UnitOfWorkFactory
from jenkins_watchdog.application.reasoning import (
    ReasoningService,
    evidence_digest,
    jenkins_build_observations,
    should_reinvestigate,
)
from jenkins_watchdog.domain.model import (
    InvestigationRequest,
    InvestigationRequestStatus,
    InvestigationStatus,
    ScanMode,
)

logger = logging.getLogger(__name__)


class InvestigationQueueService:
    def __init__(self, *, uow_factory: UnitOfWorkFactory, now: Callable[[], datetime]) -> None:
        self._uow_factory = uow_factory
        self._now = now

    async def enqueue_incident(
        self,
        incident_id: str,
        *,
        source: str,
        mode: ScanMode = ScanMode.REGULAR,
        priority: int = 0,
        scan_id: str | None = None,
        build_id: str | None = None,
        requested_by: str | None = None,
        force: bool = False,
    ) -> InvestigationRequest | None:
        now = self._now()
        async with self._uow_factory() as uow:
            incident = await uow.incidents.get(incident_id)
            if incident is None:
                raise LookupError(f"incident {incident_id} does not exist")
            active = await uow.investigation_requests.active_for_incident(incident_id)
            if active is not None:
                return active
            observations = await uow.incidents.observations(incident_id)
            builds = await uow.jenkins.builds_for_incident(incident_id)
            latest = await uow.investigations.latest_for_incident(incident_id)
            digest = evidence_digest(observations + jenkins_build_observations(builds))
            if not force and not should_reinvestigate(
                incident=incident,
                latest=latest,
                evidence_hash=digest,
                now=now,
            ):
                return None
            request = InvestigationRequest(
                id=str(uuid4()),
                incident_id=incident.id,
                occurrence_id=incident.current_occurrence.id,
                mode=mode,
                source=source[:32],
                priority=max(0, min(100, priority)),
                evidence_hash=digest,
                status=InvestigationRequestStatus.QUEUED,
                scan_id=scan_id,
                build_id=build_id,
                requested_by=requested_by,
                created_at=now,
                updated_at=now,
                next_attempt_at=now,
            )
            persisted = await uow.investigation_requests.enqueue(request)
            await uow.commit()
            return persisted

    async def latest_for_incident(self, incident_id: str) -> InvestigationRequest | None:
        async with self._uow_factory() as uow:
            return await uow.investigation_requests.latest_for_incident(incident_id)


class InvestigationWorker:
    def __init__(
        self,
        *,
        owner: str,
        uow_factory: UnitOfWorkFactory,
        reasoning: ReasoningService,
        queue: InvestigationQueueService,
        automation: AutomationService,
        events: EventService,
        now: Callable[[], datetime],
        lease_seconds: int = 300,
        heartbeat_seconds: int = 30,
        poll_interval_seconds: float = 1.0,
        max_attempts: int = 3,
    ) -> None:
        self.owner = owner
        self._uow_factory = uow_factory
        self._reasoning = reasoning
        self._queue = queue
        self._automation = automation
        self._events = events
        self._now = now
        self._lease_seconds = max(60, lease_seconds)
        self._heartbeat_seconds = max(5, min(heartbeat_seconds, self._lease_seconds // 2))
        self._poll_interval_seconds = max(0.1, poll_interval_seconds)
        self._max_attempts = max(1, max_attempts)

    async def run_once(self) -> InvestigationRequest | None:
        async with self._uow_factory() as uow:
            request = await uow.investigation_requests.claim(
                owner=self.owner,
                now=self._now(),
                lease_seconds=self._lease_seconds,
            )
            await uow.commit()
        if request is None:
            return None

        heartbeat = asyncio.create_task(self._heartbeat(request.id))

        async def progress(event: dict[str, object]) -> None:
            if request.scan_id:
                await self._events.append(
                    request.scan_id,
                    f"agent_{event.get('type', 'progress')}",
                    {"incident_id": request.incident_id, "request_id": request.id, **event},
                    now=self._now(),
                )

        try:
            if request.scan_id:
                await self._events.append(
                    request.scan_id,
                    "investigation_started",
                    {
                        "incident_id": request.incident_id,
                        "request_id": request.id,
                        "mode": request.mode.value,
                        "source": request.source,
                    },
                    now=self._now(),
                )
            investigation = await self._reasoning.investigate_if_needed(
                request.incident_id,
                force=True,
                mode=request.mode,
                on_progress=progress,
            )
            now = self._now()
            if investigation is None:
                raise RuntimeError("reasoning returned no investigation")
            if investigation.status is InvestigationStatus.SUCCEEDED:
                completed = request.succeed(investigation.id, now=now)
            else:
                completed = request.fail(
                    investigation.error_summary or "investigation failed",
                    now=now,
                    retry_at=_retry_at(request, now=now, max_attempts=self._max_attempts),
                )
            async with self._uow_factory() as uow:
                await uow.investigation_requests.save(completed)
                await uow.commit()
            if request.scan_id:
                await self._events.append(
                    request.scan_id,
                    "investigation_completed",
                    {
                        "incident_id": request.incident_id,
                        "request_id": request.id,
                        "investigation_id": investigation.id,
                        "status": completed.status.value,
                        "confidence": investigation.confidence.value if investigation.confidence else None,
                        "error_summary": investigation.error_summary,
                    },
                    now=now,
                )
            if completed.status is InvestigationRequestStatus.SUCCEEDED:
                try:
                    await self._automation.plan(request.incident_id)
                except Exception:
                    logger.exception("action planning failed after investigation %s", investigation.id)
                await self._queue.enqueue_incident(
                    request.incident_id,
                    source="evidence_follow_up",
                    mode=request.mode,
                    priority=request.priority,
                )
            return completed
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("investigation request %s failed", request.id)
            now = self._now()
            failed = request.fail(
                f"{type(exc).__name__}: {exc}",
                now=now,
                retry_at=_retry_at(request, now=now, max_attempts=self._max_attempts),
            )
            async with self._uow_factory() as uow:
                await uow.investigation_requests.save(failed)
                await uow.commit()
            return failed
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        while not stop.is_set():
            try:
                claimed = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("investigation worker iteration failed")
                claimed = None
            if claimed is not None:
                continue
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._poll_interval_seconds)
            except TimeoutError:
                pass

    async def _heartbeat(self, request_id: str) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_seconds)
            async with self._uow_factory() as uow:
                owned = await uow.investigation_requests.heartbeat(
                    request_id,
                    owner=self.owner,
                    now=self._now(),
                    lease_seconds=self._lease_seconds,
                )
                await uow.commit()
            if not owned:
                raise RuntimeError("lost investigation request lease")


def _retry_at(request: InvestigationRequest, *, now: datetime, max_attempts: int) -> datetime | None:
    if request.attempt_count >= max_attempts:
        return None
    return now + timedelta(seconds=min(300, 5 * (2 ** max(0, request.attempt_count - 1))))
