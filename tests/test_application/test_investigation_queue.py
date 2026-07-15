from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jenkins_watchdog.application.investigations import InvestigationQueueService, InvestigationWorker
from jenkins_watchdog.application.reasoning import ReasoningService, evidence_digest
from jenkins_watchdog.application.types import EnqueueScan, ReasoningReply, TriageBatchResult
from jenkins_watchdog.domain.jenkins import JenkinsBuildEnrichment, JenkinsBuildSnapshot, JenkinsJobSnapshot
from jenkins_watchdog.domain.model import (
    CheckResult,
    CheckStatus,
    Confidence,
    FindingObservation,
    Incident,
    Investigation,
    InvestigationRequestStatus,
    InvestigationStatus,
    ScanMode,
    Severity,
)
from jenkins_watchdog.infrastructure.uow import SqlAlchemyUnitOfWork

NOW = datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)


def uow_factory(factory: async_sessionmaker[AsyncSession]):
    return lambda: SqlAlchemyUnitOfWork(factory)


async def seed_incident(factory: async_sessionmaker[AsyncSession]) -> Incident:
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


class AgentReasoning:
    async def triage_batch(self, candidates):
        del candidates
        return TriageBatchResult(routes=())

    async def investigate(self, incident, observations, *, context=None, mode=ScanMode.REGULAR, on_progress=None):
        del context, mode
        if on_progress:
            await on_progress({"type": "tool_call", "tool": "k8s_get_resource"})
        return Investigation(
            id=str(uuid.uuid4()),
            incident_id=incident.id,
            occurrence_id=incident.current_occurrence.id,
            status=InvestigationStatus.SUCCEEDED,
            evidence_hash=evidence_digest(observations),
            input_version="v2",
            prompt_version="tool-agent-v1",
            model="test-model",
            confidence=Confidence.HIGH,
            usage={"total_tokens": 42},
            result={
                "root_cause": "node memory pressure",
                "actionability": "actionable",
                "classification": "infrastructure",
                "priority": "critical",
                "confidence": "high",
                "deterministic_severity": incident.severity.value,
            },
            created_at=NOW,
            completed_at=NOW,
        )

    async def chat(self, *, message, incident=None, context=None, history=(), on_progress=None):
        del incident, context, history, on_progress
        return ReasoningReply(content=message)


class Automation:
    def __init__(self) -> None:
        self.incidents: list[str] = []

    async def plan(self, incident_id: str):
        self.incidents.append(incident_id)
        return ()


class Events:
    async def append(self, *args, **kwargs):
        raise AssertionError((args, kwargs))


class RecordingEvents:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, object]]] = []

    async def append(self, scan_id, event_type, payload, *, now):
        del now
        self.events.append((scan_id, event_type, payload))


class FailedInvestigationReasoning:
    def __init__(self, incident: Incident, *, return_none: bool = False) -> None:
        self.incident = incident
        self.return_none = return_none

    async def investigate_if_needed(
        self,
        incident_id,
        *,
        force,
        mode,
        on_progress,
        budget_kind,
        scan_id,
    ):
        del budget_kind, scan_id
        assert incident_id == self.incident.id
        assert force is True and mode is ScanMode.DEEP
        await on_progress({"type": "tool_call", "tool": "jenkins_get_build_log"})
        if self.return_none:
            return None
        return Investigation(
            id=str(uuid.uuid4()),
            incident_id=incident_id,
            occurrence_id=self.incident.current_occurrence.id,
            status=InvestigationStatus.FAILED,
            evidence_hash="failed-evidence",
            input_version="v2",
            prompt_version="tool-agent-v1",
            model="test-model",
            confidence=Confidence.LOW,
            usage={},
            result={},
            error_summary="agent model unavailable",
            created_at=NOW,
            completed_at=NOW,
        )


class FailingAutomation:
    async def plan(self, incident_id: str):
        del incident_id
        raise RuntimeError("routing unavailable")


