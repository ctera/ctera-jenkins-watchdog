"""Durable investigation queue and worker orchestration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any
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


class DailyLLMBudgetExceeded(RuntimeError):
    metric: str

    def __init__(self, *, budget_kind: InvestigationBudgetKind, reset_at: datetime, message: str) -> None:
        self.budget_kind = budget_kind
        self.reset_at = reset_at
        super().__init__(message)

    def metadata(self) -> dict[str, Any]:
        raise NotImplementedError


class InvestigationBudgetExceeded(DailyLLMBudgetExceeded):
    metric = "tokens"

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
        self.limit = limit
        self.projected = projected
        self.spent = spent
        self.active_reserved = active_reserved
        self.requested = requested
        super().__init__(
            budget_kind=budget_kind,
            reset_at=reset_at,
            message=(
                f"{budget_kind.value} daily LLM token budget exhausted: "
                f"{projected:,} used or reserved exceeds {limit:,}; resets at {reset_at.isoformat()}"
            ),
        )

    def metadata(self) -> dict[str, Any]:
        return {
            "budget_metric": self.metric,
            "budget_kind": self.budget_kind.value,
            "budget_limit_tokens": self.limit,
            "budget_spent_tokens": self.spent,
            "budget_active_reserved_tokens": self.active_reserved,
            "budget_requested_tokens": self.requested,
            "budget_projected_tokens": self.projected,
            "budget_reset_at": self.reset_at.isoformat(),
        }


class InvestigationCostBudgetExceeded(DailyLLMBudgetExceeded):
    metric = "cost_usd"

    def __init__(
        self,
        *,
        budget_kind: InvestigationBudgetKind,
        limit_usd: Decimal,
        projected_usd: Decimal,
        spent_usd: Decimal,
        active_reserved_usd: Decimal,
        requested_usd: Decimal,
        reset_at: datetime,
    ) -> None:
        self.limit_usd = limit_usd
        self.projected_usd = projected_usd
        self.spent_usd = spent_usd
        self.active_reserved_usd = active_reserved_usd
        self.requested_usd = requested_usd
        super().__init__(
            budget_kind=budget_kind,
            reset_at=reset_at,
            message=(
                f"{budget_kind.value} daily LLM cost budget exhausted: "
                f"${projected_usd:.4f} used or reserved exceeds ${limit_usd:.2f}; "
                f"resets at {reset_at.isoformat()}"
            ),
        )

    def metadata(self) -> dict[str, Any]:
        return {
            "budget_metric": self.metric,
            "budget_kind": self.budget_kind.value,
            "budget_limit_usd": float(self.limit_usd),
            "budget_spent_usd": float(self.spent_usd),
            "budget_active_reserved_usd": float(self.active_reserved_usd),
            "budget_requested_usd": float(self.requested_usd),
            "budget_projected_usd": float(self.projected_usd),
            "budget_reset_at": self.reset_at.isoformat(),
        }


class InvestigationQueueService:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        now: Callable[[], datetime],
        # Keep these in step with the matching llm_* defaults in config.py; bootstrap always
        # passes explicit values, so drift here only ever surfaces in tests.
        token_budget: int = 200_000,
        deep_token_budget: int = 300_000,
        # What to reserve per investigation. Defaults to the hard budget so existing callers
        # keep their behaviour; pass the expected cost to stop one scan reserving the day.
        reservation_tokens: int | None = None,
        deep_reservation_tokens: int | None = None,
        daily_token_budget: int = 20_000_000,
        manual_token_reserve: int = 4_000_000,
        daily_cost_budget_usd: Decimal = Decimal("30.00"),
        manual_cost_reserve_usd: Decimal = Decimal("5.00"),
        max_token_cost_usd_per_million: Decimal = Decimal("5.00"),
    ) -> None:
        self._uow_factory = uow_factory
        self._now = now
        self._token_budget = max(0, token_budget)
        self._deep_token_budget = max(0, deep_token_budget)
        self._reservation_tokens = self._token_budget if reservation_tokens is None else max(0, reservation_tokens)
        self._deep_reservation_tokens = (
            self._deep_token_budget if deep_reservation_tokens is None else max(0, deep_reservation_tokens)
        )
        self._daily_token_budget = max(0, daily_token_budget)
        self._manual_token_reserve = min(self._daily_token_budget, max(0, manual_token_reserve))
        self._daily_cost_budget_usd = max(Decimal("0"), Decimal(str(daily_cost_budget_usd)))
        self._manual_cost_reserve_usd = min(
            self._daily_cost_budget_usd,
            max(Decimal("0"), Decimal(str(manual_cost_reserve_usd))),
        )
        self._max_token_cost_usd_per_million = max(
            Decimal("0"),
            Decimal(str(max_token_cost_usd_per_million)),
        )

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

    async def ensure_chat_budget_available(self) -> None:
        await self.ensure_budget_available(
            budget_kind=InvestigationBudgetKind.MANUAL,
            reserved_tokens=self._reservation_tokens,
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
        defer_budget: bool = False,
    ) -> InvestigationRequest | None:
        now = self._now()
        kind = budget_kind or (
            InvestigationBudgetKind.MANUAL
            if requested_by or source.startswith("manual") or source.startswith("api")
            else InvestigationBudgetKind.AUTOMATIC
        )
        reserved_tokens = self._deep_reservation_tokens if mode is ScanMode.DEEP else self._reservation_tokens
        async with self._uow_factory() as uow:
            await uow.investigation_requests.lock_budget()
            incident = await uow.incidents.get(incident_id)
            if incident is None:
                raise LookupError(f"incident {incident_id} does not exist")
            active = await uow.investigation_requests.active_for_incident(incident_id)
            if active is not None:
                return active
            observations = await uow.incidents.observations(incident_id)
            build = await uow.jenkins.build_detail(build_id) if build_id else None
            builds = (build,) if build else await uow.jenkins.builds_for_incident(incident_id)
            latest = await uow.investigations.latest_for_incident(incident_id)
            digest = evidence_digest(observations + jenkins_build_observations(builds))
            if not force and not should_reinvestigate(
                incident=incident,
                latest=latest,
                evidence_hash=digest,
                now=now,
            ):
                return None
            if not defer_budget:
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
        if not self._daily_token_budget and not self._daily_cost_budget_usd:
            return
        day_start = datetime.combine(now.astimezone(timezone.utc).date(), time.min, tzinfo=timezone.utc)
        spent = await uow.llm_calls.summary_since(day_start)
        active_reserved = await uow.investigation_requests.active_reserved_tokens()
        reset_at = day_start + timedelta(days=1)
        if self._daily_token_budget:
            ceiling = self._daily_token_budget
            if budget_kind is InvestigationBudgetKind.AUTOMATIC:
                ceiling -= self._manual_token_reserve
            spent_tokens = int(spent.get("total_tokens") or 0)
            projected = spent_tokens + active_reserved + reserved_tokens
            if projected > ceiling:
                raise InvestigationBudgetExceeded(
                    budget_kind=budget_kind,
                    limit=ceiling,
                    projected=projected,
                    spent=spent_tokens,
                    active_reserved=active_reserved,
                    requested=reserved_tokens,
                    reset_at=reset_at,
                )

        if self._daily_cost_budget_usd:
            cost_ceiling = self._daily_cost_budget_usd
            if budget_kind is InvestigationBudgetKind.AUTOMATIC:
                cost_ceiling -= self._manual_cost_reserve_usd
            spent_cost = Decimal(str(spent.get("estimated_cost_usd") or 0))
            spent_cost += self._reserved_cost(int(spent.get("unpriced_chargeable_tokens") or 0))
            active_reserved_cost = self._reserved_cost(active_reserved)
            requested_cost = self._reserved_cost(reserved_tokens)
            projected_cost = spent_cost + active_reserved_cost + requested_cost
            if projected_cost > cost_ceiling:
                raise InvestigationCostBudgetExceeded(
                    budget_kind=budget_kind,
                    limit_usd=cost_ceiling,
                    projected_usd=projected_cost,
                    spent_usd=spent_cost,
                    active_reserved_usd=active_reserved_cost,
                    requested_usd=requested_cost,
                    reset_at=reset_at,
                )

    def _reserved_cost(self, tokens: int) -> Decimal:
        return Decimal(max(0, tokens)) * self._max_token_cost_usd_per_million / Decimal(1_000_000)

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
        except DailyLLMBudgetExceeded as exc:
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
                        **exc.metadata(),
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
            reasoning_kwargs: dict[str, Any] = {
                "force": True,
                "mode": request.mode,
                "on_progress": progress,
                "budget_kind": request.budget_kind,
                "scan_id": request.scan_id,
            }
            if request.build_id:
                reasoning_kwargs["build_id"] = request.build_id
            investigation = await self._reasoning.investigate_if_needed(
                request.incident_id,
                **reasoning_kwargs,
            )
            now = self._now()
            if investigation is None:
                raise RuntimeError("reasoning returned no investigation")
            if investigation.status in {InvestigationStatus.SUCCEEDED, InvestigationStatus.PARTIAL}:
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
                        "investigation_status": investigation.status.value,
                        "confidence": investigation.confidence.value if investigation.confidence else None,
                        "error_summary": investigation.error_summary,
                    },
                    now=now,
                )
            if (
                completed.status is InvestigationRequestStatus.SUCCEEDED
                and investigation.status is InvestigationStatus.SUCCEEDED
            ):
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
