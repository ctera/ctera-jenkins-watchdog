from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jenkins_watchdog.application import reasoning as reasoning_module
from jenkins_watchdog.application.reasoning import ReasoningService, evidence_digest
from jenkins_watchdog.application.types import EnqueueScan, ReasoningReply, TriageBatchResult
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
        self.investigation_options = []

    async def triage_batch(self, candidates):
        del candidates
        return TriageBatchResult(routes=())

    async def investigate(self, incident, observations, **kwargs):
        self.investigations += 1
        self.investigation_options.append(kwargs)
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

    async def chat(self, *, message, incident=None, context=None, history=(), on_progress=None):
        del context
        self.chats.append((message, incident.id if incident else None, history, on_progress is not None))
        return ReasoningReply(content="answer")


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
    assert contextual.content == global_answer.content == "answer"
    assert contextual.coverage_status == global_answer.coverage_status == "unavailable"
    assert incident.id in {reference["id"] for reference in contextual.references}
    assert adapter.chats == [("why", incident.id, (), False), ("status", None, (), False)]
    assert not await service.needs_investigation(incident.id)


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
        await service.needs_investigation(str(uuid.uuid4()))
    with pytest.raises(LookupError):
        await service.chat(message="why", incident_id=str(uuid.uuid4()))


@pytest.mark.asyncio
async def test_deep_investigation_and_streaming_chat_forward_agent_context(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    incident = await seed(postgres_session_factory)
    adapter = ReasoningPort()
    service = ReasoningService(
        uow_factory=lambda: SqlAlchemyUnitOfWork(postgres_session_factory),
        reasoning=adapter,
        now=lambda: NOW,
    )

    progress_events = []

    async def progress(event):
        progress_events.append(event)

    investigation = await service.investigate_if_needed(
        incident.id,
        force=True,
        mode=ScanMode.DEEP,
        on_progress=progress,
    )
    answer = await service.chat(
        message="continue",
        incident_id=incident.id,
        history=({"role": "user", "content": "earlier"},),
        on_progress=progress,
    )
    assert investigation is not None
    assert adapter.investigation_options[0]["mode"] is ScanMode.DEEP
    assert adapter.investigation_options[0]["on_progress"] is progress
    assert adapter.investigation_options[0]["context"]["evidence_hash"]
    assert answer.content == "answer"
    assert adapter.chats[-1][2] == ({"role": "user", "content": "earlier"},)
    assert adapter.chats[-1][3] is True


def test_reasoning_context_helpers_bound_payloads_and_count_queue_tasks() -> None:
    succeeded = SimpleNamespace(status=SimpleNamespace(value="succeeded"))
    failed = SimpleNamespace(status=SimpleNamespace(value="failed"))
    assert reasoning_module._coverage_status(()) == "unavailable"
    assert reasoning_module._coverage_status((succeeded,)) == "complete"
    assert reasoning_module._coverage_status((failed,)) == "unavailable"
    assert reasoning_module._coverage_status((succeeded, failed)) == "partial"

    queue_observations = (
        SimpleNamespace(category="jenkins_queue", evidence={"queue_task": "1"}, resource_id="controller"),
        SimpleNamespace(category="jenkins_queue", evidence={"queue_task": "2"}, resource_id="controller"),
    )
    assert reasoning_module._affected_resource_count(queue_observations) == 2
    assert reasoning_module._affected_resource_count((SimpleNamespace(category="node", evidence={}, resource_id="a"),)) == 1
    assert reasoning_module._bounded_value({"a": {"b": {"c": {"d": "hidden"}}}})["a"]["b"]["c"]["d"] == "[truncated]"
    assert reasoning_module._bounded_value(tuple(range(20))) == list(range(12))
    assert reasoning_module._bounded_value("x" * 600) == "x" * 500
    assert reasoning_module._severity_rank("critical") > reasoning_module._severity_rank("warning")
    assert reasoning_module._severity_rank("unknown") == 0

    builds = (
        {
            "id": "build-1",
            "job_name": "portal",
            "build_number": 12,
            "result": "FAILURE",
            "priority_score": 80,
            "started_at": "2026-07-15T10:00:00Z",
            "source_provider": "gitlab",
            "repository": "ctera/portal",
            "change_number": "42",
        },
    )
    observations = reasoning_module.jenkins_build_observations(builds)
    assert observations[0].severity is Severity.CRITICAL
    assert observations[0].summary == "portal #12 failed"
    assert observations[0].evidence["scm"]["change_number"] == "42"
