from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jenkins_watchdog.application.incidents import IncidentService
from jenkins_watchdog.application.types import EnqueueScan
from jenkins_watchdog.domain.model import CheckResult, CheckStatus, FindingObservation, ScanMode, Severity
from jenkins_watchdog.infrastructure.uow import SqlAlchemyUnitOfWork

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)


async def store_observation(
    factory: async_sessionmaker[AsyncSession],
    *,
    evidence: dict,
    observed_at: datetime,
) -> tuple[str, FindingObservation]:
    async with SqlAlchemyUnitOfWork(factory) as uow:
        scan = await uow.scans.add(EnqueueScan(mode=ScanMode.REGULAR, categories=("jenkins_failed_build",)))
        observation = FindingObservation(
            scan_id=scan.id,
            check_name="jenkins_failed_builds",
            rule_id="jenkins.failed.v1",
            resource_id="job/app",
            severity=Severity.WARNING,
            category="jenkins_failed_build",
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
async def test_incident_service_links_every_observation_and_marks_partial_scm_unknown(
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
    assert restored.source == {
        "kind": "unknown",
        "confirmed": False,
        "reason": "partial_scm_metadata",
    }
    assert {item.scan_id for item in observations} == {first_scan, second_scan}
    assert unlinked == ()


@pytest.mark.asyncio
async def test_conflicting_complete_sources_become_unknown(
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
    assert restored.source["reason"] == "conflicting_source_metadata"
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
