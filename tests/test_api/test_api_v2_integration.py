from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jenkins_watchdog.application.delivery import DeliveryService
from jenkins_watchdog.application.events import EventService
from jenkins_watchdog.application.incidents import IncidentService
from jenkins_watchdog.application.investigations import InvestigationQueueService
from jenkins_watchdog.application.scan_service import ScanService
from jenkins_watchdog.application.selection import AnalysisSelectionService
from jenkins_watchdog.application.types import EnqueueScan
from jenkins_watchdog.domain.jenkins import (
    JenkinsBuildAttribution,
    JenkinsBuildEnrichment,
    JenkinsBuildSnapshot,
    JenkinsJobSnapshot,
)
from jenkins_watchdog.domain.model import (
    Action,
    ActionStatus,
    ActionType,
    AnalysisDecision,
    AnalysisDecisionOutcome,
    CheckResult,
    CheckStatus,
    Confidence,
    DeliveryAttempt,
    DeliveryAttemptStatus,
    FindingObservation,
    Incident,
    Investigation,
    InvestigationRequest,
    InvestigationRequestStatus,
    InvestigationStatus,
    LLMCall,
    ScanMode,
    Severity,
)
from jenkins_watchdog.domain.source import SourceAttribution, SourceKind, SourceStatus
from jenkins_watchdog.entrypoints.api_v2 import router
from jenkins_watchdog.infrastructure.uow import SqlAlchemyUnitOfWork

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)


class ImmediateNotifier:
    def __init__(self) -> None:
        self.published = []

    async def publish(self, event) -> None:
        self.published.append(event)

    async def wait(self, scan_id: str, stream_id: str, *, timeout_seconds: float = 5.0) -> str:
        del scan_id, timeout_seconds
        await asyncio.sleep(0)
        return stream_id


class Reasoning:
    def __init__(self, investigation: Investigation | None = None) -> None:
        self.investigation = investigation

    async def investigate_if_needed(self, incident_id: str, *, force: bool = False):
        del incident_id, force
        return self.investigation

    async def chat(
        self,
        *,
        message: str,
        incident_id: str | None = None,
        history=(),
        on_progress=None,
    ) -> str:
        del history
        if on_progress:
            await on_progress({"type": "tool_call", "tool": "jenkins_get_build_log", "arguments": {}})
            await on_progress({"type": "tool_result", "tool": "jenkins_get_build_log", "ok": True})
        return f"{incident_id or 'global'}:{message}"


class NeverDeliver:
    async def deliver(self, action):
        raise AssertionError(f"unexpected delivery for {action.id}")


def factory_port(factory: async_sessionmaker[AsyncSession]):
    return lambda: SqlAlchemyUnitOfWork(factory)


async def seed_terminal_scan(
    factory: async_sessionmaker[AsyncSession],
    *,
    event_count: int = 2,
) -> str:
    async with SqlAlchemyUnitOfWork(factory) as uow:
        scan = await uow.scans.add(EnqueueScan(mode=ScanMode.REGULAR, categories=()))
        scan = scan.succeed(now=NOW)
        await uow.scans.save(scan)
        for sequence in range(event_count):
            await uow.events.append(scan.id, f"event_{sequence + 1}", {"index": sequence + 1}, now=NOW)
        await uow.commit()
    return scan.id


