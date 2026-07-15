from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jenkins_watchdog.application.incidents import IncidentService
from jenkins_watchdog.application.types import EnqueueScan
from jenkins_watchdog.domain.model import CheckResult, CheckStatus, FindingObservation, ScanMode, Severity
from jenkins_watchdog.domain.source import SourceAttribution, SourceKind, SourceStatus
from jenkins_watchdog.infrastructure.uow import SqlAlchemyUnitOfWork

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)


async def store_observation(
    factory: async_sessionmaker[AsyncSession],
    *,
    evidence: dict,
    observed_at: datetime,
    category: str = "jenkins_failed_build",
) -> tuple[str, FindingObservation]:
    async with SqlAlchemyUnitOfWork(factory) as uow:
        scan = await uow.scans.add(EnqueueScan(mode=ScanMode.REGULAR, categories=("jenkins_failed_build",)))
        observation = FindingObservation(
            scan_id=scan.id,
            check_name="jenkins_failed_builds",
            rule_id="jenkins.failed.v1",
            resource_id="job/app",
            severity=Severity.WARNING,
            category=category,
            summary="build failed",
            observed_at=observed_at,
            identity_dimensions={"error_signature": "same-error"},
            evidence=evidence,
        )
        await uow.checks.save(
            scan.id,
            CheckResult(
                scan_id=scan.id,
                check_name=observation.check_name,
                status=CheckStatus.SUCCEEDED,
                categories=frozenset({observation.category}),
                started_at=observed_at,
                completed_at=observed_at,
            ),
        )
        await uow.findings.add_observations(scan.id, (observation,))
        await uow.scans.save(scan.succeed(now=observed_at))
        await uow.commit()
    return scan.id, observation


