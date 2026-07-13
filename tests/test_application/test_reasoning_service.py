from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jenkins_watchdog.application.reasoning import ReasoningService, evidence_digest
from jenkins_watchdog.application.types import EnqueueScan
from jenkins_watchdog.domain.model import (
    CheckResult,
    CheckStatus,
    Confidence,
    FindingObservation,
    Incident,
    Investigation,
    InvestigationStatus,
    ScanMode,
    Severity,
)
from jenkins_watchdog.infrastructure.uow import SqlAlchemyUnitOfWork

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)


class ReasoningPort:
    def __init__(self) -> None:
        self.investigations = 0
        self.chats = []

    async def triage(self, incident, observations):
        del incident, observations
        return {}

    async def investigate(self, incident, observations):
        self.investigations += 1
        return Investigation(
            id=str(uuid.uuid4()),
            incident_id=incident.id,
            occurrence_id=incident.current_occurrence.id,
            status=InvestigationStatus.SUCCEEDED,
            evidence_hash=evidence_digest(observations),
            input_version="v1",
            prompt_version="v1",
            model="test-model",
            confidence=Confidence.MEDIUM,
            usage={"total_tokens": 20},
            result={
                "actionability": "actionable",
                "classification": "infrastructure",
                "priority": "critical",
                "deterministic_severity": incident.severity.value,
            },
            created_at=NOW,
            completed_at=NOW,
        )

    async def chat(self, *, message, incident=None):
        self.chats.append((message, incident.id if incident else None))
        return "answer"


async def seed(factory: async_sessionmaker[AsyncSession]) -> Incident:
    async with SqlAlchemyUnitOfWork(factory) as uow:
        scan = await uow.scans.add(EnqueueScan(mode=ScanMode.REGULAR, categories=("k8s_node",)))
        observation = FindingObservation(
            scan_id=scan.id,
            check_name="k8s_nodes",
            rule_id="k8s.node.pressure.v1",
            resource_id="node/worker-1",
            severity=Severity.CRITICAL,
            category="k8s_node",
            summary="memory pressure",
            observed_at=NOW,
            evidence={"condition": "MemoryPressure"},
        )
        await uow.checks.save(
            scan.id,
            CheckResult(
                scan_id=scan.id,
                check_name=observation.check_name,
                status=CheckStatus.SUCCEEDED,
                started_at=NOW,
                completed_at=NOW,
            ),
        )
        await uow.findings.add_observations(scan.id, (observation,))
        incident = Incident.open_new(
            id=str(uuid.uuid4()),
            correlation_rule_id="stable_finding",
            correlation_key=observation.stable_identity,
            observation=observation,
            opened_at=NOW,
        )
        await uow.incidents.save(incident)
        await uow.incidents.link_observation(incident, observation)
        await uow.commit()
    return incident


@pytest.mark.asyncio
async def test_reasoning_service_persists_triage_reuses_fresh_result_and_supports_chat(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    incident = await seed(postgres_session_factory)
    adapter = ReasoningPort()
    service = ReasoningService(
        uow_factory=lambda: SqlAlchemyUnitOfWork(postgres_session_factory),
        reasoning=adapter,
        now=lambda: NOW,
    )

    first = await service.investigate_if_needed(incident.id)
    reused = await service.investigate_if_needed(incident.id)
    forced = await service.investigate_if_needed(incident.id, force=True)
    contextual = await service.chat(message="why", incident_id=incident.id)
    global_answer = await service.chat(message="status")

    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        updated = await uow.incidents.get(incident.id)
    assert first is not None and first.status is InvestigationStatus.SUCCEEDED
    assert reused == first
    assert forced is not None and forced.id != first.id
    assert adapter.investigations == 2
    assert updated is not None and updated.actionability == "actionable"
    assert updated.classification == "infrastructure"
    assert updated.priority == "critical"
    assert contextual == global_answer == "answer"
    assert adapter.chats == [("why", incident.id), ("status", None)]


@pytest.mark.asyncio
async def test_reasoning_service_rejects_unknown_incident(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = ReasoningService(
        uow_factory=lambda: SqlAlchemyUnitOfWork(postgres_session_factory),
        reasoning=ReasoningPort(),
        now=lambda: NOW,
    )

    with pytest.raises(LookupError):
        await service.investigate_if_needed(str(uuid.uuid4()))
    with pytest.raises(LookupError):
        await service.chat(message="why", incident_id=str(uuid.uuid4()))
