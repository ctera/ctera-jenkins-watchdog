"""Durable investigation queue and worker orchestration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, time, timedelta, timezone
from uuid import uuid4

from jenkins_watchdog.application.automation import AutomationService
from jenkins_watchdog.application.events import EventService
from jenkins_watchdog.application.ports import UnitOfWork, UnitOfWorkFactory
from jenkins_watchdog.application.reasoning import (
    ReasoningService,
    evidence_digest,
    jenkins_build_observations,
    should_reinvestigate,
)
from jenkins_watchdog.domain.model import (
    InvestigationBudgetKind,
    InvestigationRequest,
    InvestigationRequestStatus,
    InvestigationStatus,
    ScanMode,
)

logger = logging.getLogger(__name__)


class InvestigationBudgetExceeded(RuntimeError):
    def __init__(
        self,
        *,
        budget_kind: InvestigationBudgetKind,
        limit: int,
        projected: int,
        spent: int,
        active_reserved: int,
        requested: int,
        reset_at: datetime,
    ) -> None:
        self.budget_kind = budget_kind
        self.limit = limit
        self.projected = projected
        self.spent = spent
        self.active_reserved = active_reserved
        self.requested = requested
        self.reset_at = reset_at
        super().__init__(
            f"{budget_kind.value} daily LLM token budget exhausted: "
            f"{projected:,} used or reserved exceeds {limit:,}; resets at {reset_at.isoformat()}"
        )


class InvestigationQueueService:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        now: Callable[[], datetime],
        token_budget: int = 24_000,
        deep_token_budget: int = 40_000,
        daily_token_budget: int = 400_000,
        manual_token_reserve: int = 100_000,
    ) -> None:
        self._uow_factory = uow_factory
        self._now = now
        self._token_budget = max(0, token_budget)
        self._deep_token_budget = max(0, deep_token_budget)
        self._daily_token_budget = max(0, daily_token_budget)
        self._manual_token_reserve = min(self._daily_token_budget, max(0, manual_token_reserve))

    async def ensure_budget_available(
        self,
        *,
        budget_kind: InvestigationBudgetKind,
        reserved_tokens: int,
    ) -> None:
        now = self._now()
        async with self._uow_factory() as uow:
            await uow.investigation_requests.lock_budget()
            await self._enforce_budget(
                uow,
                budget_kind=budget_kind,
                reserved_tokens=max(0, reserved_tokens),
                now=now,
            )

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
        budget_kind: InvestigationBudgetKind | None = None,
    ) -> InvestigationRequest | None:
        now = self._now()
        kind = budget_kind or (
            InvestigationBudgetKind.MANUAL
            if requested_by or source.startswith("manual") or source.startswith("api")
            else InvestigationBudgetKind.AUTOMATIC
        )
        reserved_tokens = self._deep_token_budget if mode is ScanMode.DEEP else self._token_budget
        async with self._uow_factory() as uow:
            await uow.investigation_requests.lock_budget()
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
            await self._enforce_budget(
                uow,
                budget_kind=kind,
                reserved_tokens=reserved_tokens,
                now=now,
            )
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
                budget_kind=kind,
                reserved_tokens=reserved_tokens,
                created_at=now,
                updated_at=now,
                next_attempt_at=now,
            )
            persisted = await uow.investigation_requests.enqueue(request)
            await uow.commit()
            return persisted

    async def _enforce_budget(
        self,
        uow: UnitOfWork,
        *,
        budget_kind: InvestigationBudgetKind,
        reserved_tokens: int,
        now: datetime,
    ) -> None:
        if not self._daily_token_budget:
            return
        day_start = datetime.combine(now.astimezone(timezone.utc).date(), time.min, tzinfo=timezone.utc)
        spent = await uow.llm_calls.summary_since(day_start)
        active_reserved = await uow.investigation_requests.active_reserved_tokens()
        ceiling = self._daily_token_budget
        if budget_kind is InvestigationBudgetKind.AUTOMATIC:
            ceiling -= self._manual_token_reserve
        projected = int(spent.get("total_tokens") or 0) + active_reserved + reserved_tokens
        if projected > ceiling:
            raise InvestigationBudgetExceeded(
                budget_kind=budget_kind,
                limit=ceiling,
                projected=projected,
                spent=int(spent.get("total_tokens") or 0),
                active_reserved=active_reserved,
                requested=reserved_tokens,
                reset_at=day_start + timedelta(days=1),
            )

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

        try:
            await self._queue.ensure_budget_available(
                budget_kind=request.budget_kind,
                reserved_tokens=0,
            )
        except InvestigationBudgetExceeded as exc:
            deferred = request.defer_for_budget(
                str(exc),
                now=self._now(),
                retry_at=exc.reset_at,
            )
            async with self._uow_factory() as uow:
                await uow.investigation_requests.save(deferred)
                await uow.commit()
            if request.scan_id:
                await self._events.append(
                    request.scan_id,
                    "investigation_budget_deferred",
                    {
                        "incident_id": request.incident_id,
                        "request_id": request.id,
                        "budget_kind": request.budget_kind.value,
                        "limit_tokens": exc.limit,
                        "projected_tokens": exc.projected,
                        "retry_at": exc.reset_at.isoformat(),
                    },
                    now=self._now(),
                )
            return deferred

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
                budget_kind=request.budget_kind,
                scan_id=request.scan_id,
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
