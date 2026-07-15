"""Idempotent staged scan execution."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime

from jenkins_watchdog.application.automation import AutomationService
from jenkins_watchdog.application.events import EventService
from jenkins_watchdog.application.incidents import IncidentService
from jenkins_watchdog.application.ports import CheckRunner, UnitOfWorkFactory
from jenkins_watchdog.application.selection import AnalysisSelectionService
from jenkins_watchdog.domain.model import AnalysisDecisionOutcome, CheckStatus, Scan, ScanStage

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
        selection_service: AnalysisSelectionService,
        automation_service: AutomationService,
        events: EventService,
        now: Callable[[], datetime],
        max_investigations: int = 12,
        max_deep_investigations: int = 20,
    ) -> None:
        self._uow_factory = uow_factory
        self._check_runner = check_runner
        self._incident_service = incident_service
        self._selection_service = selection_service
        self._automation_service = automation_service
        self._events = events
        self._now = now
        self._max_investigations = max(0, max_investigations)
        self._max_deep_investigations = max(0, max_deep_investigations)

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
        await self._raise_if_cancelled(await self._get_scan(scan_id))
        selection = await self._selection_service.select(
            ranked_incidents,
            source="deep_scan" if scan.mode.value == "deep" else "scan",
            mode=scan.mode,
            limit=limit,
            scan_id=scan_id,
            priority_by_incident={incident_id: max(1, 100 - position) for position, incident_id in enumerate(ranked_incidents)},
        )
        queued_incidents = {
            decision.incident_id
            for decision in selection.decisions
            if decision.outcome is AnalysisDecisionOutcome.SELECTED and decision.request_id
        }
        for decision in selection.decisions:
            await self._events.append(
                scan_id,
                "analysis_selection_decided",
                {
                    "incident_id": decision.incident_id,
                    "outcome": decision.outcome.value,
                    "reason_code": decision.reason_code,
                    "reason": decision.reason,
                    "request_id": decision.request_id,
                },
                now=self._now(),
            )
        await self._events.append(
            scan_id,
            "investigation_budget_applied",
            {
                "candidate_count": len(ranked_incidents),
                "completed_count": 0,
                "queued_count": len(queued_incidents),
                "skipped_count": len(selection.decisions) - len(queued_incidents),
                "max_investigations": limit,
                "budget_deferred_count": sum(
                    decision.outcome is AnalysisDecisionOutcome.BUDGET_DEFERRED
                    for decision in selection.decisions
                ),
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
