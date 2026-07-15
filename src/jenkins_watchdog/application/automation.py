"""Deterministic planning of immutable external actions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

from jenkins_watchdog.application.ports import PayloadRenderer, UnitOfWorkFactory
from jenkins_watchdog.application.routing import resolve_routing
from jenkins_watchdog.domain.model import (
    Action,
    ActionStatus,
    ActionType,
    Confidence,
    IncidentStatus,
    InvestigationStatus,
    Severity,
)
from jenkins_watchdog.domain.routing import RoutingConfig


@dataclass(frozen=True, slots=True)
class IntegrationPolicy:
    email_enabled: bool = False
    jira_enabled: bool = False
    github_enabled: bool = False
    gitlab_enabled: bool = False
    jira_project: str = "CI"


class AutomationService:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        routing: RoutingConfig,
        renderer: PayloadRenderer,
        policy: IntegrationPolicy,
        now: Callable[[], datetime],
    ) -> None:
        self._uow_factory = uow_factory
        self._routing = routing
        self._renderer = renderer
        self._policy = policy
        self._now = now

    async def plan(self, incident_id: str) -> tuple[Action, ...]:
        async with self._uow_factory() as uow:
            incident = await uow.incidents.get(incident_id)
            if incident is None:
                raise LookupError(f"incident {incident_id} does not exist")
            observations = await uow.incidents.observations(incident_id)
            investigation = await uow.investigations.latest_for_incident(incident_id)
            trigger_email = None
            if observations:
                scan = await uow.scans.get(observations[-1].scan_id)
                trigger_email = scan.triggering_user_email if scan else None

        if (
            incident.status is not IncidentStatus.OPEN
            or not incident.current_occurrence.active
            or incident.severity not in {Severity.WARNING, Severity.CRITICAL}
        ):
            return ()

        job_name = _latest_evidence(observations, "job_name")
        decision = resolve_routing(
            config=self._routing,
            incident_source=incident.source,
            job_name=str(job_name) if job_name else None,
            triggering_user_email=trigger_email,
        )
        confidence = (
            investigation.confidence
            if investigation is not None and investigation.confidence is not None
            else Confidence.LOW
        )
        confident = (
            investigation is not None
            and investigation.status is InvestigationStatus.SUCCEEDED
            and investigation.occurrence_id == incident.current_occurrence.id
            and confidence in {Confidence.MEDIUM, Confidence.HIGH}
        )
        result = dict(investigation.result) if investigation else {}
        context = {
            "incident_id": incident.id,
            "occurrence_number": incident.current_occurrence.number,
            "title": incident.title,
            "severity": incident.severity.value,
            "status": incident.status.value,
            "root_cause": str(result.get("root_cause", "Investigation unavailable")),
            "impact": str(result.get("impact", "Unknown")),
            "suggested_fix": str(result.get("suggested_fix", "Review the linked evidence.")),
            "confidence": confidence.value,
            "source": decision.source,
        }
        now = self._now()
        planned: list[Action] = []
        source_kind = decision.source.get("kind")
        if (
            source_kind == "merge_request"
            and confident
            and bool(decision.source.get("verified"))
            and bool(decision.source.get("allow_mr_comments"))
        ):
            provider = str(decision.source.get("provider", ""))
            enabled = {
                "github": self._policy.github_enabled,
                "gitlab": self._policy.gitlab_enabled,
            }.get(provider, False)
            if enabled:
                build = _latest_evidence(observations, "build_number") or _latest_evidence(observations, "latest_build")
                destination = f"{provider}:{decision.source['repository']}:{decision.source['change_number']}"
                planned.append(
                    _action(
                        incident_id=incident.id,
                        occurrence_id=incident.current_occurrence.id,
                        action_type=(ActionType.GITHUB_COMMENT if provider == "github" else ActionType.GITLAB_COMMENT),
                        destination=destination,
                        payload=self._renderer.render("mr_comment", context),
                        template_version=self._renderer.template_version,
                        idempotency_key=(
                            f"mr:{provider}:{decision.source['repository']}:"
                            f"{decision.source['change_number']}:{build or 'unknown'}:"
                            f"{self._renderer.template_version}"
                        ),
                        external_identity=destination,
                        now=now,
                    )
                )
        elif source_kind == "infrastructure" and confident and self._policy.jira_enabled:
            action_type = ActionType.JIRA_UPDATE if incident.current_occurrence.number > 1 else ActionType.JIRA_CREATE
            planned.append(
                _action(
                    incident_id=incident.id,
                    occurrence_id=incident.current_occurrence.id,
                    action_type=action_type,
                    destination=self._policy.jira_project,
                    payload=self._renderer.render(action_type.value, context),
                    template_version=self._renderer.template_version,
                    idempotency_key=(
                        f"jira:{'update' if action_type is ActionType.JIRA_UPDATE else 'create'}:"
                        f"{incident.id}"
                        + (f":{incident.current_occurrence.number}" if action_type is ActionType.JIRA_UPDATE else "")
                    ),
                    external_identity=f"jira:incident:{incident.id}",
                    now=now,
                )
            )

        if self._policy.email_enabled:
            bucket = (
                f"reopen-{incident.current_occurrence.number}"
                if incident.current_occurrence.number > 1
                else str(int(now.timestamp()) // (6 * 60 * 60))
            )
            for recipient in decision.recipients:
                email_context = {**context, "recipient": recipient}
                planned.append(
                    _action(
                        incident_id=incident.id,
                        occurrence_id=incident.current_occurrence.id,
                        action_type=ActionType.EMAIL,
                        destination=recipient,
                        payload=self._renderer.render("email", email_context),
                        template_version=self._renderer.template_version,
                        idempotency_key=(
                            f"email:{incident.id}:{incident.current_occurrence.id}:{recipient}:"
                            f"{self._renderer.template_version}:{bucket}"
                        ),
                        external_identity=f"email:{incident.id}:{recipient}",
                        now=now,
                    )
                )

        persisted: list[Action] = []
        async with self._uow_factory() as uow:
            for action in planned:
                persisted.append(await uow.actions.add(action))
            await uow.commit()
        return tuple(persisted)


def _action(
    *,
    incident_id: str,
    occurrence_id: str,
    action_type: ActionType,
    destination: str,
    payload: dict[str, Any],
    template_version: str,
    idempotency_key: str,
    external_identity: str,
    now: datetime,
) -> Action:
    return Action(
        id=str(uuid4()),
        incident_id=incident_id,
        occurrence_id=occurrence_id,
        action_type=action_type,
        destination=destination,
        status=ActionStatus.PENDING,
        rendered_payload=payload,
        template_version=template_version,
        idempotency_key=idempotency_key,
        external_identity=external_identity,
        created_at=now,
        updated_at=now,
        next_attempt_at=now,
    )


def _latest_evidence(observations: tuple[Any, ...], name: str) -> Any:
    for observation in reversed(observations):
        value = observation.evidence.get(name)
        if value not in (None, ""):
            return value
    return None
