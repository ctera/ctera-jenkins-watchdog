from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jenkins_watchdog.application import jenkins_monitor as monitor_module
from jenkins_watchdog.application.incidents import IncidentService
from jenkins_watchdog.application.investigations import InvestigationQueueService
from jenkins_watchdog.application.jenkins_monitor import JenkinsMonitorService, JenkinsMonitorWorker
from jenkins_watchdog.application.selection import AnalysisSelectionService
from jenkins_watchdog.application.types import TriageBatchResult, TriageRoute
from jenkins_watchdog.domain.jenkins import (
    JenkinsBuildEnrichment,
    JenkinsBuildHistoryPage,
    JenkinsBuildSnapshot,
    JenkinsCoverage,
    JenkinsHeadType,
    JenkinsJobSnapshot,
)
from jenkins_watchdog.infrastructure.uow import SqlAlchemyUnitOfWork, SqlAlchemyUnitOfWorkFactory

NOW = datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)


def _uow_factory(factory: async_sessionmaker[AsyncSession]) -> SqlAlchemyUnitOfWorkFactory:
    return SqlAlchemyUnitOfWorkFactory(factory)


class MonitorSource:
    def __init__(self, *, enrichment_error: bool = False, discovery_error: bool = False) -> None:
        self.enrichment_error = enrichment_error
        self.discovery_error = discovery_error
        self.history_calls: list[tuple[str, int | None]] = []
        self.enrichment_calls: list[tuple[str, int, bool]] = []
        self.parent = JenkinsJobSnapshot(
            full_name="portal",
            display_name="portal",
            url="https://jenkins/job/portal/",
            job_class="org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject",
            color="red",
            parent_full_name=None,
        )
        self.child = JenkinsJobSnapshot(
            full_name="portal/MR-42",
            display_name="MR-42",
            url="https://jenkins/job/portal/job/MR-42/",
            job_class="org.jenkinsci.plugins.workflow.job.WorkflowJob",
            color="red",
            parent_full_name="portal",
            first_build_number=1,
            first_build_at=NOW - timedelta(days=2),
            last_build_number=2,
            last_build_at=NOW,
        )
        self.broken = JenkinsJobSnapshot(
            full_name="broken-history",
            display_name="broken-history",
            url="https://jenkins/job/broken-history/",
            job_class="org.jenkinsci.plugins.workflow.job.WorkflowJob",
            color="red",
            parent_full_name=None,
            first_build_number=1,
            last_build_number=1,
            last_build_at=NOW,
        )
        self.stale = JenkinsJobSnapshot(
            full_name="stale",
            display_name="stale",
            url="https://jenkins/job/stale/",
            job_class="hudson.model.FreeStyleProject",
            color="blue",
            parent_full_name=None,
            first_build_number=1,
            last_build_number=10,
            last_build_at=NOW - timedelta(days=30),
        )

    async def discover_jobs(self) -> tuple[JenkinsJobSnapshot, ...]:
        if self.discovery_error:
            raise RuntimeError("catalog unavailable")
        return self.parent, self.child, self.broken, self.stale

    async def enrich_job_source(self, job: JenkinsJobSnapshot) -> JenkinsJobSnapshot:
        return replace(
            job,
            head_type=JenkinsHeadType.CHANGE_REQUEST,
            head_name="MR-42",
            source_provider="gitlab",
            repository="ctera/portal",
            source_url="https://gitlab.example/ctera/portal/merge_requests/42",
        )

    async def build_history(
        self,
        job: JenkinsJobSnapshot,
        *,
        cutoff: datetime,
        after_number: int | None,
    ) -> JenkinsBuildHistoryPage:
        del cutoff
        self.history_calls.append((job.full_name, after_number))
        if job.full_name == "broken-history":
            raise TimeoutError("history timeout")
        return JenkinsBuildHistoryPage(
            builds=(
                JenkinsBuildSnapshot(
                    job_full_name=job.full_name,
                    number=1,
                    result="SUCCESS",
                    url=f"{job.url}1/",
                    started_at=NOW - timedelta(hours=2),
                    duration_ms=60_000,
                ),
                JenkinsBuildSnapshot(
                    job_full_name=job.full_name,
                    number=2,
                    result="FAILURE",
                    url=f"{job.url}2/",
                    started_at=NOW - timedelta(hours=1),
                    duration_ms=180_000,
                ),
            ),
            coverage=JenkinsCoverage.EXACT,
        )

    async def enrich_build(
        self,
        build: JenkinsBuildSnapshot,
        *,
        include_log: bool,
    ) -> JenkinsBuildEnrichment:
        self.enrichment_calls.append((build.job_full_name, build.number, include_log))
        if self.enrichment_error:
            raise RuntimeError("console unavailable")
        return JenkinsBuildEnrichment(
            job_full_name=build.job_full_name,
            number=build.number,
            trigger_kind="gitlab_webhook",
            source_provider="gitlab",
            repository="ctera/portal",
            change_number="42",
            change_url="https://gitlab.example/ctera/portal/merge_requests/42",
            head_name="MR-42",
            failed_stage="Compile",
            failure_classification="compilation_error",
            failure_signature="compiler-signature",
            failure_summary="TypeScript compilation failed",
            error_lines=("error TS2322",),
            stage_evidence=({"name": "Compile", "status": "FAILED", "duration_ms": 10},),
            log_enriched=True,
        )