async def seed_incident(factory: async_sessionmaker[AsyncSession]) -> tuple[Incident, Investigation, Action]:
    async with SqlAlchemyUnitOfWork(factory) as uow:
        scan = await uow.scans.add(
            EnqueueScan(
                mode=ScanMode.REGULAR,
                categories=("jenkins_failed_build",),
                triggering_user_email="trigger@example.com",
            )
        )
        observation = FindingObservation(
            scan_id=scan.id,
            check_name="jenkins_failed_builds",
            rule_id="jenkins.failed.v1",
            resource_id="jenkins-job/app/MR-42",
            severity=Severity.CRITICAL,
            category="jenkins_failed_build",
            summary="compile failed",
            observed_at=NOW,
            identity_dimensions={"error_signature": "compiler"},
            evidence={"job_name": "app/MR-42", "build_number": 42},
        )
        await uow.checks.save(
            scan.id,
            CheckResult(
                scan_id=scan.id,
                check_name=observation.check_name,
                status=CheckStatus.SUCCEEDED,
                categories=frozenset({observation.category}),
                started_at=NOW,
                completed_at=NOW,
            ),
        )
        await uow.findings.add_observations(scan.id, (observation,))
        incident = Incident.open_new(
            id=str(uuid.uuid4()),
            correlation_rule_id="jenkins_error_signature",
            correlation_key="compiler",
            observation=observation,
            opened_at=NOW,
        ).associate_source({"kind": "merge_request", "confirmed": True}, now=NOW)
        await uow.incidents.save(incident)
        await uow.incidents.link_observation(incident, observation)
        investigation = Investigation(
            id=str(uuid.uuid4()),
            incident_id=incident.id,
            occurrence_id=incident.current_occurrence.id,
            status=InvestigationStatus.SUCCEEDED,
            evidence_hash="hash",
            input_version="v1",
            prompt_version="v1",
            model="model",
            confidence=Confidence.HIGH,
            usage={"total_tokens": 12},
            result={"root_cause": "compiler", "deterministic_severity": "critical"},
            created_at=NOW,
            completed_at=NOW,
        )
        await uow.investigations.save(investigation)
        action = Action(
            id=str(uuid.uuid4()),
            incident_id=incident.id,
            occurrence_id=incident.current_occurrence.id,
            action_type=ActionType.EMAIL,
            destination="ops@example.com",
            status=ActionStatus.PERMANENTLY_FAILED,
            rendered_payload={"subject": "subject", "body": "body"},
            template_version="v1",
            idempotency_key="email-key",
            external_identity="email-identity",
            attempt_count=6,
            created_at=NOW,
            updated_at=NOW,
            completed_at=NOW,
            failure_summary="SMTP 550",
        )
        await uow.actions.add(action)
        await uow.delivery_attempts.save(
            DeliveryAttempt(
                id=str(uuid.uuid4()),
                action_id=action.id,
                retry_cycle=1,
                attempt_number=6,
                status=DeliveryAttemptStatus.PERMANENT_FAILED,
                response_metadata={"smtp_code": 550},
                error_summary="SMTP 550",
                started_at=NOW,
                completed_at=NOW,
            )
        )
        await uow.scans.save(scan.succeed(now=NOW))
        await uow.commit()
    return incident, investigation, action


def make_app(
    factory: async_sessionmaker[AsyncSession],
    *,
    reasoning: Reasoning | None = None,
) -> tuple[FastAPI, SimpleNamespace]:
    app = FastAPI()
    notifier = ImmediateNotifier()
    uow = factory_port(factory)
    events = EventService(uow, notifier)
    delivery = DeliveryService(owner="api-retry", uow_factory=uow, delivery=NeverDeliver(), now=lambda: NOW)
    queue = InvestigationQueueService(uow_factory=uow, now=lambda: NOW)
    selection = AnalysisSelectionService(
        uow_factory=uow,
        reasoning=SimpleNamespace(),
        queue=queue,
        now=lambda: NOW,
    )
    container = SimpleNamespace(
        uow_factory=uow,
        notifier=notifier,
        scan_service=ScanService(uow, events=events),
        reasoning_service=reasoning or Reasoning(),
        investigation_queue=queue,
        selection_service=selection,
        incident_service=IncidentService(uow),
        make_delivery_worker=lambda owner: delivery,
    )
    app.state.container = container

    @app.middleware("http")
    async def actor(request: Request, call_next):
        if value := request.headers.get("X-Actor"):
            request.state.user = {"email": value}
        return await call_next(request)

    app.include_router(router, prefix="/api/v2")
    return app, container


