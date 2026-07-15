"""Incremental, durable Jenkins catalog and build ingestion."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import TypeVar

from jenkins_watchdog.application.incidents import IncidentService
from jenkins_watchdog.application.ports import JenkinsSourcePort, UnitOfWorkFactory
from jenkins_watchdog.application.selection import AnalysisSelectionService
from jenkins_watchdog.domain.jenkins import (
    JenkinsBuildEnrichment,
    JenkinsBuildHistoryPage,
    JenkinsBuildSnapshot,
    JenkinsCoverage,
    JenkinsJobSnapshot,
    JenkinsSyncStats,
)
from jenkins_watchdog.domain.model import ScanMode

logger = logging.getLogger(__name__)
T = TypeVar("T")
R = TypeVar("R")


class JenkinsMonitorService:
    def __init__(
        self,
        *,
        source: JenkinsSourcePort,
        uow_factory: UnitOfWorkFactory,
        now: Callable[[], datetime],
        window_hours: int = 168,
        fetch_concurrency: int = 10,
        enrichment_limit: int = 250,
        log_enrichment_limit: int = 30,
        lease_seconds: int = 900,
        heartbeat_seconds: int = 60,
        incident_service: IncidentService | None = None,
        selection_service: AnalysisSelectionService | None = None,
        automatic_investigations: bool = True,
        minimum_investigation_priority: int = 1,
        analysis_candidate_limit: int = 250,
        automatic_selection_limit: int = 12,
    ) -> None:
        self._source = source
        self._uow_factory = uow_factory
        self._now = now
        self._window_hours = max(1, window_hours)
        self._fetch_concurrency = max(1, fetch_concurrency)
        self._enrichment_limit = max(1, enrichment_limit)
        self._log_enrichment_limit = max(0, log_enrichment_limit)
        self._lease_seconds = max(60, lease_seconds)
        self._heartbeat_seconds = max(15, heartbeat_seconds)
        self._incident_service = incident_service
        self._selection_service = selection_service
        self._automatic_investigations = automatic_investigations
        self._minimum_investigation_priority = max(0, minimum_investigation_priority)
        self._analysis_candidate_limit = max(1, analysis_candidate_limit)
        self._automatic_selection_limit = max(0, automatic_selection_limit)

    async def sync(self, *, owner: str, window_hours: int | None = None) -> JenkinsSyncStats | None:
        started_at = self._now()
        window = max(1, window_hours or self._window_hours)
        cutoff = started_at - timedelta(hours=window)
        async with self._uow_factory() as uow:
            claimed = await uow.jenkins.claim_sync(
                owner=owner,
                now=started_at,
                lease_seconds=self._lease_seconds,
            )
            await uow.commit()
        if not claimed:
            return None

        heartbeat = asyncio.create_task(self._heartbeat(owner))
        try:
            stats = await self._sync_claimed(owner=owner, started_at=started_at, cutoff=cutoff)
            async with self._uow_factory() as uow:
                await uow.jenkins.complete_sync(owner=owner, stats=stats)
                await uow.commit()
            return stats
        except asyncio.CancelledError:
            now = self._now()
            async with self._uow_factory() as uow:
                await uow.jenkins.fail_sync(
                    owner=owner,
                    now=now,
                    summary="Jenkins synchronization cancelled",
                )
                await uow.commit()
            raise
        except Exception as exc:
            logger.exception("Jenkins synchronization failed")
            now = self._now()
            async with self._uow_factory() as uow:
                await uow.jenkins.fail_sync(
                    owner=owner,
                    now=now,
                    summary=f"{type(exc).__name__}: {exc}",
                )
                await uow.commit()
            raise
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _sync_claimed(
        self,
        *,
        owner: str,
        started_at: datetime,
        cutoff: datetime,
    ) -> JenkinsSyncStats:
        jobs = await self._source.discover_jobs()
        now = self._now()
        async with self._uow_factory() as uow:
            await uow.jenkins.upsert_jobs(jobs, now=now)
            watermarks = await uow.jenkins.watermarks(tuple(job.full_name for job in jobs))
            running_builds = await uow.jenkins.running_build_numbers()
            await uow.commit()

        active_jobs: list[JenkinsJobSnapshot] = []
        for job in jobs:
            if job.is_container or job.last_build_number is None:
                continue
            watermark = watermarks.get(job.full_name)
            first_observation = watermark is None and bool(job.last_build_at and job.last_build_at >= cutoff)
            advanced = watermark is not None and job.last_build_number > watermark
            if first_observation or advanced or job.full_name in running_builds:
                active_jobs.append(job)
        active = tuple(active_jobs)
        parent_classes = {job.full_name: job.job_class for job in jobs}
        source_candidates = tuple(
            job
            for job in jobs
            if not job.is_container
            and job.last_build_at is not None
            and job.last_build_at >= cutoff
            and job.parent_full_name is not None
            and parent_classes.get(job.parent_full_name, "").endswith("WorkflowMultiBranchProject")
        )
        enriched_jobs, source_errors = await self._map_bounded(
            source_candidates,
            self._source.enrich_job_source,
            label="job source",
        )
        if enriched_jobs:
            async with self._uow_factory() as uow:
                await uow.jenkins.upsert_jobs(tuple(enriched_jobs), now=self._now())
                await uow.commit()

        semaphore = asyncio.Semaphore(self._fetch_concurrency)

        async def fetch_history(job: JenkinsJobSnapshot) -> tuple[JenkinsJobSnapshot, JenkinsBuildHistoryPage]:
            after_number = watermarks.get(job.full_name)
            running_numbers = running_builds.get(job.full_name, ())
            if running_numbers:
                oldest_running = min(running_numbers)
                after_number = min(after_number, oldest_running) - 1 if after_number is not None else oldest_running - 1
            async with semaphore:
                return job, await self._source.build_history(
                    job,
                    cutoff=cutoff,
                    after_number=after_number,
                )

        history_results = await asyncio.gather(*(fetch_history(job) for job in active), return_exceptions=True)
        errors = list(source_errors)
        histories: list[tuple[JenkinsJobSnapshot, JenkinsBuildHistoryPage]] = []
        for job, history_result in zip(active, history_results, strict=True):
            if isinstance(history_result, BaseException):
                errors.append(f"history {job.full_name}: {type(history_result).__name__}")
            else:
                histories.append(history_result)
        observed_builds = tuple(build for _, history in histories for build in history.builds)
        exact_jobs = sum(
            history.coverage in {JenkinsCoverage.EXACT, JenkinsCoverage.JOB_STARTED_IN_WINDOW}
            for _, history in histories
        )
        retention_limited = sum(history.coverage is JenkinsCoverage.RETENTION_LIMITED for _, history in histories)
        async with self._uow_factory() as uow:
            new_builds = await uow.jenkins.upsert_builds(observed_builds, now=self._now())
            for job, history in histories:
                await uow.jenkins.set_job_coverage(job.full_name, history.coverage.value, now=self._now())
            pending = await uow.jenkins.pending_enrichment(
                limit=self._enrichment_limit,
                log_limit=self._log_enrichment_limit,
            )
            await uow.commit()

        async def enrich(index_and_build: tuple[int, JenkinsBuildSnapshot]) -> JenkinsBuildEnrichment:
            index, build = index_and_build
            async with semaphore:
                return await self._source.enrich_build(
                    build,
                    include_log=(
                        build.enrichment_status == "log_pending"
                        or index < self._log_enrichment_limit
                    ),
                )

        enrichment_results = await asyncio.gather(
            *(enrich(item) for item in enumerate(pending)),
            return_exceptions=True,
        )
        enriched_count = 0
        async with self._uow_factory() as uow:
            for build, enrichment_result in zip(pending, enrichment_results, strict=True):
                if isinstance(enrichment_result, BaseException):
                    errors.append(
                        f"enrichment {build.job_full_name} #{build.number}: {type(enrichment_result).__name__}"
                    )
                    await uow.jenkins.mark_enrichment_failed(
                        build.job_full_name,
                        build.number,
                        now=self._now(),
                        summary=f"{type(enrichment_result).__name__}: {enrichment_result}",
                    )
                    continue
                await uow.jenkins.save_enrichment(enrichment_result, now=self._now())
                enriched_count += 1
            await uow.jenkins.refresh_classifications(now=self._now())
            await uow.commit()

        analyzed_candidates, queued_investigations = await self._correlate_and_queue_builds()

        completed_at = self._now()
        batch_limit = self._enrichment_limit
        if pending and pending[0].enrichment_status == "log_pending":
            batch_limit = self._log_enrichment_limit
        return JenkinsSyncStats(
            started_at=started_at,
            completed_at=completed_at,
            cutoff_at=cutoff,
            jobs_discovered=len(jobs),
            active_jobs=len(active),
            builds_observed=len(observed_builds),
            new_builds=new_builds,
            enriched_builds=enriched_count,
            exact_jobs=exact_jobs,
            retention_limited_jobs=retention_limited,
            errors=tuple(errors[:100]),
            details={
                "source_metadata_jobs": len(enriched_jobs),
                "pending_enrichment_remaining_is_bounded": bool(pending) and len(pending) == batch_limit,
                "owner": owner,
                "analysis_candidates_processed": analyzed_candidates,
                "investigations_queued": queued_investigations,
                "analysis_backlog_remaining_is_bounded": analyzed_candidates == self._analysis_candidate_limit,
            },
        )

    async def _correlate_and_queue_builds(self) -> tuple[int, int]:
        if self._incident_service is None:
            return 0, 0
        minimum = self._minimum_investigation_priority if self._automatic_investigations else 0
        async with self._uow_factory() as uow:
            candidates = await uow.jenkins.analysis_candidates(
                min_priority=minimum,
                limit=self._analysis_candidate_limit,
            )
        incident_ids: list[str] = []
        priorities: dict[str, int] = {}
        build_ids: dict[str, str] = {}
        for build in candidates:
            incident = await self._incident_service.correlate_jenkins_build(build, now=self._now())
            incident_ids.append(incident.id)
            priority = int(build.get("priority_score") or 0)
            if priority >= priorities.get(incident.id, -1):
                priorities[incident.id] = priority
                build_ids[incident.id] = str(build["id"])
        if self._selection_service is None or not incident_ids:
            return len(candidates), 0
        selection = await self._selection_service.select(
            tuple(dict.fromkeys(incident_ids)),
            source="jenkins_monitor",
            mode=ScanMode.REGULAR,
            limit=self._automatic_selection_limit,
            priority_by_incident=priorities,
            build_id_by_incident=build_ids,
        )
        return len(candidates), selection.selected_count

    async def _map_bounded(
        self,
        values: tuple[T, ...],
        operation: Callable[[T], Awaitable[R]],
        *,
        label: str,
    ) -> tuple[list[R], list[str]]:
        semaphore = asyncio.Semaphore(self._fetch_concurrency)

        async def invoke(value: T) -> R:
            async with semaphore:
                return await operation(value)

        results = await asyncio.gather(*(invoke(value) for value in values), return_exceptions=True)
        completed: list[R] = []
        errors: list[str] = []
        for value, result in zip(values, results, strict=True):
            if isinstance(result, BaseException):
                name = getattr(value, "full_name", str(value))
                errors.append(f"{label} {name}: {type(result).__name__}")
            else:
                completed.append(result)
        return completed, errors

    async def _heartbeat(self, owner: str) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_seconds)
            async with self._uow_factory() as uow:
                owned = await uow.jenkins.heartbeat_sync(
                    owner=owner,
                    now=self._now(),
                    lease_seconds=self._lease_seconds,
                )
                await uow.commit()
            if not owned:
                raise RuntimeError("lost Jenkins synchronization lease")


class JenkinsMonitorWorker:
    def __init__(
        self,
        *,
        owner: str,
        monitor: JenkinsMonitorService,
        interval_seconds: float = 300,
    ) -> None:
        self.owner = owner
        self._monitor = monitor
        self._interval_seconds = max(15.0, interval_seconds)

    async def run_once(self) -> JenkinsSyncStats | None:
        return await self._monitor.sync(owner=self.owner)

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        while not stop.is_set():
            delay = self._interval_seconds
            try:
                stats = await self.run_once()
                if stats is not None:
                    logger.info(
                        "Jenkins sync completed: %s jobs, %s observed builds, %s new builds",
                        stats.jobs_discovered,
                        stats.builds_observed,
                        stats.new_builds,
                    )
                    if stats.details.get("pending_enrichment_remaining_is_bounded"):
                        delay = min(15.0, self._interval_seconds)
                    if stats.details.get("analysis_backlog_remaining_is_bounded"):
                        delay = min(15.0, self._interval_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Jenkins monitor iteration failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                pass