@pytest.mark.asyncio
async def test_queue_deduplicates_worker_persists_result_and_plans_actions(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    incident = await seed_incident(postgres_session_factory)
    factory = uow_factory(postgres_session_factory)
    queue = InvestigationQueueService(uow_factory=factory, now=lambda: NOW)
    first = await queue.enqueue_incident(incident.id, source="automatic", priority=80)
    duplicate = await queue.enqueue_incident(incident.id, source="manual", force=True)
    assert first is not None and duplicate is not None and duplicate.id == first.id

    automation = Automation()
    worker = InvestigationWorker(
        owner="worker-a",
        uow_factory=factory,
        reasoning=ReasoningService(uow_factory=factory, reasoning=AgentReasoning(), now=lambda: NOW),
        queue=queue,
        automation=automation,
        events=Events(),
        now=lambda: NOW,
        lease_seconds=60,
        heartbeat_seconds=5,
    )
    completed = await worker.run_once()
    assert completed is not None and completed.status is InvestigationRequestStatus.SUCCEEDED
    assert completed.investigation_id
    assert automation.incidents == [incident.id]

    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        persisted = await uow.investigation_requests.get(first.id)
        investigation = await uow.investigations.latest_for_incident(incident.id)
        updated = await uow.incidents.get(incident.id)
    assert persisted is not None and persisted.attempt_count == 1
    assert investigation is not None and investigation.result["root_cause"] == "node memory pressure"
    assert updated is not None and updated.classification == "infrastructure"


@pytest.mark.asyncio
async def test_same_jenkins_signature_correlates_builds_to_one_incident(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from jenkins_watchdog.application.incidents import IncidentService

    factory = uow_factory(postgres_session_factory)
    job = JenkinsJobSnapshot(
        full_name="Portal_Build_DAILY_MR_PATCH",
        display_name="Portal Build",
        url="https://jenkins/job/portal",
        job_class="org.jenkinsci.plugins.workflow.job.WorkflowJob",
        color="red",
        parent_full_name=None,
        last_build_number=12359,
        last_build_at=NOW,
    )
    builds = tuple(
        JenkinsBuildSnapshot(
            job_full_name=job.full_name,
            number=number,
            result="FAILURE",
            url=f"https://jenkins/job/portal/{number}",
            started_at=NOW + timedelta(minutes=number - 12358),
            duration_ms=120_000,
        )
        for number in (12358, 12359)
    )
    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        await uow.jenkins.upsert_jobs((job,), now=NOW)
        await uow.jenkins.upsert_builds(builds, now=NOW)
        for build in builds:
            await uow.jenkins.save_enrichment(
                JenkinsBuildEnrichment(
                    job_full_name=job.full_name,
                    number=build.number,
                    failure_classification="compilation_error",
                    failure_signature="same-compiler-error",
                    failure_summary="TypeScript compilation failed",
                    error_lines=("TS2322",),
                    log_enriched=True,
                ),
                now=NOW,
            )
        await uow.jenkins.refresh_classifications(now=NOW)
        candidates = await uow.jenkins.analysis_candidates(min_priority=0, limit=10)
        await uow.commit()

    service = IncidentService(factory)
    incidents = [await service.correlate_jenkins_build(build, now=NOW) for build in candidates]
    assert len(candidates) == 2
    assert incidents[0].id == incidents[1].id
    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        linked = await uow.jenkins.builds_for_incident(incidents[0].id)
    assert {item["build_number"] for item in linked} == {12358, 12359}


@pytest.mark.asyncio
async def test_queue_rejects_unknown_incident_and_exposes_latest_request(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    queue = InvestigationQueueService(
        uow_factory=uow_factory(postgres_session_factory),
        now=lambda: NOW,
    )
    with pytest.raises(LookupError, match="does not exist"):
        await queue.enqueue_incident(str(uuid.uuid4()), source="manual")

    incident = await seed_incident(postgres_session_factory)
    request = await queue.enqueue_incident(
        incident.id,
        source="manual-source-name-that-is-longer-than-thirty-two-characters",
        priority=500,
        requested_by="operator@example.com",
        force=True,
    )
    assert request is not None
    assert request.source == "manual-source-name-that-is-longe"
    assert request.priority == 100
    assert await queue.latest_for_incident(incident.id) == request


@pytest.mark.asyncio
async def test_failed_agent_attempt_records_scan_progress_and_stops_at_retry_limit(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    incident = await seed_incident(postgres_session_factory)
    factory = uow_factory(postgres_session_factory)
    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        scan_id = (await uow.incidents.observations(incident.id))[0].scan_id
    queue = InvestigationQueueService(uow_factory=factory, now=lambda: NOW)
    request = await queue.enqueue_incident(
        incident.id,
        source="manual",
        mode=ScanMode.DEEP,
        priority=-1,
        scan_id=scan_id,
        force=True,
    )
    assert request is not None and request.priority == 0
    events = RecordingEvents()
    worker = InvestigationWorker(
        owner="worker-failed",
        uow_factory=factory,
        reasoning=FailedInvestigationReasoning(incident),  # type: ignore[arg-type]
        queue=queue,
        automation=FailingAutomation(),  # type: ignore[arg-type]
        events=events,  # type: ignore[arg-type]
        now=lambda: NOW,
        lease_seconds=60,
        heartbeat_seconds=5,
        max_attempts=1,
    )
    completed = await worker.run_once()
    assert completed is not None
    assert completed.status is InvestigationRequestStatus.FAILED
    assert completed.error_summary == "agent model unavailable"
    assert [event_type for _, event_type, _ in events.events] == [
        "investigation_started",
        "agent_tool_call",
        "investigation_completed",
    ]


@pytest.mark.asyncio
async def test_worker_exception_requeues_with_backoff_then_has_no_immediate_work(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    incident = await seed_incident(postgres_session_factory)
    factory = uow_factory(postgres_session_factory)
    queue = InvestigationQueueService(uow_factory=factory, now=lambda: NOW)
    assert await queue.enqueue_incident(
        incident.id,
        source="manual",
        mode=ScanMode.DEEP,
        force=True,
    )
    worker = InvestigationWorker(
        owner="worker-retry",
        uow_factory=factory,
        reasoning=FailedInvestigationReasoning(incident, return_none=True),  # type: ignore[arg-type]
        queue=queue,
        automation=FailingAutomation(),  # type: ignore[arg-type]
        events=Events(),  # type: ignore[arg-type]
        now=lambda: NOW,
        lease_seconds=60,
        heartbeat_seconds=5,
        max_attempts=3,
    )
    failed = await worker.run_once()
    assert failed is not None and failed.status is InvestigationRequestStatus.QUEUED
    assert failed.next_attempt_at == NOW + timedelta(seconds=5)
    assert "reasoning returned no investigation" in (failed.error_summary or "")
    assert await worker.run_once() is None

    stop = __import__("asyncio").Event()
    stop.set()
    await worker.run_forever(stop)


@pytest.mark.asyncio
async def test_successful_analysis_survives_automation_planning_error(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    incident = await seed_incident(postgres_session_factory)
    factory = uow_factory(postgres_session_factory)
    queue = InvestigationQueueService(uow_factory=factory, now=lambda: NOW)
    assert await queue.enqueue_incident(incident.id, source="manual", force=True)
    worker = InvestigationWorker(
        owner="worker-success",
        uow_factory=factory,
        reasoning=ReasoningService(uow_factory=factory, reasoning=AgentReasoning(), now=lambda: NOW),
        queue=queue,
        automation=FailingAutomation(),  # type: ignore[arg-type]
        events=Events(),  # type: ignore[arg-type]
        now=lambda: NOW,
        lease_seconds=60,
        heartbeat_seconds=5,
    )
    completed = await worker.run_once()
    assert completed is not None and completed.status is InvestigationRequestStatus.SUCCEEDED
