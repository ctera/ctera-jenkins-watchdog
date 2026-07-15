"""Idempotent staged scan execution."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime

from jenkins_watchdog.application.automation import AutomationService
from jenkins_watchdog.application.events import EventService
from jenkins_watchdog.application.incidents import IncidentService
from jenkins_watchdog.application.investigations import InvestigationQueueService
from jenkins_watchdog.application.ports import CheckRunner, UnitOfWorkFactory
from jenkins_watchdog.application.reasoning import ReasoningService
from jenkins_watchdog.domain.model import CheckStatus, Scan, ScanStage

TERMINAL_CHECK_STATUSES = frozenset(
    {
        CheckStatus.SUCCEEDED,
        CheckStatus.FAILED,
        CheckStatus.TIMED_OUT,
        CheckStatus.CANCELLED,
        CheckStatus.FILTERED,
    }
)


class ScanCancelled(Exception):
    pass


class ScanPipeline:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        check_runner: CheckRunner,
        incident_service: IncidentService,
        reasoning_service: ReasoningService,
        automation_service: AutomationService,
        events: EventService,
        now: Callable[[], datetime],
        max_investigations: int = 12,
        max_deep_investigations: int = 20,
        token_budget: int = 24000,
        deep_token_budget: int = 40000,
        investigation_queue: InvestigationQueueService | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._check_runner = check_runner
        self._incident_service = incident_service
        self._reasoning_service = reasoning_service
        self._automation_service = automation_service
        self._events = events
        self._now = now
        self._max_investigations = max(0, max_investigations)
        self._max_deep_investigations = max(0, max_deep_investigations)
        self._token_budget = max(0, token_budget)
        self._deep_token_budget = max(0, deep_token_budget)
        self._investigation_queue = investigation_queue

    async def execute(self, scan_id: str) -> Scan:
        scan = await self._get_scan(scan_id)
        await self._raise_if_cancelled(scan)
        selected_checks = frozenset(self._check_runner.checks_for_categories(tuple(scan.categories)))
        successful_checks: set[str] = set()

        scan = await self._advance(scan, ScanStage.DETECTING)
        for check_name in self._check_runner.check_names:
            await self._raise_if_cancelled(await self._get_scan(scan_id))
            if check_name not in selected_checks:
                continue
            async with self._uow_factory() as uow:
                existing = await uow.checks.get(scan_id, check_name)
            if existing is not None and existing.status in TERMINAL_CHECK_STATUSES:
                if existing.status is CheckStatus.SUCCEEDED:
                    successful_checks.add(check_name)
                continue

            await self._events.append(scan_id, "check_started", {"check": check_name}, now=self._now())
            result = await self._check_runner.run(scan_id, check_name, scan.mode)
            if result.status is CheckStatus.SUCCEEDED and scan.categories:
                findings = tuple(item for item in result.findings if item.category in scan.categories)
                result = replace(result, findings=findings)
            async with self._uow_factory() as uow:
                await uow.checks.save(scan_id, result)
                if result.status is CheckStatus.SUCCEEDED:
                    await uow.findings.add_observations(scan_id, result.findings)
                await uow.commit()
            if result.status is CheckStatus.SUCCEEDED:
                successful_checks.add(check_name)
            await self._events.append(
                scan_id,
                "check_completed",
                {
                    "check": check_name,
                    "status": result.status.value,
                    "finding_count": len(result.findings),
                    "failure_summary": result.failure_summary,
                },
                now=self._now(),
            )

        scan = await self._get_scan(scan_id)
        await self._raise_if_cancelled(scan)
        scan = await self._advance(scan, ScanStage.FINDINGS_STORED)
        scan = await self._advance(scan, ScanStage.CORRELATING)
        incident_ids = await self._incident_service.correlate_and_reconcile(
            scan_id=scan_id,
            selected_checks=selected_checks,
            successful_checks=frozenset(successful_checks),
            now=self._now(),
        )
        if scan.mode.value == "deep":
            async with self._uow_factory() as uow:
                active_incident_ids = frozenset(item.id for item in await uow.incidents.active())
            incident_ids = incident_ids | active_incident_ids
        await self._events.append(
            scan_id,
            "correlation_completed",
            {"incident_ids": sorted(incident_ids), "incident_count": len(incident_ids)},
            now=self._now(),
        )
        scan = await self._advance(await self._get_scan(scan_id), ScanStage.RECONCILING)
        await self._raise_if_cancelled(scan)
        scan = await self._advance(scan, ScanStage.INVESTIGATING)
        ranked_incidents = await self._rank_incidents(incident_ids)
        limit = self._max_deep_investigations if scan.mode.value == "deep" else self._max_investigations
        token_budget = self._deep_token_budget if scan.mode.value == "deep" else self._token_budget
        candidates: list[str] = []
        needs_investigation = getattr(self._reasoning_service, "needs_investigation", None)
        for incident_id in ranked_incidents:
            if needs_investigation is None or await needs_investigation(incident_id):
                candidates.append(incident_id)

        completed_investigations = 0
        used_tokens = 0
        queued_incidents: set[str] = set()
        if self._investigation_queue is not None:
            for position, incident_id in enumerate(candidates):
                await self._raise_if_cancelled(await self._get_scan(scan_id))
                request = await self._investigation_queue.enqueue_incident(
                    incident_id,
                    source="deep_scan" if scan.mode.value == "deep" else "scan",
                    mode=scan.mode,
                    priority=max(1, 100 - position),
                    scan_id=scan_id,
                )
                if request is None:
                    continue
                queued_incidents.add(incident_id)
                await self._events.append(
                    scan_id,
                    "investigation_queued",
                    {
                        "incident_id": incident_id,
                        "request_id": request.id,
                        "status": request.status.value,
                        "mode": request.mode.value,
                    },
                    now=self._now(),
                )
        else:
            for incident_id in candidates[:limit]:
                await self._raise_if_cancelled(await self._get_scan(scan_id))
                investigation = await self._reasoning_service.investigate_if_needed(incident_id)
                if investigation is not None:
                    completed_investigations += 1
                    total_tokens = investigation.usage.get("total_tokens")
                    if isinstance(total_tokens, int):
                        used_tokens += total_tokens
                    await self._events.append(
                        scan_id,
                        "investigation_completed",
                        {
                            "incident_id": incident_id,
                            "status": investigation.status.value,
                            "confidence": investigation.confidence.value if investigation.confidence else None,
                        },
                        now=self._now(),
                    )
                    if token_budget and used_tokens >= token_budget:
                        break
        await self._events.append(
            scan_id,
            "investigation_budget_applied",
            {
                "candidate_count": len(candidates),
                "completed_count": completed_investigations,
                "queued_count": len(queued_incidents),
                "skipped_count": (
                    0
                    if self._investigation_queue is not None
                    else max(0, len(candidates) - completed_investigations)
                ),
                "max_investigations": None if self._investigation_queue is not None else limit,
                "token_budget": token_budget,
                "used_tokens": used_tokens,
            },
            now=self._now(),
        )
        scan = await self._advance(scan, ScanStage.PLANNING_ACTIONS)
        for incident_id in sorted(incident_ids):
            await self._raise_if_cancelled(await self._get_scan(scan_id))
            if incident_id in queued_incidents:
                continue
            actions = await self._automation_service.plan(incident_id)
            if actions:
                await self._events.append(
                    scan_id,
                    "actions_planned",
                    {"incident_id": incident_id, "action_ids": [action.id for action in actions]},
                    now=self._now(),
                )
        scan = await self._get_scan(scan_id)
        await self._raise_if_cancelled(scan)

        now = self._now()
        completed = scan.succeed(now=now)
        async with self._uow_factory() as uow:
            await uow.scans.save(completed)
            await uow.commit()
        await self._events.append(
            scan_id,
            "scan_completed",
            {"status": completed.status.value, "incident_count": len(incident_ids)},
            now=now,
        )
        return completed

    async def _advance(self, scan: Scan, stage: ScanStage) -> Scan:
        advanced = scan.advance(stage, now=self._now())
        if advanced == scan:
            return scan
        async with self._uow_factory() as uow:
            await uow.scans.save(advanced)
            await uow.commit()
        await self._events.append(scan.id, "scan_stage_changed", {"stage": stage.value}, now=self._now())
        return advanced

    async def _get_scan(self, scan_id: str) -> Scan:
        async with self._uow_factory() as uow:
            scan = await uow.scans.get(scan_id)
        if scan is None:
            raise LookupError(f"scan {scan_id} does not exist")
        return scan

    async def _rank_incidents(self, incident_ids: frozenset[str]) -> tuple[str, ...]:
        ranked: list[tuple[int, int, datetime, str]] = []
        severity_rank = {"critical": 2, "warning": 1, "low": 0}
        async with self._uow_factory() as uow:
            for incident_id in incident_ids:
                incident = await uow.incidents.get(incident_id)
                if incident is None:
                    continue
                ranked.append(
                    (
                        severity_rank[incident.severity.value],
                        len(incident.current_occurrence.observation_identities),
                        incident.updated_at or incident.created_at,
                        incident_id,
                    )
                )
        ranked.sort(reverse=True)
        return tuple(item[3] for item in ranked)

    async def _raise_if_cancelled(self, scan: Scan) -> None:
        if not scan.cancellation_requested:
            return
        now = self._now()
        cancelled = scan.cancel(now=now)
        async with self._uow_factory() as uow:
            await uow.scans.save(cancelled)
            await uow.commit()
        await self._events.append(scan.id, "scan_cancelled", {}, now=now)
        raise ScanCancelled(scan.id)
