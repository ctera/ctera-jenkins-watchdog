from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jenkins_watchdog.application.types import EnqueueScan
from jenkins_watchdog.domain.model import (
    CheckResult,
    CheckStatus,
    FindingObservation,
    Incident,
    ScanMode,
    ScanStage,
    Severity,
)
from jenkins_watchdog.infrastructure.models import Base
from jenkins_watchdog.infrastructure.uow import SqlAlchemyUnitOfWork

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)


def test_schema_contains_expected_business_tables() -> None:
    assert set(Base.metadata.tables) == {
        "scans",
        "check_executions",
        "findings",
        "incidents",
        "incident_occurrences",
        "incident_findings",
        "investigations",
        "actions",
        "delivery_attempts",
        "scan_events",
        "jenkins_jobs",
        "jenkins_builds",
        "jenkins_build_edges",
        "jenkins_sync_state",
        "investigation_requests",
    }


async def _enqueue(factory: async_sessionmaker[AsyncSession]) -> str:
    async with SqlAlchemyUnitOfWork(factory) as uow:
        scan = await uow.scans.add(EnqueueScan(mode=ScanMode.REGULAR, categories=("k8s_node",)))
        await uow.commit()
        return scan.id


@pytest.mark.asyncio
async def test_two_workers_claim_a_scan_only_once(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    scan_id = await _enqueue(postgres_session_factory)
    claim_at = datetime.now(timezone.utc) + timedelta(seconds=1)

    async def claim(owner: str):
        async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
            scan = await uow.scans.claim(owner=owner, now=claim_at, lease_seconds=60)
            await uow.commit()
            return scan

    first, second = await asyncio.gather(claim("worker-a"), claim("worker-b"))

    claimed = [scan for scan in (first, second) if scan is not None]
    assert len(claimed) == 1
    assert claimed[0].id == scan_id
    assert claimed[0].attempt_count == 1


@pytest.mark.asyncio
async def test_expired_scan_lease_resumes_persisted_stage_and_terminal_check(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    scan_id = await _enqueue(postgres_session_factory)
    claim_at = datetime.now(timezone.utc) + timedelta(seconds=1)
    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        claimed = await uow.scans.claim(owner="dead-worker", now=claim_at, lease_seconds=60)
        assert claimed is not None
        claimed = claimed.advance(ScanStage.FINDINGS_STORED, now=claim_at)
        await uow.scans.save(claimed)
        await uow.checks.save(
            scan_id,
            CheckResult(
                scan_id=scan_id,
                check_name="k8s_nodes",
                status=CheckStatus.SUCCEEDED,
                categories=frozenset({"k8s_node"}),
                started_at=claim_at,
                completed_at=claim_at,
            ),
        )
        await uow.commit()

    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        reclaimed = await uow.scans.claim(owner="replacement", now=claim_at + timedelta(seconds=61), lease_seconds=60)
        saved_check = await uow.checks.get(scan_id, "k8s_nodes")
        await uow.commit()

    assert reclaimed is not None
    assert reclaimed.stage == ScanStage.FINDINGS_STORED
    assert reclaimed.attempt_count == 2
    assert saved_check is not None and saved_check.status == CheckStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_findings_are_persisted_before_and_linked_once_during_correlation(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    scan_id = await _enqueue(postgres_session_factory)
    observation = FindingObservation(
        scan_id=scan_id,
        check_name="jenkins_failed_builds",
        rule_id="jenkins.failed_build.v1",
        resource_id="job/main",
        severity=Severity.CRITICAL,
        category="jenkins_failed_build",
        summary="compile failed",
        observed_at=NOW,
        identity_dimensions={"error_signature": "compiler"},
        evidence={"build": 42},
    )
    incident = Incident.open_new(
        id=str(uuid.uuid4()),
        correlation_rule_id="jenkins_error_signature",
        correlation_key="compiler",
        observation=observation,
        opened_at=NOW,
    )

    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        await uow.checks.save(
            scan_id,
            CheckResult(
                scan_id=scan_id,
                check_name=observation.check_name,
                status=CheckStatus.SUCCEEDED,
                categories=frozenset({observation.category}),
                started_at=NOW,
                completed_at=NOW,
            ),
        )
        await uow.findings.add_observations(scan_id, (observation,))
        assert await uow.findings.unlinked_for_scan(scan_id) == (observation,)
        await uow.incidents.save(incident)
        await uow.incidents.link_observation(incident, observation)
        await uow.incidents.link_observation(incident, observation)
        await uow.commit()

    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        assert await uow.findings.unlinked_for_scan(scan_id) == ()
        restored = await uow.incidents.get(incident.id)

    assert restored is not None
    assert restored.current_occurrence.observation_identities == frozenset({observation.stable_identity})


@pytest.mark.asyncio
async def test_scan_event_sequences_are_durable_and_monotonic(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    scan_id = await _enqueue(postgres_session_factory)
    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        first = await uow.events.append(scan_id, "scan_queued", {}, now=NOW)
        second = await uow.events.append(scan_id, "scan_started", {"worker": "a"}, now=NOW)
        await uow.commit()

    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        replay = await uow.events.after(scan_id, first.sequence)

    assert first.sequence == 1
    assert second.sequence == 2
    assert replay == (second,)
