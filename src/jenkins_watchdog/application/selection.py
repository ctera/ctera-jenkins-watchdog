"""Central policy for deciding which incidents receive agent investigation."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any
from uuid import uuid4

from jenkins_watchdog.application.investigations import (
    DailyLLMBudgetExceeded,
    InvestigationQueueService,
)
from jenkins_watchdog.application.ports import ReasoningPort, UnitOfWorkFactory
from jenkins_watchdog.application.reasoning import (
    evidence_digest,
    jenkins_build_observations,
    should_reinvestigate,
)
from jenkins_watchdog.application.types import SelectionBatchResult, TriageCandidate
from jenkins_watchdog.domain.model import (
    AnalysisDecision,
    AnalysisDecisionOutcome,
    Incident,
    IncidentStatus,
    InvestigationBudgetKind,
    ScanMode,
    Severity,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _Candidate:
    incident: Incident
    evidence_hash: str
    priority: int
    builds: tuple[dict[str, Any], ...]
    observations: tuple[Any, ...]
    build_id: str | None


@dataclass(frozen=True, slots=True)
class _Route:
    candidate: _Candidate
    outcome: AnalysisDecisionOutcome
    reason_code: str
    reason: str
    llm_call_id: str | None = None
    metadata: Mapping[str, Any] | None = None


class AnalysisSelectionService:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        reasoning: ReasoningPort,
        queue: InvestigationQueueService,
        now: Callable[[], datetime],
        triage_batch_size: int = 50,
        triage_token_reservation: int = 2_048,
        automatic_enabled: bool = True,
    ) -> None:
        self._uow_factory = uow_factory
        self._reasoning = reasoning
        self._queue = queue
        self._now = now
        self._triage_batch_size = max(1, triage_batch_size)
        self._triage_token_reservation = max(0, triage_token_reservation)
        self._automatic_enabled = automatic_enabled

    async def select(
        self,
        incident_ids: tuple[str, ...],
        *,
        source: str,
        mode: ScanMode,
        limit: int,
        scan_id: str | None = None,
        priority_by_incident: Mapping[str, int] | None = None,
        build_id_by_incident: Mapping[str, str] | None = None,
    ) -> SelectionBatchResult:
        candidates = await self._load_candidates(
            incident_ids,
            priority_by_incident=priority_by_incident or {},
            build_id_by_incident=build_id_by_incident or {},
        )
        routes: list[_Route] = []
        uncertain: list[_Candidate] = []
        for candidate in candidates:
            route = await self._deterministic_route(candidate, mode=mode)
            if route is None:
                uncertain.append(candidate)
            else:
                routes.append(route)

        for offset in range(0, len(uncertain), self._triage_batch_size):
            batch = uncertain[offset : offset + self._triage_batch_size]
            routes.extend(await self._triage(batch, scan_id=scan_id))

        selected = sorted(
            (route for route in routes if route.outcome is AnalysisDecisionOutcome.SELECTED),
            key=lambda route: (
                route.candidate.priority,
                _severity_rank(route.candidate.incident.severity),
                route.candidate.incident.updated_at or route.candidate.incident.created_at,
            ),
            reverse=True,
        )
        allowed_ids = {route.candidate.incident.id for route in selected[: max(0, limit)]}
        if len(selected) > limit:
            routes = [
                (
                    route
                    if route.outcome is not AnalysisDecisionOutcome.SELECTED
                    or route.candidate.incident.id in allowed_ids
                    else replace(
                        route,
                        outcome=AnalysisDecisionOutcome.DEFERRED,
                        reason_code="cycle_limit_reached",
                        reason=f"Deferred because this {mode.value} cycle is limited to {limit} investigations.",
                    )
                )
                for route in routes
            ]

        request_ids: list[str] = []
        request_id_by_incident: dict[str, str] = {}
        final_routes: list[_Route] = []
        for route in routes:
            if route.outcome is not AnalysisDecisionOutcome.SELECTED:
                final_routes.append(route)
                continue
            try:
                request = await self._queue.enqueue_incident(
                    route.candidate.incident.id,
                    source=source,
                    mode=mode,
                    priority=route.candidate.priority,
                    scan_id=scan_id,
                    build_id=route.candidate.build_id,
                    budget_kind=InvestigationBudgetKind.AUTOMATIC,
                )
            except DailyLLMBudgetExceeded as exc:
                final_routes.append(
                    replace(
                        route,
                        outcome=AnalysisDecisionOutcome.BUDGET_DEFERRED,
                        reason_code="daily_budget_exhausted",
                        reason=str(exc),
                        metadata=_budget_metadata(exc),
                    )
                )
                continue
            if request is None:
                final_routes.append(
                    replace(
                        route,
                        outcome=AnalysisDecisionOutcome.REUSED,
                        reason_code="fresh_investigation_reused",
                        reason="Existing analysis already covers the current evidence.",
                    )
                )
                continue
            request_ids.append(request.id)
            request_id_by_incident[route.candidate.incident.id] = request.id
            final_routes.append(route)

        decisions = tuple(
            replace(
                self._decision(route, source=source, mode=mode, scan_id=scan_id),
                request_id=request_id_by_incident.get(route.candidate.incident.id),
            )
            for route in final_routes
        )
        async with self._uow_factory() as uow:
            for decision in decisions:
                await uow.analysis_decisions.save(decision)
            await uow.commit()
        return SelectionBatchResult(decisions=decisions, request_ids=tuple(request_ids))

    async def request_manual(
        self,
        incident_id: str,
        *,
        source: str,
        mode: ScanMode,
        requested_by: str | None,
        force: bool = True,
        build_id: str | None = None,
    ) -> Any:
        request = await self._queue.enqueue_incident(
            incident_id,
            source=source,
            mode=mode,
            requested_by=requested_by,
            build_id=build_id,
            force=force,
            budget_kind=InvestigationBudgetKind.MANUAL,
        )
        async with self._uow_factory() as uow:
            incident = await uow.incidents.get(incident_id)
            if incident is None:
                raise LookupError(f"incident {incident_id} does not exist")
            observations = await uow.incidents.observations(incident_id)
            builds = await uow.jenkins.builds_for_incident(incident_id)
            digest = evidence_digest(observations + jenkins_build_observations(builds))
            await uow.analysis_decisions.save(
                AnalysisDecision(
                    id=str(uuid4()),
                    incident_id=incident.id,
                    occurrence_id=incident.current_occurrence.id,
                    outcome=AnalysisDecisionOutcome.SELECTED,
                    reason_code="manual_request",
                    reason="An operator explicitly requested agent investigation.",
                    source=source,
                    mode=mode,
                    priority=100,
                    evidence_hash=digest,
                    request_id=request.id if request else None,
                    created_at=self._now(),
                )
            )
            await uow.commit()
        return request

    async def _load_candidates(
        self,
        incident_ids: tuple[str, ...],
        *,
        priority_by_incident: Mapping[str, int],
        build_id_by_incident: Mapping[str, str],
    ) -> tuple[_Candidate, ...]:
        loaded: list[_Candidate] = []
        async with self._uow_factory() as uow:
            for incident_id in dict.fromkeys(incident_ids):
                incident = await uow.incidents.get(incident_id)
                if incident is None:
                    continue
                stored = await uow.incidents.observations(incident_id)
                builds = await uow.jenkins.builds_for_incident(incident_id)
                observations = stored + jenkins_build_observations(builds)
                inferred_priority = max((int(build.get("priority_score") or 0) for build in builds), default=0)
                loaded.append(
                    _Candidate(
                        incident=incident,
                        evidence_hash=evidence_digest(observations),
                        priority=max(inferred_priority, int(priority_by_incident.get(incident_id, 0))),
                        builds=builds,
                        observations=observations,
                        build_id=build_id_by_incident.get(incident_id),
                    )
                )
        return tuple(loaded)

    async def _deterministic_route(self, candidate: _Candidate, *, mode: ScanMode) -> _Route | None:
        incident = candidate.incident
        if incident.status is IncidentStatus.SUPPRESSED:
            return _route(candidate, AnalysisDecisionOutcome.MANUAL_ONLY, "suppressed", "Suppressed incidents require manual analysis.")
        if incident.status is not IncidentStatus.OPEN:
            return _route(candidate, AnalysisDecisionOutcome.DEFERRED, "not_open", "The incident is no longer open.")
        if not self._automatic_enabled:
            return _route(candidate, AnalysisDecisionOutcome.MANUAL_ONLY, "automation_disabled", "Automatic agent investigation is disabled.")
        async with self._uow_factory() as uow:
            active = await uow.investigation_requests.active_for_incident(incident.id)
            latest = await uow.investigations.latest_for_incident(incident.id)
        if active is not None:
            return _route(candidate, AnalysisDecisionOutcome.REUSED, "already_queued", "An investigation is already queued or running for this incident.")
        if not should_reinvestigate(
            incident=incident,
            latest=latest,
            evidence_hash=candidate.evidence_hash,
            now=self._now(),
        ):
            return _route(candidate, AnalysisDecisionOutcome.REUSED, "unchanged_evidence", "A fresh investigation already explains the unchanged evidence.")
        if candidate.builds and all(bool(build.get("propagated_failure")) for build in candidate.builds):
            return _route(candidate, AnalysisDecisionOutcome.DEFERRED, "propagated_failure", "Only propagated downstream failures were observed; investigate the direct root failure instead.")
        if candidate.builds and all(bool(build.get("recovered")) for build in candidate.builds):
            return _route(candidate, AnalysisDecisionOutcome.DEFERRED, "recovered", "All affected builds have a later successful run.")
        if candidate.builds and all(str(build.get("result")) == "ABORTED" for build in candidate.builds):
            return _route(candidate, AnalysisDecisionOutcome.MANUAL_ONLY, "aborted", "ABORTED builds are not automatically investigated.")
        if incident.severity is Severity.LOW:
            return _route(candidate, AnalysisDecisionOutcome.DEFERRED, "low_severity", "Low-severity incidents do not justify automatic agent cost.")
        if incident.severity is Severity.CRITICAL:
            return _route(candidate, AnalysisDecisionOutcome.SELECTED, "critical_failure", "Critical direct evidence requires agent investigation.")
        if mode is ScanMode.DEEP:
            return _route(candidate, AnalysisDecisionOutcome.SELECTED, "deep_scan", "Deep scan selected this open warning for expanded investigation.")
        direct_new_failure = any(
            str(build.get("result")) == "FAILURE"
            and not build.get("propagated_failure")
            and not build.get("recovered")
            and str(build.get("novelty")) in {"new_failure", "new_regression"}
            for build in candidate.builds
        )
        if direct_new_failure:
            return _route(candidate, AnalysisDecisionOutcome.SELECTED, "new_direct_failure", "A new direct Jenkins failure is likely actionable.")
        return None

    async def _triage(self, batch: list[_Candidate], *, scan_id: str | None) -> list[_Route]:
        try:
            await self._queue.ensure_budget_available(
                budget_kind=InvestigationBudgetKind.AUTOMATIC,
                reserved_tokens=self._triage_token_reservation,
            )
            result = await self._reasoning.triage_batch(
                tuple(TriageCandidate(candidate.incident, candidate.observations) for candidate in batch)
            )
        except DailyLLMBudgetExceeded as exc:
            return [
                _route(
                    candidate,
                    AnalysisDecisionOutcome.BUDGET_DEFERRED,
                    "daily_budget_exhausted",
                    str(exc),
                    metadata=_budget_metadata(exc),
                )
                for candidate in batch
            ]
        except Exception as exc:
            # Only the exception type reaches the decision record, so log the detail here or
            # the reason for deferring a whole batch is lost.
            logger.warning(
                "batch triage failed for %d candidates (scan_id=%s): %s",
                len(batch),
                scan_id,
                exc,
                exc_info=True,
            )
            return [
                _route(
                    candidate,
                    AnalysisDecisionOutcome.DEFERRED,
                    "triage_unavailable",
                    f"Batch triage was unavailable: {type(exc).__name__}.",
                )
                for candidate in batch
            ]
        call_id = result.model_calls[0].id if result.model_calls else None
        calls = tuple(
            replace(
                call,
                budget_kind=InvestigationBudgetKind.AUTOMATIC,
                scan_id=scan_id,
                metadata={**dict(call.metadata), "incident_ids": [candidate.incident.id for candidate in batch]},
            )
            for call in result.model_calls
        )
        if calls:
            async with self._uow_factory() as uow:
                await uow.llm_calls.save_many(calls)
                await uow.commit()
        routes_by_id = {route.incident_id: route for route in result.routes}
        routes: list[_Route] = []
        for candidate in batch:
            triage_route = routes_by_id.get(candidate.incident.id)
            selected = triage_route is not None and triage_route.action == "investigate"
            routes.append(
                _route(
                    candidate,
                    AnalysisDecisionOutcome.SELECTED if selected else AnalysisDecisionOutcome.DEFERRED,
                    "batch_triage_selected" if selected else "batch_triage_deferred",
                    triage_route.reason if triage_route else "Batch triage returned no route.",
                    llm_call_id=call_id,
                )
            )
        return routes

    def _decision(
        self,
        route: _Route,
        *,
        source: str,
        mode: ScanMode,
        scan_id: str | None,
    ) -> AnalysisDecision:
        return AnalysisDecision(
            id=str(uuid4()),
            incident_id=route.candidate.incident.id,
            occurrence_id=route.candidate.incident.current_occurrence.id,
            outcome=route.outcome,
            reason_code=route.reason_code,
            reason=route.reason,
            source=source[:32],
            mode=mode,
            priority=route.candidate.priority,
            evidence_hash=route.candidate.evidence_hash,
            scan_id=scan_id,
            llm_call_id=route.llm_call_id,
            created_at=self._now(),
            metadata={"build_count": len(route.candidate.builds), **dict(route.metadata or {})},
        )


def _route(
    candidate: _Candidate,
    outcome: AnalysisDecisionOutcome,
    reason_code: str,
    reason: str,
    *,
    llm_call_id: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> _Route:
    return _Route(candidate, outcome, reason_code, reason, llm_call_id, metadata)


def _budget_metadata(exc: DailyLLMBudgetExceeded) -> dict[str, Any]:
    return exc.metadata()


def _severity_rank(severity: Severity) -> int:
    return {Severity.LOW: 0, Severity.WARNING: 1, Severity.CRITICAL: 2}[severity]
