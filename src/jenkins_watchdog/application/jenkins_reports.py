"""Fixed-window, agent-only Jenkins failure report workflow."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from jenkins_watchdog.application.investigations import InvestigationQueueService
from jenkins_watchdog.application.ports import JenkinsSourcePort, UnitOfWorkFactory
from jenkins_watchdog.domain.jenkins import JenkinsCoverage, JenkinsJobSnapshot
from jenkins_watchdog.domain.model import FindingObservation, Incident, InvestigationBudgetKind, ScanMode, Severity

_FAILED = frozenset({"FAILURE", "UNSTABLE", "ABORTED"})


class JenkinsFailureReportService:
    def __init__(self, *, source: JenkinsSourcePort, uow_factory: UnitOfWorkFactory, queue: InvestigationQueueService, now: Callable[[], datetime], concurrency: int = 10) -> None:
        self._source, self._uow_factory, self._queue, self._now = source, uow_factory, queue, now
        self._concurrency = max(1, concurrency)

    async def create(self, *, mode: ScanMode, scan_id: str | None = None) -> dict[str, Any]:
        end = self._now()
        start = end - timedelta(hours=24 if mode is ScanMode.DEEP else 4)
        async with self._uow_factory() as uow:
            report = await uow.jenkins_reports.create(
                mode=mode.value,
                start=start,
                end=end,
                now=end,
                scan_id=scan_id,
            )
            await uow.commit()
        try:
            await self._collect(report["id"], cutoff=start, end=end, mode=mode, scan_id=scan_id)
        except Exception as exc:
            async with self._uow_factory() as uow:
                await uow.jenkins_reports.fail(report["id"], summary=f"{type(exc).__name__}: {exc}", now=self._now())
                await uow.commit()
        return (await self.detail(report["id"], limit=50, offset=0)) or report

    async def ensure_for_scan(self, scan_id: str, *, mode: ScanMode) -> dict[str, Any]:
        async with self._uow_factory() as uow:
            existing = await uow.jenkins_reports.for_scan(scan_id)
        if existing is not None:
            return (await self.detail(existing["id"], limit=50, offset=0)) or existing
        return await self.create(mode=mode, scan_id=scan_id)

    async def detail_for_scan(
        self,
        scan_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
        status: str | None = None,
        job: str | None = None,
    ) -> dict[str, Any] | None:
        async with self._uow_factory() as uow:
            report = await uow.jenkins_reports.for_scan(scan_id)
        if report is None:
            return None
        return await self.detail(report["id"], limit=limit, offset=offset, status=status, job=job)

    async def list(self, *, limit: int = 25) -> list[dict[str, Any]]:
        async with self._uow_factory() as uow:
            return await uow.jenkins_reports.list(limit=limit)

    async def summaries_for_scans(self, scan_ids: tuple[str, ...]) -> dict[str, dict[str, Any]]:
        summaries: dict[str, dict[str, Any]] = {}
        async with self._uow_factory() as uow:
            for scan_id in scan_ids:
                report = await uow.jenkins_reports.for_scan(scan_id)
                if report is None:
                    continue
                report = await uow.jenkins_reports.refresh(report["id"], now=self._now())
                if report is None:
                    continue
                counts = await uow.jenkins_reports.status_counts(report["id"])
                summaries[scan_id] = report | {
                    "total_builds": report["failures_found"],
                    "counts": counts,
                }
            await uow.commit()
        return summaries

    async def detail(self, report_id: str, *, limit: int, offset: int, status: str | None = None, job: str | None = None) -> dict[str, Any] | None:
        async with self._uow_factory() as uow:
            report = await uow.jenkins_reports.refresh(report_id, now=self._now())
            if report is None:
                return None
            items, total = await uow.jenkins_reports.page(report_id, limit=limit, offset=offset, status=status, job=job)
            counts = await uow.jenkins_reports.status_counts(report_id)
            await uow.commit()
        return report | {"builds": items, "total_builds": total, "offset": offset, "limit": limit, "counts": counts}

    async def _collect(
        self,
        report_id: str,
        *,
        cutoff: datetime,
        end: datetime,
        mode: ScanMode,
        scan_id: str | None,
    ) -> None:
        jobs = await self._source.discover_jobs()
        leaves = tuple(job for job in jobs if not job.is_container)
        now = self._now()
        async with self._uow_factory() as uow:
            await uow.jenkins.upsert_jobs(jobs, now=now)
            await uow.commit()
        semaphore = asyncio.Semaphore(self._concurrency)
        async def history(job: JenkinsJobSnapshot):
            async with semaphore:
                return job, await self._source.build_history(job, cutoff=cutoff, after_number=None)
        results = await asyncio.gather(*(history(job) for job in leaves), return_exceptions=True)
        coverage: list[dict[str, Any]] = []
        failed = []
        async with self._uow_factory() as uow:
            for job, result in zip(leaves, results, strict=True):
                if isinstance(result, BaseException):
                    coverage.append({"job_name": job.full_name, "kind": "inaccessible", "error": f"{type(result).__name__}: {result}"})
                    continue
                _, page = result
                await uow.jenkins.upsert_builds(page.builds, now=now)
                await uow.jenkins.set_job_coverage(job.full_name, page.coverage.value, now=now)
                if page.coverage in {JenkinsCoverage.RETENTION_LIMITED, JenkinsCoverage.UNKNOWN}:
                    coverage.append({"job_name": job.full_name, "kind": page.coverage.value})
                failed.extend(
                    build for build in page.builds
                    if (
                        build.result in _FAILED
                        and not build.building
                        and cutoff <= build.started_at
                        and build.started_at + timedelta(milliseconds=build.duration_ms) <= end
                    )
                )
            await uow.commit()
        rows: list[tuple[str, str]] = []
        async with self._uow_factory() as uow:
            seen: set[tuple[str, int]] = set()
            for build in failed:
                key = build.job_full_name, build.number
                if key in seen:
                    continue
                seen.add(key)
                persisted = await uow.jenkins.build_by_job_number(*key)
                if persisted:
                    row = await uow.jenkins_reports.add_build(report_id=report_id, build_id=persisted["id"], now=now)
                    rows.append((row["id"], persisted["id"]))
            await uow.jenkins_reports.set_collected(report_id, jobs_discovered=len(leaves), failures_found=len(rows), coverage_exceptions=coverage, now=now)
            await uow.commit()
        for row_id, build_id in rows:
            await self._enqueue_build(
                report_id=report_id,
                row_id=row_id,
                build_id=build_id,
                mode=mode,
                scan_id=scan_id,
            )

    async def _enqueue_build(
        self,
        *,
        report_id: str,
        row_id: str,
        build_id: str,
        mode: ScanMode,
        scan_id: str | None,
    ) -> None:
        now = self._now()
        async with self._uow_factory() as uow:
            build = await uow.jenkins.build_detail(build_id)
            if build is None:
                return
            observation = FindingObservation(scan_id=scan_id or f"jenkins-report:{report_id}", check_name="jenkins_failure_report", rule_id="jenkins.failure.report.v1", resource_id=f"{build['job_name']}#{build['build_number']}", severity=Severity.CRITICAL if build["result"] == "FAILURE" else Severity.WARNING, category="jenkins_build", summary=f"Jenkins build {build['job_name']} #{build['build_number']} finished {build['result']}", observed_at=build["started_at"], evidence={"build_id": build_id, "result": build["result"], "url": build["url"]})
            incident = Incident.open_new(id=str(uuid4()), correlation_rule_id="jenkins_failure_report_build", correlation_key=f"{report_id}:{build_id}", observation=observation, opened_at=now, title=observation.summary)
            await uow.incidents.save(incident)
            await uow.commit()
        request = await self._queue.enqueue_incident(incident.id, source="jenkins_report", mode=mode, priority=100, scan_id=scan_id, build_id=build_id, force=True, budget_kind=InvestigationBudgetKind.AUTOMATIC, defer_budget=True)
        if request:
            async with self._uow_factory() as uow:
                await uow.jenkins_reports.attach_request(row_id, request.id, now=self._now())
                await uow.commit()
