from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jenkins_watchdog.application.delivery import DeliveryService
from jenkins_watchdog.application.events import EventService
from jenkins_watchdog.application.scan_service import ScanService
from jenkins_watchdog.application.types import EnqueueScan
from jenkins_watchdog.domain.model import (
    Action,
    ActionStatus,
    ActionType,
    CheckResult,
    CheckStatus,
    Confidence,
    DeliveryAttempt,
    DeliveryAttemptStatus,
    FindingObservation,
    Incident,
    Investigation,
    InvestigationStatus,
    ScanMode,
    Severity,
)
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

    async def chat(self, *, message: str, incident_id: str | None = None) -> str:
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
    container = SimpleNamespace(
        uow_factory=uow,
        notifier=notifier,
        scan_service=ScanService(uow, events=events),
        reasoning_service=reasoning or Reasoning(),
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
    assert reinvestigated.json()["id"] == investigation.id
    assert contextual_chat.json()["content"] == f"{incident.id}:why"
    assert global_chat.json()["content"] == "global:status"


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