def _service(
    factory: async_sessionmaker[AsyncSession],
    source: MonitorSource,
    *,
    automatic: bool = True,
    minimum_priority: int = 1,
) -> JenkinsMonitorService:
    uow_factory = _uow_factory(factory)
    queue = InvestigationQueueService(uow_factory=uow_factory, now=lambda: NOW)

    class Triage:
        async def triage_batch(self, candidates):
            return TriageBatchResult(
                routes=tuple(
                    TriageRoute(candidate.incident.id, "defer", "test triage")
                    for candidate in candidates
                )
            )

    selection = AnalysisSelectionService(
        uow_factory=uow_factory,
        reasoning=Triage(),
        queue=queue,
        now=lambda: NOW,
        automatic_enabled=automatic,
    )
    return JenkinsMonitorService(
        source=source,
        uow_factory=uow_factory,
        now=lambda: NOW,
        window_hours=168,
        fetch_concurrency=2,
        enrichment_limit=10,
        log_enrichment_limit=5,
        lease_seconds=60,
        heartbeat_seconds=15,
        incident_service=IncidentService(uow_factory),
        selection_service=selection,
        automatic_investigations=automatic,
        minimum_investigation_priority=minimum_priority,
        analysis_candidate_limit=10,
    )


@pytest.mark.asyncio
async def test_sync_indexes_all_jobs_and_links_one_actionable_failure_to_agent_queue(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    source = MonitorSource()
    service = _service(postgres_session_factory, source)

    stats = await service.sync(owner="monitor-a")
    assert stats is not None
    assert stats.jobs_discovered == 4
    assert stats.active_jobs == 2
    assert stats.builds_observed == 2
    assert stats.new_builds == 2
    assert stats.enriched_builds == 1
    assert stats.exact_jobs == 1
    assert stats.retention_limited_jobs == 0
    assert stats.details["source_metadata_jobs"] == 1
    assert stats.details["analysis_candidates_processed"] == 1
    assert stats.details["investigations_queued"] == 1
    assert stats.errors == ("history broken-history: TimeoutError",)
    assert source.enrichment_calls == [("portal/MR-42", 2, True)]

    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        status = await uow.jenkins.sync_status()
        summary = await uow.jenkins.jenkins_summary(since=NOW - timedelta(days=7))
        failures = await uow.jenkins.failure_builds(since=NOW - timedelta(days=7), limit=10)
        active_incidents = await uow.incidents.active()
        request = await uow.investigation_requests.latest_for_incident(active_incidents[0].id)
    assert status["status"] == "partial"
    assert summary["job_count"] == 4
    assert summary["build_count"] == 2
    assert summary["failure_build_count"] == 1
    assert request is not None
    assert len(active_incidents) == 1
    assert failures.items[0]["incident_id"] == active_incidents[0].id
    assert failures.items[0]["failure_summary"] == "TypeScript compilation failed"

    second = await service.sync(owner="monitor-a")
    assert second is not None
    assert second.builds_observed == 0
    assert second.new_builds == 0
    assert second.details["investigations_queued"] == 0


@pytest.mark.asyncio
async def test_sync_lease_prevents_overlapping_catalog_runs(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    factory = _uow_factory(postgres_session_factory)
    async with factory() as uow:
        assert await uow.jenkins.claim_sync(owner="other", now=NOW, lease_seconds=300)
        await uow.commit()
    assert await _service(postgres_session_factory, MonitorSource()).sync(owner="monitor-a") is None


@pytest.mark.asyncio
async def test_sync_honors_configured_automatic_investigation_priority_floor(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = _service(
        postgres_session_factory,
        MonitorSource(),
        minimum_priority=100,
    )

    stats = await service.sync(owner="monitor-priority-floor")

    assert stats is not None
    assert stats.details["analysis_candidates_processed"] == 0
    assert stats.details["investigations_queued"] == 0
    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        assert await uow.incidents.active() == ()


@pytest.mark.asyncio
async def test_sync_records_fatal_discovery_failure(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = _service(postgres_session_factory, MonitorSource(discovery_error=True))
    with pytest.raises(RuntimeError, match="catalog unavailable"):
        await service.sync(owner="monitor-a")
    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        status = await uow.jenkins.sync_status()
    assert status["status"] == "failed"
    assert status["failure_summary"] == "RuntimeError: catalog unavailable"


@pytest.mark.asyncio
async def test_repeated_enrichment_failure_becomes_investigable_without_log(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    source = MonitorSource(enrichment_error=True)
    service = _service(postgres_session_factory, source)
    for index in range(3):
        stats = await service.sync(owner=f"monitor-{index}")
        assert stats is not None
    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        failures = await uow.jenkins.failure_builds(since=NOW - timedelta(days=7), limit=10)
        active_incidents = await uow.incidents.active()
        request = await uow.investigation_requests.latest_for_incident(active_incidents[0].id)
    assert failures.items[0]["enrichment_status"] == "failed"
    detail = failures.items[0]
    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        persisted = await uow.jenkins.build_detail(detail["id"])
    assert persisted is not None and persisted["evidence"]["enrichment_attempt_count"] == 3
    assert request is not None


@pytest.mark.asyncio
async def test_monitor_worker_run_once_and_stopped_loop(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    worker = JenkinsMonitorWorker(
        owner="worker-a",
        monitor=_service(postgres_session_factory, MonitorSource(), automatic=False),
        interval_seconds=0,
    )
    stats = await worker.run_once()
    assert stats is not None and stats.jobs_discovered == 4
    stop = __import__("asyncio").Event()
    stop.set()
    await worker.run_forever(stop)


@pytest.mark.asyncio
async def test_monitor_worker_loop_handles_backlog_and_iteration_failure(
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    worker = JenkinsMonitorWorker(
        owner="worker-loop",
        monitor=_service(postgres_session_factory, MonitorSource(), automatic=False),
        interval_seconds=15,
    )
    stop = asyncio.Event()
    stats = SimpleNamespace(
        jobs_discovered=1,
        builds_observed=1,
        new_builds=1,
        details={
            "pending_enrichment_remaining_is_bounded": True,
            "analysis_backlog_remaining_is_bounded": True,
        },
    )

    async def one_success():
        stop.set()
        return stats

    monkeypatch.setattr(worker, "run_once", one_success)
    await worker.run_forever(stop)

    stop.clear()

    async def one_failure():
        stop.set()
        raise RuntimeError("sync failed")

    monkeypatch.setattr(worker, "run_once", one_failure)
    await worker.run_forever(stop)


@pytest.mark.asyncio
async def test_monitor_heartbeat_detects_lost_lease_without_waiting(
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    service = _service(postgres_session_factory, MonitorSource())

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(monitor_module.asyncio, "sleep", no_wait)
    with pytest.raises(RuntimeError, match="lost Jenkins synchronization lease"):
        await service._heartbeat("missing-owner")
