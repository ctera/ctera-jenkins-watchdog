from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jenkins_watchdog.application.investigations import InvestigationQueueService
from jenkins_watchdog.application.jenkins_reports import JenkinsFailureReportService
from jenkins_watchdog.domain.jenkins import (
    JenkinsBuildHistoryPage,
    JenkinsBuildSnapshot,
    JenkinsCoverage,
    JenkinsJobSnapshot,
)
from jenkins_watchdog.domain.model import (
    Confidence,
    Investigation,
    InvestigationStatus,
    ScanMode,
)
from jenkins_watchdog.infrastructure.uow import SqlAlchemyUnitOfWork

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)


def _job(name: str, *, container: bool = False) -> JenkinsJobSnapshot:
    return JenkinsJobSnapshot(
        full_name=name,
        display_name=name,
        url=f"https://jenkins.example/job/{name}/",
        job_class=(
            "com.cloudbees.hudson.plugins.folder.Folder"
            if container
            else "org.jenkinsci.plugins.workflow.job.WorkflowJob"
        ),
        color="red",
        parent_full_name=None,
        first_build_number=1,
        first_build_at=NOW - timedelta(days=2),
        last_build_number=10,
        last_build_at=NOW,
    )


def _build(job: str, number: int, result: str, *, age_hours: int = 1) -> JenkinsBuildSnapshot:
    return JenkinsBuildSnapshot(
        job_full_name=job,
        number=number,
        result=result,
        url=f"https://jenkins.example/job/{job}/{number}/",
        started_at=NOW - timedelta(hours=age_hours),
        duration_ms=60_000,
    )


class ReportSource:
    def __init__(
        self,
        jobs: tuple[JenkinsJobSnapshot, ...],
        histories: dict[str, JenkinsBuildHistoryPage | BaseException],
    ) -> None:
        self.jobs = jobs
        self.histories = histories
        self.calls: list[tuple[str, datetime, int | None]] = []

    async def discover_jobs(self) -> tuple[JenkinsJobSnapshot, ...]:
        return self.jobs

    async def build_history(
        self,
        job: JenkinsJobSnapshot,
        *,
        cutoff: datetime,
        after_number: int | None,
    ) -> JenkinsBuildHistoryPage:
        self.calls.append((job.full_name, cutoff, after_number))
        result = self.histories[job.full_name]
        if isinstance(result, BaseException):
            raise result
        return result

    async def enrich_build(self, *args, **kwargs):
        raise AssertionError("report collection must not invoke diagnostic enrichment")

    async def enrich_job_source(self, *args, **kwargs):
        raise AssertionError("report collection must not infer a source before investigation")

    async def attribute_build(self, *args, **kwargs):
        raise AssertionError("report collection must not infer build attribution")


def _services(
    factory: async_sessionmaker[AsyncSession],
    source: ReportSource,
    clock: list[datetime],
) -> tuple[JenkinsFailureReportService, InvestigationQueueService]:
    def uow_factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(factory)

    queue = InvestigationQueueService(
        uow_factory=uow_factory,
        now=lambda: clock[0],
        reservation_tokens=1_000,
        deep_reservation_tokens=1_000,
        daily_token_budget=0,
        daily_cost_budget_usd=Decimal("0"),
    )
    return (
        JenkinsFailureReportService(
            source=source,  # type: ignore[arg-type]
            uow_factory=uow_factory,
            queue=queue,
            now=lambda: clock[0],
        ),
        queue,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "window_hours"),
    [(ScanMode.REGULAR, 4), (ScanMode.DEEP, 24)],
)
async def test_report_uses_a_fixed_window_and_ignores_catalog_watermarks(
    postgres_session_factory: async_sessionmaker[AsyncSession],
    mode: ScanMode,
    window_hours: int,
) -> None:
    job = _job("Portal/Main")
    source = ReportSource(
        (job,),
        {job.full_name: JenkinsBuildHistoryPage((), JenkinsCoverage.EXACT)},
    )
    clock = [NOW]
    service, _ = _services(postgres_session_factory, source, clock)

    report = await service.create(mode=mode)

    assert report["window_ended_at"] == NOW
    assert report["window_started_at"] == NOW - timedelta(hours=window_hours)
    assert report["status"] == "complete"
    assert source.calls == [(job.full_name, NOW - timedelta(hours=window_hours), None)]