def client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_scan_collection_detail_cancel_and_cursor_validation(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    terminal_id = await seed_terminal_scan(postgres_session_factory)
    app, container = make_app(postgres_session_factory)
    async with client(app) as api:
        page = await api.get("/api/v2/scans", params={"limit": 1})
        detail = await api.get(f"/api/v2/scans/{terminal_id}")
        missing = await api.get(f"/api/v2/scans/{uuid.uuid4()}")
        invalid = await api.get("/api/v2/scans", params={"cursor": "broken"})
        queued = await api.post("/api/v2/scans", json={"mode": "deep", "categories": ["k8s_node"]})
        scan_id = queued.json()["id"]
        first_cancel = await api.post(f"/api/v2/scans/{scan_id}/cancel")
        second_cancel = await api.post(f"/api/v2/scans/{scan_id}/cancel")

    assert page.status_code == 200 and len(page.json()["items"]) == 1
    assert detail.json()["status"] == "succeeded"
    assert missing.status_code == 404
    assert invalid.status_code == 422
    assert first_cancel.json()["cancel_requested"] is True
    assert second_cancel.json() == first_cancel.json()
    assert [item.type for item in container.notifier.published].count("scan_cancel_requested") == 1


@pytest.mark.asyncio
async def test_terminal_scan_reports_linked_agent_analysis_until_request_finishes(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    incident, _, _ = await seed_incident(postgres_session_factory)
    scan_id = await seed_terminal_scan(postgres_session_factory)
    request = InvestigationRequest(
        id=str(uuid.uuid4()),
        incident_id=incident.id,
        occurrence_id=incident.current_occurrence.id,
        mode=ScanMode.REGULAR,
        source="scan",
        priority=100,
        evidence_hash="analysis-evidence",
        status=InvestigationRequestStatus.QUEUED,
        scan_id=scan_id,
        created_at=NOW,
        updated_at=NOW,
        next_attempt_at=NOW,
    )
    decision = AnalysisDecision(
        id=str(uuid.uuid4()),
        incident_id=incident.id,
        occurrence_id=incident.current_occurrence.id,
        outcome=AnalysisDecisionOutcome.SELECTED,
        reason_code="critical_failure",
        reason="Critical direct evidence requires agent investigation.",
        source="scan",
        mode=ScanMode.REGULAR,
        priority=100,
        evidence_hash="analysis-evidence",
        scan_id=scan_id,
        request_id=request.id,
        created_at=NOW,
    )
    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        await uow.investigation_requests.enqueue(request)
        await uow.analysis_decisions.save(decision)
        await uow.commit()

    app, _ = make_app(postgres_session_factory)
    async with client(app) as api:
        queued = await api.get(f"/api/v2/scans/{scan_id}")

        async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
            stored = await uow.investigation_requests.get(request.id)
            assert stored is not None
            failed = stored.claim(owner="test-agent", now=NOW, lease_seconds=60).fail(
                "model request failed",
                now=NOW + timedelta(seconds=1),
            )
            await uow.investigation_requests.save(failed)
            await uow.commit()

        finished = await api.get(f"/api/v2/scans/{scan_id}")

    queued_analysis = queued.json()["analysis"]
    assert queued.json()["status"] == "succeeded"
    assert queued_analysis["status"] == "queued"
    assert queued_analysis["candidate_count"] == 1
    assert queued_analysis["selected_count"] == 1
    assert queued_analysis["active_count"] == 1
    assert queued_analysis["items"][0] == {
        "incident_id": incident.id,
        "incident_title": incident.title,
        "severity": "critical",
        "outcome": "selected",
        "reason_code": "critical_failure",
        "reason": "Critical direct evidence requires agent investigation.",
        "request_id": request.id,
        "request_status": "queued",
        "investigation_id": None,
        "investigation_status": None,
        "error_summary": None,
        "completed_at": None,
    }
    finished_analysis = finished.json()["analysis"]
    assert finished_analysis["status"] == "complete_with_issues"
    assert finished_analysis["active_count"] == 0
    assert finished_analysis["failed_count"] == 1
    assert finished_analysis["items"][0]["error_summary"] == "model request failed"


@pytest.mark.asyncio
async def test_terminal_scan_exposes_partial_agent_assessment_as_complete_with_issues(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    incident, _, _ = await seed_incident(postgres_session_factory)
    scan_id = await seed_terminal_scan(postgres_session_factory)
    completed_at = NOW + timedelta(minutes=1)
    investigation = Investigation(
        id=str(uuid.uuid4()),
        incident_id=incident.id,
        occurrence_id=incident.current_occurrence.id,
        status=InvestigationStatus.PARTIAL,
        evidence_hash="partial-evidence",
        input_version="v2",
        prompt_version="tool-agent-v1",
        model="model",
        confidence=Confidence.LOW,
        usage={"total_tokens": 12},
        result={"root_cause": "incomplete", "completion_status": "partial"},
        error_summary="exploration token limit reached",
        created_at=completed_at,
        completed_at=completed_at,
    )
    request = InvestigationRequest(
        id=str(uuid.uuid4()),
        incident_id=incident.id,
        occurrence_id=incident.current_occurrence.id,
        mode=ScanMode.REGULAR,
        source="scan",
        priority=100,
        evidence_hash="partial-evidence",
        status=InvestigationRequestStatus.SUCCEEDED,
        scan_id=scan_id,
        investigation_id=investigation.id,
        created_at=NOW,
        updated_at=completed_at,
        completed_at=completed_at,
    )
    decision = AnalysisDecision(
        id=str(uuid.uuid4()),
        incident_id=incident.id,
        occurrence_id=incident.current_occurrence.id,
        outcome=AnalysisDecisionOutcome.SELECTED,
        reason_code="critical_failure",
        reason="Critical direct evidence requires agent investigation.",
        source="scan",
        mode=ScanMode.REGULAR,
        priority=100,
        evidence_hash="partial-evidence",
        scan_id=scan_id,
        request_id=request.id,
        created_at=NOW,
    )
    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        await uow.investigations.save(investigation)
        await uow.investigation_requests.enqueue(request)
        await uow.analysis_decisions.save(decision)
        await uow.commit()

    app, _ = make_app(postgres_session_factory)
    async with client(app) as api:
        response = await api.get(f"/api/v2/scans/{scan_id}")

    analysis = response.json()["analysis"]
    assert analysis["status"] == "complete_with_issues"
    assert analysis["succeeded_count"] == 0
    assert analysis["partial_count"] == 1
    assert analysis["failed_count"] == 0
    assert analysis["items"][0]["investigation_status"] == "partial"
    assert analysis["items"][0]["error_summary"] == "exploration token limit reached"


@pytest.mark.asyncio
async def test_terminal_scan_reports_all_budget_deferred_as_a_distinct_analysis_outcome(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    incident, _, _ = await seed_incident(postgres_session_factory)
    scan_id = await seed_terminal_scan(postgres_session_factory)
    reset_at = NOW + timedelta(hours=12)
    decision = AnalysisDecision(
        id=str(uuid.uuid4()),
        incident_id=incident.id,
        occurrence_id=incident.current_occurrence.id,
        outcome=AnalysisDecisionOutcome.BUDGET_DEFERRED,
        reason_code="daily_budget_exhausted",
        reason="automatic daily LLM token budget exhausted",
        source="scan",
        mode=ScanMode.REGULAR,
        priority=100,
        evidence_hash="analysis-evidence",
        scan_id=scan_id,
        created_at=NOW,
        metadata={
            "budget_metric": "cost_usd",
            "budget_limit_tokens": 300_000,
            "budget_spent_tokens": 989_116,
            "budget_projected_tokens": 1_013_116,
            "budget_limit_usd": 10.50,
            "budget_spent_usd": 10.42,
            "budget_projected_usd": 11.02,
        },
    )
    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        await uow.analysis_decisions.save(decision)
        await uow.commit()

    app, _ = make_app(postgres_session_factory)
    async with client(app) as api:
        response = await api.get(f"/api/v2/scans/{scan_id}")

    analysis = response.json()["analysis"]
    assert analysis["status"] == "budget_deferred"
    assert analysis["candidate_count"] == 1
    assert analysis["selected_count"] == 0
    assert analysis["budget_deferred_count"] == 1
    assert analysis["budget_metric"] == "cost_usd"
    assert analysis["budget_reset_at"] == reset_at.isoformat().replace("+00:00", "Z")
    assert analysis["budget_limit_tokens"] == 300_000
    assert analysis["budget_spent_tokens"] == 989_116
    assert analysis["budget_projected_tokens"] == 1_013_116
    assert analysis["budget_limit_usd"] == 10.50
    assert analysis["budget_spent_usd"] == 10.42
    assert analysis["budget_projected_usd"] == 11.02


@pytest.mark.asyncio
async def test_sse_replays_after_last_event_id_for_multiple_viewers(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    scan_id = await seed_terminal_scan(postgres_session_factory, event_count=3)
    app, _ = make_app(postgres_session_factory)

    async def view() -> str:
        async with client(app) as api:
            response = await api.get(
                f"/api/v2/scans/{scan_id}/events",
                headers={"Last-Event-ID": "1"},
            )
            assert response.status_code == 200
            return response.text

    first, second = await asyncio.gather(view(), view())

    for body in (first, second):
        assert "id: 1" not in body
        assert "id: 2" in body
        assert '"sequence":2' in body
        assert "id: 3" in body


@pytest.mark.asyncio
async def test_incident_detail_filters_suppression_reasoning_and_chat(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    incident, investigation, _ = await seed_incident(postgres_session_factory)
    app, _ = make_app(postgres_session_factory, reasoning=Reasoning(investigation))
    async with client(app) as api:
        page = await api.get("/api/v2/incidents", params={"status": "open", "severity": "critical"})
        invalid_filter = await api.get("/api/v2/incidents", params={"status": "invalid"})
        detail = await api.get(f"/api/v2/incidents/{incident.id}")
        unauthenticated = await api.post(
            f"/api/v2/incidents/{incident.id}/suppress",
            json={"reason": "maintenance"},
        )
        suppressed = await api.post(
            f"/api/v2/incidents/{incident.id}/suppress",
            headers={"X-Actor": "operator@example.com"},
            json={"reason": "maintenance"},
        )
        unsuppressed = await api.post(
            f"/api/v2/incidents/{incident.id}/unsuppress",
            headers={"X-Actor": "operator@example.com"},
        )
        reinvestigated = await api.post(f"/api/v2/incidents/{incident.id}/reinvestigate")
        contextual_chat = await api.post(
            f"/api/v2/incidents/{incident.id}/chat",
            json={"message": "why"},
        )
        global_chat = await api.post("/api/v2/chat", json={"message": "status"})

    assert page.json()["items"][0]["id"] == incident.id
    assert invalid_filter.status_code == 422
    assert detail.json()["observations"][0]["summary"] == "compile failed"
    assert detail.json()["occurrences"][0]["responsible_checks"] == ["jenkins_failed_builds"]
    assert detail.json()["latest_investigation"]["confidence"] == "high"
    assert unauthenticated.status_code == 401
    assert suppressed.json()["suppressed_by"] == "operator@example.com"
    assert unsuppressed.json()["status"] == "open"
    assert reinvestigated.status_code == 202
    assert reinvestigated.json()["incident_id"] == incident.id
    assert reinvestigated.json()["status"] == "queued"
    assert contextual_chat.json()["content"] == f"{incident.id}:why"
    assert global_chat.json()["content"] == "global:status"


@pytest.mark.asyncio
async def test_chat_stream_exposes_tool_activity_and_final_answer(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app, _ = make_app(postgres_session_factory)
    async with client(app) as api:
        response = await api.post("/api/v2/chat/stream", json={"message": "inspect build"})

    assert response.status_code == 200
    assert "event: tool_call" in response.text
    assert "jenkins_get_build_log" in response.text
    assert "event: tool_result" in response.text
    assert "event: message" in response.text
    assert "global:inspect build" in response.text


@pytest.mark.asyncio
async def test_chat_rejects_calls_when_daily_cost_budget_is_exhausted(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        await uow.llm_calls.save_many(
            (
                LLMCall(
                    id=str(uuid.uuid4()),
                    purpose="chat",
                    model="test-model",
                    prompt_tokens=100,
                    completion_tokens=25,
                    cache_read_input_tokens=0,
                    cache_creation_input_tokens=0,
                    total_tokens=125,
                    # At or above llm_daily_cost_budget_usd, so the chat reservation tips it over.
                    estimated_cost_usd=Decimal("30.00"),
                    cost_source="test",
                    created_at=NOW,
                ),
            )
        )
        await uow.commit()
    app, _ = make_app(postgres_session_factory)

    async with client(app) as api:
        response = await api.post("/api/v2/chat", json={"message": "status"})

    assert response.status_code == 429
    assert response.json()["detail"]["code"] == "llm_budget_exhausted"
    assert "daily LLM cost budget exhausted" in response.json()["detail"]["message"]


@pytest.mark.asyncio
async def test_analyze_build_creates_incident_and_durable_request(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    job = JenkinsJobSnapshot(
        full_name="Portal_Build_DAILY_MR_PATCH",
        display_name="Portal Build",
        url="https://jenkins/job/portal",
        job_class="org.jenkinsci.plugins.workflow.job.WorkflowJob",
        color="red",
        parent_full_name=None,
        last_build_number=12358,
        last_build_at=NOW,
    )
    snapshot = JenkinsBuildSnapshot(
        job_full_name=job.full_name,
        number=12358,
        result="FAILURE",
        url="https://jenkins/job/portal/12358",
        started_at=NOW,
        duration_ms=120_000,
    )
    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        await uow.jenkins.upsert_jobs((job,), now=NOW)
        await uow.jenkins.upsert_builds((snapshot,), now=NOW)
        await uow.jenkins.save_enrichment(
            JenkinsBuildEnrichment(
                job_full_name=job.full_name,
                number=12358,
                attribution=JenkinsBuildAttribution(
                    job_full_name=job.full_name,
                    number=12358,
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
                failure_classification="compilation_error",
                failure_signature="typescript-error",
                failure_summary="TypeScript compilation failed",
                error_lines=("TS2322",),
                log_enriched=True,
            ),
            now=NOW,
        )
        await uow.jenkins.refresh_classifications(now=NOW)
        page = await uow.jenkins.failure_builds(since=NOW - timedelta(hours=1), limit=1)
        build_id = page.items[0]["id"]
        assert page.total_count == 1
        await uow.commit()

    app, _ = make_app(postgres_session_factory)
    async with client(app) as api:
        queued = await api.post(
            f"/api/v2/jenkins/builds/{build_id}/analyze",
            json={"mode": "deep"},
            headers={"X-Actor": "operator@example.com"},
        )
        detail = await api.get(f"/api/v2/jenkins/builds/{build_id}")

    assert queued.status_code == 202
    assert queued.json()["mode"] == "deep"
    assert queued.json()["status"] == "queued"
    assert queued.json()["requested_by"] == "operator@example.com"
    assert detail.json()["incident_id"] == queued.json()["incident_id"]
    assert detail.json()["investigation_request"]["id"] == queued.json()["id"]
    assert detail.json()["source_kind"] == "change_request"
    assert detail.json()["source_status"] == "verified"
    assert detail.json()["source_profile_id"] == "portal-backend"
    assert detail.json()["repository"] == "Portal/Backend"
    assert detail.json()["change_number"] == "6836"
    assert detail.json()["source_verified_at"] == NOW.isoformat().replace("+00:00", "Z")
    assert detail.json()["incident"]["source"]["kind"] == "merge_request"
    assert detail.json()["incident"]["source"]["verified"] is True


@pytest.mark.asyncio
async def test_action_collection_detail_and_manual_retry(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    incident, _, action = await seed_incident(postgres_session_factory)
    app, _ = make_app(postgres_session_factory)
    async with client(app) as api:
        page = await api.get(
            "/api/v2/actions",
            params={"status": "permanently_failed", "incident_id": incident.id},
        )
        invalid = await api.get("/api/v2/actions", params={"incident_id": "not-a-uuid"})
        detail = await api.get(f"/api/v2/actions/{action.id}")
        retried = await api.post(f"/api/v2/actions/{action.id}/retry")
        conflict = await api.post(f"/api/v2/actions/{action.id}/retry")
        missing = await api.get(f"/api/v2/actions/{uuid.uuid4()}")

    assert page.json()["items"][0]["id"] == action.id
    assert invalid.status_code == 422
    assert detail.json()["attempts"][0]["response_metadata"] == {"smtp_code": 550}
    assert retried.json()["status"] == "pending"
    assert retried.json()["retry_cycle"] == 2
    assert conflict.status_code == 409
    assert missing.status_code == 404
