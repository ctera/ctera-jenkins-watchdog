from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jenkins_watchdog.application.automation import AutomationService, IntegrationPolicy
from jenkins_watchdog.application.delivery import DeliveryError, DeliveryService
from jenkins_watchdog.application.types import EnqueueScan
from jenkins_watchdog.domain.model import (
    Action,
    ActionStatus,
    ActionType,
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
from jenkins_watchdog.domain.routing import RoutingConfig
from jenkins_watchdog.infrastructure.templates import FilePayloadRenderer
from jenkins_watchdog.infrastructure.uow import SqlAlchemyUnitOfWork

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.value = NOW

    def __call__(self) -> datetime:
        return self.value


class AlwaysRetry:
    def __init__(self) -> None:
        self.calls = 0

    async def deliver(self, action: Action):
        del action
        self.calls += 1
        raise DeliveryError("HTTP 503", retryable=True, metadata={"status_code": 503})


class Succeed:
    async def deliver(self, action: Action):
        return {
            "external_reference": f"sent:{action.id}",
            "metadata": {"status_code": 201},
        }


class PermanentFailure:
    async def deliver(self, action: Action):
        del action
        raise DeliveryError("HTTP 400", retryable=False, metadata={"status_code": 400})


class UnexpectedFailure:
    async def deliver(self, action: Action):
        del action
        raise RuntimeError("credential should not be persisted")


async def seed_incident(
    factory: async_sessionmaker[AsyncSession],
    *,
    source: dict,
    confidence: Confidence = Confidence.MEDIUM,
    triggering_email: str | None = "trigger@example.com",
    severity: Severity = Severity.WARNING,
) -> tuple[Incident, FindingObservation]:
    async with SqlAlchemyUnitOfWork(factory) as uow:
        scan = await uow.scans.add(
            EnqueueScan(
                mode=ScanMode.REGULAR,
                categories=("jenkins_failed_build",),
                triggering_user_email=triggering_email,
            )
        )
        observation = FindingObservation(
            scan_id=scan.id,
            check_name="jenkins_failed_builds",
            rule_id="jenkins.failed_build.v1",
            resource_id=f"jenkins-job/app/MR-42/{uuid.uuid4()}",
            severity=severity,
            category="jenkins_failed_build",
            summary="build failed",
            observed_at=NOW,
            evidence={"job_name": "app/MR-42", "build_number": 123},
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
            correlation_rule_id="stable_finding",
            correlation_key=observation.stable_identity,
            observation=observation,
            opened_at=NOW,
        ).associate_source(source, now=NOW)
        await uow.incidents.save(incident)
        await uow.incidents.link_observation(incident, observation)
        await uow.investigations.save(
            Investigation(
                id=str(uuid.uuid4()),
                incident_id=incident.id,
                occurrence_id=incident.current_occurrence.id,
                status=InvestigationStatus.SUCCEEDED,
                evidence_hash="hash",
                input_version="v1",
                prompt_version="v1",
                model="model",
                confidence=confidence,
                usage={"total_tokens": 10},
                result={
                    "root_cause": "compiler",
                    "impact": "build blocked",
                    "suggested_fix": "fix compile error",
                    "actionability": "actionable",
                    "classification": source.get("kind", "unknown"),
                    "priority": "warning",
                    "deterministic_severity": "warning",
                },
                created_at=NOW,
                completed_at=NOW,
            )
        )
        await uow.scans.save(scan.succeed(now=NOW))
        await uow.commit()
    return incident, observation


def routing(recipients: tuple[str, ...] = ("fallback@example.com",)) -> RoutingConfig:
    return RoutingConfig(version=1, teams=(), routes=(), global_fallback_recipients=recipients)


async def seed_action(factory: async_sessionmaker[AsyncSession], *, key: str | None = None) -> Action:
    incident, _ = await seed_incident(factory, source={"kind": "unknown", "confirmed": False})
    action = Action(
        id=str(uuid.uuid4()),
        incident_id=incident.id,
        occurrence_id=incident.current_occurrence.id,
        action_type=ActionType.EMAIL,
        destination="fallback@example.com",
        status=ActionStatus.PENDING,
        rendered_payload={"subject": "subject", "body": "body"},
        template_version="v1",
        idempotency_key=key or str(uuid.uuid4()),
        external_identity=f"email:{incident.id}",
        created_at=NOW,
        updated_at=NOW,
        next_attempt_at=NOW,
    )
    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.actions.add(action)
        await uow.commit()
    return action


@pytest.mark.asyncio
async def test_confirmed_mr_plans_per_build_comment_and_email_idempotently(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    incident, _ = await seed_incident(
        postgres_session_factory,
        source={
            "kind": "merge_request",
            "confirmed": True,
            "provider": "github",
            "repository": "ctera/app",
            "change_number": "42",
        },
    )
    service = AutomationService(
        uow_factory=lambda: SqlAlchemyUnitOfWork(postgres_session_factory),
        routing=routing(),
        renderer=FilePayloadRenderer("templates/automation"),
        policy=IntegrationPolicy(email_enabled=True, github_enabled=True),
        now=lambda: NOW,
    )

    first = await service.plan(incident.id)
    second = await service.plan(incident.id)

    assert {item.action_type for item in first} == {ActionType.GITHUB_COMMENT, ActionType.EMAIL}
    comment = next(item for item in first if item.action_type is ActionType.GITHUB_COMMENT)
    assert ":123:v1" in comment.idempotency_key
    assert {item.id for item in second} == {item.id for item in first}


@pytest.mark.asyncio
async def test_low_confidence_blocks_jira_but_not_email(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    incident, _ = await seed_incident(
        postgres_session_factory,
        source={"kind": "infrastructure", "confirmed": True},
        confidence=Confidence.LOW,
    )
    service = AutomationService(
        uow_factory=lambda: SqlAlchemyUnitOfWork(postgres_session_factory),
        routing=routing(),
        renderer=FilePayloadRenderer("templates/automation"),
        policy=IntegrationPolicy(email_enabled=True, jira_enabled=True),
        now=lambda: NOW,
    )

    actions = await service.plan(incident.id)

    assert [item.action_type for item in actions] == [ActionType.EMAIL]


@pytest.mark.asyncio
async def test_high_confidence_infrastructure_plans_jira_create_and_email(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    incident, _ = await seed_incident(
        postgres_session_factory,
        source={"kind": "infrastructure", "confirmed": True},
        confidence=Confidence.HIGH,
    )
    service = AutomationService(
        uow_factory=lambda: SqlAlchemyUnitOfWork(postgres_session_factory),
        routing=routing(),
        renderer=FilePayloadRenderer("templates/automation"),
        policy=IntegrationPolicy(email_enabled=True, jira_enabled=True, jira_project="OPS"),
        now=lambda: NOW,
    )

    actions = await service.plan(incident.id)

    assert {item.action_type for item in actions} == {ActionType.JIRA_CREATE, ActionType.EMAIL}
    jira = next(item for item in actions if item.action_type is ActionType.JIRA_CREATE)
    assert jira.destination == "OPS"
    assert jira.idempotency_key == f"jira:create:{incident.id}"


@pytest.mark.asyncio
async def test_reopened_infrastructure_plans_jira_update_and_bypasses_email_bucket(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    incident, observation = await seed_incident(
        postgres_session_factory,
        source={"kind": "infrastructure", "confirmed": True},
        confidence=Confidence.HIGH,
    )
    reopened = incident.reconcile_after_scan(
        selected_checks=frozenset({observation.check_name}),
        successful_checks=frozenset({observation.check_name}),
        reconciled_at=NOW,
    ).observe(observation)
    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        await uow.incidents.save(reopened)
        await uow.investigations.save(
            Investigation(
                id=str(uuid.uuid4()),
                incident_id=reopened.id,
                occurrence_id=reopened.current_occurrence.id,
                status=InvestigationStatus.SUCCEEDED,
                evidence_hash="reopened-hash",
                input_version="v1",
                prompt_version="v1",
                model="model",
                confidence=Confidence.HIGH,
                usage={"total_tokens": 10},
                result={"root_cause": "recurrence", "impact": "blocked", "suggested_fix": "repair"},
                created_at=NOW,
                completed_at=NOW,
            )
        )
        await uow.commit()
    service = AutomationService(
        uow_factory=lambda: SqlAlchemyUnitOfWork(postgres_session_factory),
        routing=routing(),
        renderer=FilePayloadRenderer("templates/automation"),
        policy=IntegrationPolicy(email_enabled=True, jira_enabled=True),
        now=lambda: NOW,
    )

    actions = await service.plan(incident.id)

    jira = next(item for item in actions if item.action_type is ActionType.JIRA_UPDATE)
    email = next(item for item in actions if item.action_type is ActionType.EMAIL)
    assert jira.idempotency_key.endswith(":2")
    assert email.idempotency_key.endswith(":reopen-2")


@pytest.mark.asyncio
async def test_gitlab_source_routes_to_gitlab_comment_and_low_severity_or_suppression_blocks_all(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    incident, _ = await seed_incident(
        postgres_session_factory,
        source={
            "kind": "merge_request",
            "confirmed": True,
            "provider": "gitlab",
            "repository": "ctera/app",
            "change_number": "42",
        },
        confidence=Confidence.HIGH,
    )
    service = AutomationService(
        uow_factory=lambda: SqlAlchemyUnitOfWork(postgres_session_factory),
        routing=routing(),
        renderer=FilePayloadRenderer("templates/automation"),
        policy=IntegrationPolicy(gitlab_enabled=True),
        now=lambda: NOW,
    )
    [comment] = await service.plan(incident.id)
    assert comment.action_type is ActionType.GITLAB_COMMENT

    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        current = await uow.incidents.get(incident.id)
        assert current is not None
        await uow.incidents.save(current.suppress(reason="maintenance", actor="operator", suppressed_at=NOW))
        await uow.commit()
    assert await service.plan(incident.id) == ()

    low, _ = await seed_incident(
        postgres_session_factory,
        source={"kind": "unknown", "confirmed": False},
        severity=Severity.LOW,
    )
    email_service = AutomationService(
        uow_factory=lambda: SqlAlchemyUnitOfWork(postgres_session_factory),
        routing=routing(),
        renderer=FilePayloadRenderer("templates/automation"),
        policy=IntegrationPolicy(email_enabled=True),
        now=lambda: NOW,
    )
    assert await email_service.plan(low.id) == ()


@pytest.mark.asyncio
async def test_automation_rejects_unknown_incident(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = AutomationService(
        uow_factory=lambda: SqlAlchemyUnitOfWork(postgres_session_factory),
        routing=routing(),
        renderer=FilePayloadRenderer("templates/automation"),
        policy=IntegrationPolicy(),
        now=lambda: NOW,
    )

    with pytest.raises(LookupError):
        await service.plan(str(uuid.uuid4()))


@pytest.mark.asyncio
async def test_stale_investigation_and_unsupported_scm_provider_do_not_create_provider_actions(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    incident, observation = await seed_incident(
        postgres_session_factory,
        source={
            "kind": "merge_request",
            "confirmed": True,
            "provider": "bitbucket",
            "repository": "ctera/app",
            "change_number": "42",
        },
        confidence=Confidence.HIGH,
    )
    reopened = incident.reconcile_after_scan(
        selected_checks=frozenset({observation.check_name}),
        successful_checks=frozenset({observation.check_name}),
        reconciled_at=NOW,
    ).observe(observation)
    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        await uow.incidents.save(reopened)
        await uow.commit()
    service = AutomationService(
        uow_factory=lambda: SqlAlchemyUnitOfWork(postgres_session_factory),
        routing=routing(),
        renderer=FilePayloadRenderer("templates/automation"),
        policy=IntegrationPolicy(email_enabled=True, github_enabled=True, gitlab_enabled=True),
        now=lambda: NOW,
    )

    actions = await service.plan(incident.id)

    assert [item.action_type for item in actions] == [ActionType.EMAIL]


@pytest.mark.asyncio
async def test_delivery_exhausts_six_calls_and_manual_retry_keeps_history_and_identity(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    incident, _ = await seed_incident(
        postgres_session_factory,
        source={"kind": "unknown", "confirmed": False},
    )
    action = Action(
        id=str(uuid.uuid4()),
        incident_id=incident.id,
        occurrence_id=incident.current_occurrence.id,
        action_type=ActionType.EMAIL,
        destination="fallback@example.com",
        status=ActionStatus.PENDING,
        rendered_payload={"subject": "subject", "body": "body"},
        template_version="v1",
        idempotency_key="email-key",
        external_identity="email-identity",
        created_at=NOW,
        updated_at=NOW,
        next_attempt_at=NOW,
    )
    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        await uow.actions.add(action)
        await uow.commit()

    clock = Clock()
    adapter = AlwaysRetry()
    service = DeliveryService(
        owner="worker",
        uow_factory=lambda: SqlAlchemyUnitOfWork(postgres_session_factory),
        delivery=adapter,
        now=clock,
    )
    for _ in range(6):
        assert await service.run_once()
        async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
            current = await uow.actions.get(action.id)
        assert current is not None
        if current.next_attempt_at:
            clock.value = current.next_attempt_at

    assert adapter.calls == 6
    assert current.status == ActionStatus.PERMANENTLY_FAILED
    original_key = current.idempotency_key
    original_identity = current.external_identity

    retried = await service.manual_retry(action.id)
    assert retried.retry_cycle == 2
    assert retried.idempotency_key == original_key
    assert retried.external_identity == original_identity
    success = DeliveryService(
        owner="worker",
        uow_factory=lambda: SqlAlchemyUnitOfWork(postgres_session_factory),
        delivery=Succeed(),
        now=clock,
    )
    assert await success.run_once()

    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        completed = await uow.actions.get(action.id)
        attempts = await uow.delivery_attempts.for_action(action.id)
    assert completed is not None and completed.status == ActionStatus.SUCCEEDED
    assert len(attempts) == 7
    assert attempts[-1].retry_cycle == 2
    assert attempts[-1].attempt_number == 1


@pytest.mark.asyncio
async def test_delivery_no_work_invalid_manual_retry_and_stopped_loop(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = DeliveryService(
        owner="worker",
        uow_factory=lambda: SqlAlchemyUnitOfWork(postgres_session_factory),
        delivery=Succeed(),
        now=lambda: NOW,
        poll_interval_seconds=0.001,
    )
    assert not await service.run_once()
    with pytest.raises(LookupError):
        await service.manual_retry(str(uuid.uuid4()))

    pending = await seed_action(postgres_session_factory)
    with pytest.raises(ValueError, match="permanently failed"):
        await service.manual_retry(pending.id)

    stop = asyncio.Event()
    stop.set()
    await service.run_forever(stop)


@pytest.mark.asyncio
async def test_delivery_records_permanent_and_sanitized_unexpected_failures(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    permanent = await seed_action(postgres_session_factory, key="permanent")
    permanent_service = DeliveryService(
        owner="worker",
        uow_factory=lambda: SqlAlchemyUnitOfWork(postgres_session_factory),
        delivery=PermanentFailure(),
        now=lambda: NOW,
    )
    assert await permanent_service.run_once()
    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        saved = await uow.actions.get(permanent.id)
        attempts = await uow.delivery_attempts.for_action(permanent.id)
    assert saved is not None and saved.status is ActionStatus.PERMANENTLY_FAILED
    assert attempts[0].response_metadata == {"status_code": 400}

    unexpected = await seed_action(postgres_session_factory, key="unexpected")
    unexpected_service = DeliveryService(
        owner="worker",
        uow_factory=lambda: SqlAlchemyUnitOfWork(postgres_session_factory),
        delivery=UnexpectedFailure(),
        now=lambda: NOW,
    )
    assert await unexpected_service.run_once()
    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        saved = await uow.actions.get(unexpected.id)
    assert saved is not None
    assert saved.status is ActionStatus.RETRY_SCHEDULED
    assert "credential" not in (saved.failure_summary or "")


@pytest.mark.asyncio
async def test_delivery_heartbeat_rejects_another_workers_lease(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    action = await seed_action(postgres_session_factory)
    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        claimed = await uow.actions.claim(owner="another-worker", now=NOW, lease_seconds=60)
        await uow.commit()
    assert claimed is not None and claimed.id == action.id

    service = DeliveryService(
        owner="worker",
        uow_factory=lambda: SqlAlchemyUnitOfWork(postgres_session_factory),
        delivery=Succeed(),
        now=lambda: NOW,
        heartbeat_seconds=0,
    )
    with pytest.raises(RuntimeError, match="lost action lease"):
        await service._heartbeat(action.id)