@pytest.mark.asyncio
async def test_incident_service_links_every_observation_and_preserves_confirmed_source(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = IncidentService(lambda: SqlAlchemyUnitOfWork(postgres_session_factory))
    first_scan, first = await store_observation(
        postgres_session_factory,
        evidence={
            "job_name": "app/MR-42",
            "scm": {"provider": "github", "repository": "ctera/app", "change_number": 42},
        },
        observed_at=NOW,
    )

    first_ids = await service.correlate_and_reconcile(
        scan_id=first_scan,
        selected_checks=frozenset({first.check_name}),
        successful_checks=frozenset({first.check_name}),
        now=NOW,
    )
    incident_id = next(iter(first_ids))
    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        first_incident = await uow.incidents.get(incident_id)
    assert first_incident is not None and first_incident.source["kind"] == "merge_request"

    second_scan, second = await store_observation(
        postgres_session_factory,
        evidence={"scm": {"provider": "github", "repository": "ctera/app"}},
        observed_at=NOW + timedelta(minutes=1),
    )
    second_ids = await service.correlate_and_reconcile(
        scan_id=second_scan,
        selected_checks=frozenset({second.check_name}),
        successful_checks=frozenset({second.check_name}),
        now=NOW + timedelta(minutes=1),
    )

    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        restored = await uow.incidents.get(incident_id)
        observations = await uow.incidents.observations(incident_id)
        unlinked = await uow.findings.unlinked_for_scan(second_scan)
    assert second_ids == first_ids
    assert restored is not None
    assert restored.source["kind"] == "merge_request"
    assert restored.source["confirmed"] is True
    assert restored.source["provider"] == "github"
    assert restored.source["repository"] == "ctera/app"
    assert restored.source["change_number"] == "42"
    assert {item.scan_id for item in observations} == {first_scan, second_scan}
    assert unlinked == ()


@pytest.mark.asyncio
async def test_conflicting_complete_sources_remain_visible_as_multiple(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = IncidentService(lambda: SqlAlchemyUnitOfWork(postgres_session_factory))
    scan_id, first = await store_observation(
        postgres_session_factory,
        evidence={"provider": "github", "repository": "ctera/app", "change_number": 42},
        observed_at=NOW,
    )
    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        second = FindingObservation(
            scan_id=scan_id,
            check_name=first.check_name,
            rule_id="jenkins.pattern.v1",
            resource_id="job/other",
            severity=Severity.CRITICAL,
            category=first.category,
            summary="same failure",
            observed_at=NOW,
            identity_dimensions={"error_signature": "same-error"},
            evidence={"provider": "gitlab", "repository": "ctera/other", "change_number": 99},
        )
        await uow.findings.add_observations(scan_id, (second,))
        await uow.commit()

    incident_ids = await service.correlate_and_reconcile(
        scan_id=scan_id,
        selected_checks=frozenset({first.check_name}),
        successful_checks=frozenset({first.check_name}),
        now=NOW,
    )
    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        restored = await uow.incidents.get(next(iter(incident_ids)))
        observations = await uow.incidents.observations(restored.id) if restored else ()

    assert restored is not None
    assert restored.severity is Severity.CRITICAL
    assert restored.source["kind"] == "multiple"
    assert restored.source["confirmed"] is True
    assert restored.source["source_count"] == 2
    assert {
        (source["provider"], source["repository"], source["change_number"])
        for source in restored.source["sources"]
    } == {
        ("github", "ctera/app", "42"),
        ("gitlab", "ctera/other", "99"),
    }
    assert len(observations) == 2


@pytest.mark.asyncio
async def test_unsupported_complete_scm_provider_is_not_confirmed(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    scan_id, observation = await store_observation(
        postgres_session_factory,
        evidence={"provider": "bitbucket", "repository": "ctera/app", "change_number": 42},
        observed_at=NOW,
    )
    service = IncidentService(lambda: SqlAlchemyUnitOfWork(postgres_session_factory))

    incident_ids = await service.correlate_and_reconcile(
        scan_id=scan_id,
        selected_checks=frozenset({observation.check_name}),
        successful_checks=frozenset({observation.check_name}),
        now=NOW,
    )
    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        incident = await uow.incidents.get(next(iter(incident_ids)))

    assert incident is not None
    assert incident.source == {
        "kind": "unknown",
        "confirmed": False,
        "reason": "unsupported_scm_provider",
    }


@pytest.mark.asyncio
async def test_associate_jenkins_source_merges_revision_and_pipeline_evidence(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    scan_id, observation = await store_observation(
        postgres_session_factory,
        evidence={},
        observed_at=NOW,
        category="jenkins_build",
    )
    service = IncidentService(lambda: SqlAlchemyUnitOfWork(postgres_session_factory))
    incident_ids = await service.correlate_and_reconcile(
        scan_id=scan_id,
        selected_checks=frozenset({observation.check_name}),
        successful_checks=frozenset({observation.check_name}),
        now=NOW,
    )
    incident_id = next(iter(incident_ids))
    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        initial = await uow.incidents.get(incident_id)
    assert initial is not None
    assert initial.source["kind"] == "infrastructure"

    revision = await service.associate_jenkins_source(
        incident_id,
        SourceAttribution(
            kind=SourceKind.REPOSITORY_REVISION,
            status=SourceStatus.VERIFIED,
            provider="gitlab",
            repository="Portal/Backend",
            branch="main",
            commit_sha="444e7bd",
            profile_id="portal-backend",
            verified_at=NOW,
        ),
        now=NOW,
    )
    combined = await service.associate_jenkins_source(
        incident_id,
        SourceAttribution(
            kind=SourceKind.PIPELINE,
            status=SourceStatus.RESOLVED,
            provider="jenkins",
            title="Nightly_Portal #42",
            state="timer",
        ),
        now=NOW + timedelta(minutes=1),
    )
    missing = await service.associate_jenkins_source(
        "00000000-0000-0000-0000-000000000000",
        SourceAttribution(SourceKind.PIPELINE, SourceStatus.RESOLVED),
        now=NOW,
    )

    assert revision is not None
    assert revision.source["kind"] == "repository"
    assert revision.source["verified"] is True
    assert revision.source["commit_sha"] == "444e7bd"
    assert combined is not None
    assert combined.source["kind"] == "multiple"
    assert combined.source["source_count"] == 2
    assert {source["kind"] for source in combined.source["sources"]} == {"repository", "pipeline"}
    assert missing is None


@pytest.mark.asyncio
async def test_repeated_pipeline_runs_share_one_incident_source(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    scan_id, observation = await store_observation(
        postgres_session_factory,
        evidence={},
        observed_at=NOW,
        category="jenkins_build",
    )
    service = IncidentService(lambda: SqlAlchemyUnitOfWork(postgres_session_factory))
    incident_ids = await service.correlate_and_reconcile(
        scan_id=scan_id,
        selected_checks=frozenset({observation.check_name}),
        successful_checks=frozenset({observation.check_name}),
        now=NOW,
    )
    incident_id = next(iter(incident_ids))

    for number in (42, 43):
        updated = await service.associate_jenkins_source(
            incident_id,
            SourceAttribution(
                kind=SourceKind.PIPELINE,
                status=SourceStatus.RESOLVED,
                provider="jenkins",
                title=f"Nightly_Portal #{number}",
                state="timer",
                url=f"https://jenkins.example/job/Nightly_Portal/{number}/",
            ),
            now=NOW + timedelta(minutes=number),
        )

    assert updated is not None
    assert updated.source["kind"] == "pipeline"
    assert updated.source["job_name"] == "Nightly_Portal"
    assert updated.source["url"].endswith("/43/")


@pytest.mark.asyncio
async def test_pending_source_is_not_confirmed(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    scan_id, observation = await store_observation(
        postgres_session_factory,
        evidence={},
        observed_at=NOW,
        category="jenkins_build",
    )
    service = IncidentService(lambda: SqlAlchemyUnitOfWork(postgres_session_factory))
    incident_ids = await service.correlate_and_reconcile(
        scan_id=scan_id,
        selected_checks=frozenset({observation.check_name}),
        successful_checks=frozenset({observation.check_name}),
        now=NOW,
    )
    updated = await service.associate_jenkins_source(
        next(iter(incident_ids)),
        SourceAttribution(
            kind=SourceKind.CHANGE_REQUEST,
            status=SourceStatus.PENDING,
            provider="gitlab",
            repository="Portal/Backend",
            change_number="6836",
        ),
        now=NOW,
    )

    assert updated is not None
    assert updated.source["kind"] == "merge_request"
    assert updated.source["confirmed"] is False


@pytest.mark.asyncio
async def test_concrete_source_conflict_replaces_generic_infrastructure_placeholder(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    scan_id, observation = await store_observation(
        postgres_session_factory,
        evidence={},
        observed_at=NOW,
        category="jenkins_build",
    )
    service = IncidentService(lambda: SqlAlchemyUnitOfWork(postgres_session_factory))
    incident_ids = await service.correlate_and_reconcile(
        scan_id=scan_id,
        selected_checks=frozenset({observation.check_name}),
        successful_checks=frozenset({observation.check_name}),
        now=NOW,
    )
    updated = await service.associate_jenkins_source(
        next(iter(incident_ids)),
        SourceAttribution(
            kind=SourceKind.CHANGE_REQUEST,
            status=SourceStatus.CONFLICT,
            provider="gitlab",
            repository="Portal/Backend",
            change_number="6836",
            profile_id="portal-backend",
            reason="provider_record_not_found",
        ),
        now=NOW,
    )

    assert updated is not None
    assert updated.source["kind"] == "merge_request"
    assert updated.source["status"] == "conflict"
    assert updated.source["confirmed"] is False
    assert updated.source["reason"] == "provider_record_not_found"