@pytest.mark.asyncio
async def test_report_collects_every_failed_build_and_surfaces_job_coverage_errors(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    folder = _job("Portal", container=True)
    exact = _job("Portal/Main")
    retained = _job("Portal/Release")
    inaccessible = _job("Portal/Restricted")
    source = ReportSource(
        (folder, exact, retained, inaccessible),
        {
            exact.full_name: JenkinsBuildHistoryPage(
                (
                    _build(exact.full_name, 10, "FAILURE"),
                    _build(exact.full_name, 10, "FAILURE"),
                    _build(exact.full_name, 9, "ABORTED", age_hours=2),
                    _build(exact.full_name, 8, "SUCCESS", age_hours=3),
                    _build(exact.full_name, 7, "FAILURE", age_hours=5),
                ),
                JenkinsCoverage.EXACT,
            ),
            retained.full_name: JenkinsBuildHistoryPage(
                (_build(retained.full_name, 4, "UNSTABLE", age_hours=3),),
                JenkinsCoverage.RETENTION_LIMITED,
            ),
            inaccessible.full_name: PermissionError("Jenkins returned 403"),
        },
    )
    clock = [NOW]
    service, _ = _services(postgres_session_factory, source, clock)

    report = await service.create(mode=ScanMode.REGULAR)

    assert report["status"] == "investigating"
    assert report["jobs_discovered"] == 3
    assert report["failures_found"] == 3
    assert report["total_builds"] == 3
    assert report["counts"] == {"queued": 3}
    assert {(row["job_name"], row["build_number"]) for row in report["builds"]} == {
        ("Portal/Main", 9),
        ("Portal/Main", 10),
        ("Portal/Release", 4),
    }
    assert len({row["build_id"] for row in report["builds"]}) == 3
    assert len({row["investigation_request_id"] for row in report["builds"]}) == 3
    assert report["coverage_exceptions"] == [
        {"job_name": "Portal/Release", "kind": "retention_limited"},
        {
            "job_name": "Portal/Restricted",
            "kind": "inaccessible",
            "error": "PermissionError: Jenkins returned 403",
        },
    ]
    assert [call[0] for call in source.calls] == [
        "Portal/Main",
        "Portal/Release",
        "Portal/Restricted",
    ]
    assert all(after_number is None for _, _, after_number in source.calls)

    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        requests = [
            await uow.investigation_requests.get(row["investigation_request_id"])
            for row in report["builds"]
        ]
    assert all(request is not None for request in requests)
    assert {request.build_id for request in requests if request is not None} == {
        row["build_id"] for row in report["builds"]
    }
    assert all(request.source == "jenkins_report" for request in requests if request is not None)


async def _finish_request(
    factory: async_sessionmaker[AsyncSession],
    request_id: str,
    *,
    now: datetime,
    status: InvestigationStatus,
) -> None:
    async with SqlAlchemyUnitOfWork(factory) as uow:
        request = await uow.investigation_requests.get(request_id)
        assert request is not None
        claimed = request.claim(owner="test-worker", now=now, lease_seconds=60)
        investigation = Investigation(
            id=str(uuid.uuid4()),
            incident_id=request.incident_id,
            occurrence_id=request.occurrence_id,
            status=status,
            evidence_hash=request.evidence_hash,
            input_version="v2",
            prompt_version="tool-agent-v1",
            model="test-model",
            confidence=Confidence.HIGH if status is InvestigationStatus.SUCCEEDED else Confidence.LOW,
            result={
                "root_cause": "compiler mismatch" if status is InvestigationStatus.SUCCEEDED else None,
                "plain_language_summary": "The compiler did not match the project configuration.",
                "impact": "The build did not produce an artifact.",
                "evidence": [{"source": "jenkins_console", "reference": "lines 80-92"}],
                "suggested_fix": "Use the compiler version pinned by the project.",
                "verification_steps": ["Re-run the exact build."],
                "confidence": "high" if status is InvestigationStatus.SUCCEEDED else "low",
            },
            error_summary=("The required compiler manifest was inaccessible." if status is InvestigationStatus.PARTIAL else None),
            created_at=now,
            completed_at=now,
        )
        await uow.investigations.save(investigation)
        await uow.investigation_requests.save(claimed.succeed(investigation.id, now=now))
        await uow.commit()


@pytest.mark.asyncio
async def test_report_waits_for_budget_and_completes_only_after_every_build_is_terminal(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    job = _job("Portal/Main")
    source = ReportSource(
        (job,),
        {
            job.full_name: JenkinsBuildHistoryPage(
                (
                    _build(job.full_name, 10, "FAILURE"),
                    _build(job.full_name, 9, "FAILURE", age_hours=2),
                ),
                JenkinsCoverage.EXACT,
            )
        },
    )
    clock = [NOW]
    service, _ = _services(postgres_session_factory, source, clock)
    report = await service.create(mode=ScanMode.REGULAR)
    first, second = report["builds"]
    reset_at = NOW + timedelta(hours=12)

    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        waiting = await uow.investigation_requests.get(second["investigation_request_id"])
        assert waiting is not None
        await uow.investigation_requests.save(
            replace(
                waiting,
                next_attempt_at=reset_at,
                error_summary="automatic daily LLM token budget exhausted",
                updated_at=NOW,
            )
        )
        await uow.commit()

    await _finish_request(
        postgres_session_factory,
        first["investigation_request_id"],
        now=NOW,
        status=InvestigationStatus.SUCCEEDED,
    )
    paused = await service.detail(report["id"], limit=50, offset=0)
    assert paused is not None
    assert paused["status"] == "waiting_budget"
    assert paused["completed_at"] is None
    assert paused["budget_reset_at"] == reset_at
    assert paused["counts"] == {"explained": 1, "waiting_budget": 1}

    clock[0] = reset_at
    await _finish_request(
        postgres_session_factory,
        second["investigation_request_id"],
        now=reset_at,
        status=InvestigationStatus.PARTIAL,
    )
    complete = await service.detail(report["id"], limit=1, offset=0)
    assert complete is not None
    assert complete["status"] == "complete"
    assert complete["completed_at"] == reset_at
    assert complete["total_builds"] == 2
    assert len(complete["builds"]) == 1
    assert complete["counts"] == {"evidence_gap": 1, "explained": 1}
