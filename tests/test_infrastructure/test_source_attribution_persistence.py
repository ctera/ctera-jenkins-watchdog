from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jenkins_watchdog.domain.jenkins import JenkinsBuildAttribution, JenkinsBuildSnapshot, JenkinsJobSnapshot
from jenkins_watchdog.domain.source import SourceAttribution, SourceKind, SourceStatus
from jenkins_watchdog.infrastructure.uow import SqlAlchemyUnitOfWork

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_source_attribution_deduplicates_and_propagates_by_logical_execution(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    jobs = tuple(
        JenkinsJobSnapshot(
            full_name=name,
            display_name=name,
            url=f"https://jenkins/job/{name}/",
            job_class="org.jenkinsci.plugins.workflow.job.WorkflowJob",
            color="red",
            parent_full_name=None,
            last_build_number=number,
            last_build_at=NOW,
        )
        for name, number in (("Portal_Build_DAILY_MR_PATCH", 12452), ("Portal_Unit_Tests", 8172))
    )
    builds = tuple(
        JenkinsBuildSnapshot(
            job_full_name=job.full_name,
            number=job.last_build_number or 1,
            result="FAILURE",
            url=f"{job.url}{job.last_build_number}/",
            started_at=NOW,
            duration_ms=120_000,
        )
        for job in jobs
    )

    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        await uow.jenkins.upsert_jobs(jobs, now=NOW)
        await uow.jenkins.upsert_builds(builds, now=NOW)
        assert len(await uow.jenkins.pending_attribution(limit=10)) == 2
        for build in builds:
            await uow.jenkins.save_attribution(
                JenkinsBuildAttribution(
                    job_full_name=build.job_full_name,
                    number=build.number,
                    root_job_full_name="MR_Trigger",
                    root_build_number=18156,
                    trigger_kind="gitlab_webhook",
                ),
                now=NOW,
            )
        pending = await uow.jenkins.pending_attribution(limit=10)
        assert len(pending) == 1

        selected = pending[0]
        await uow.jenkins.save_attribution(
            JenkinsBuildAttribution(
                job_full_name=selected.job_full_name,
                number=selected.number,
                root_job_full_name="MR_Trigger",
                root_build_number=18156,
                trigger_kind="gitlab_webhook",
                source=SourceAttribution(
                    kind=SourceKind.CHANGE_REQUEST,
                    status=SourceStatus.VERIFIED,
                    provider="gitlab",
                    repository="Portal/Backend",
                    change_number="6836",
                    url="http://git.ctera.local/Portal/Backend/-/merge_requests/6836",
                    branch="PIM-7623-av",
                    commit_sha="444e7bd",
                    title="PIM-7623 - infected files are missing device_id",
                    state="opened",
                    profile_id="portal-backend",
                    resolution_method="root_cause_url",
                    verified_at=NOW,
                ),
            ),
            now=NOW,
        )
        failures = await uow.jenkins.failure_builds(since=NOW - timedelta(hours=1), limit=10)
        await uow.commit()

    assert len(failures.items) == 2
    assert {item["logical_run_key"] for item in failures.items} == {"MR_Trigger#18156"}
    assert {item["source_status"] for item in failures.items} == {"verified"}
    assert {item["repository"] for item in failures.items} == {"Portal/Backend"}
    assert {item["change_number"] for item in failures.items} == {"6836"}
    assert all(
        item["source_verified_at"] == NOW
        and item["source_profile_id"] == "portal-backend"
        for item in failures.items
    )
