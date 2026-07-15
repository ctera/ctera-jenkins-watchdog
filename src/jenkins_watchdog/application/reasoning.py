"""Reasoning orchestration and deterministic reinvestigation policy."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from jenkins_watchdog.application.ports import ReasoningPort, ReasoningProgress, UnitOfWorkFactory
from jenkins_watchdog.application.types import ChatResult
from jenkins_watchdog.domain.model import (
    FindingObservation,
    Incident,
    Investigation,
    InvestigationStatus,
    ScanMode,
    Severity,
)
from jenkins_watchdog.domain.serialization import to_primitive

INVESTIGATION_MAX_AGE = timedelta(hours=24)


class ReasoningService:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        reasoning: ReasoningPort,
        now: Callable[[], datetime],
    ) -> None:
        self._uow_factory = uow_factory
        self._reasoning = reasoning
        self._now = now

    async def investigate_if_needed(
        self,
        incident_id: str,
        *,
        force: bool = False,
        mode: ScanMode = ScanMode.REGULAR,
        on_progress: ReasoningProgress | None = None,
    ) -> Investigation | None:
        async with self._uow_factory() as uow:
            incident = await uow.incidents.get(incident_id)
            if incident is None:
                raise LookupError(f"incident {incident_id} does not exist")
            stored_observations = await uow.incidents.observations(incident_id)
            builds = await uow.jenkins.builds_for_incident(incident_id)
            latest = await uow.investigations.latest_for_incident(incident_id)
        observations = stored_observations + jenkins_build_observations(builds)
        evidence_hash = evidence_digest(observations)
        if not force and not should_reinvestigate(
            incident=incident,
            latest=latest,
            evidence_hash=evidence_hash,
            now=self._now(),
        ):
            return latest

        if builds or mode is ScanMode.DEEP or on_progress is not None:
            investigation = await self._reasoning.investigate(
                incident,
                observations,
                context={"jenkins_builds": list(builds), "evidence_hash": evidence_hash},
                mode=mode,
                on_progress=on_progress,
            )
        else:
            investigation = await self._reasoning.investigate(incident, observations)
        async with self._uow_factory() as uow:
            await uow.investigations.save(investigation)
            if investigation.status is InvestigationStatus.SUCCEEDED:
                result = investigation.result
                incident = incident.apply_triage(
                    actionability=str(result.get("actionability", "unknown")),
                    classification=str(result.get("classification", "unknown")),
                    priority=str(result.get("priority", incident.severity.value)),
                    now=investigation.completed_at or self._now(),
                )
                await uow.incidents.save(incident)
            await uow.commit()
        return investigation

    async def needs_investigation(self, incident_id: str) -> bool:
        async with self._uow_factory() as uow:
            incident = await uow.incidents.get(incident_id)
            if incident is None:
                raise LookupError(f"incident {incident_id} does not exist")
            stored_observations = await uow.incidents.observations(incident_id)
            builds = await uow.jenkins.builds_for_incident(incident_id)
            latest = await uow.investigations.latest_for_incident(incident_id)
        observations = stored_observations + jenkins_build_observations(builds)
        return should_reinvestigate(
            incident=incident,
            latest=latest,
            evidence_hash=evidence_digest(observations),
            now=self._now(),
        )

    async def chat(
        self,
        *,
        message: str,
        incident_id: str | None = None,
        history: tuple[dict[str, str], ...] = (),
        on_progress: ReasoningProgress | None = None,
    ) -> ChatResult:
        now = self._now()
        references: list[dict[str, str]] = []
        incident = None
        async with self._uow_factory() as uow:
            latest_scan = await uow.scans.latest_completed()
            checks = await uow.checks.for_scan(latest_scan.id) if latest_scan else ()
            coverage = _coverage_status(checks)
            context: dict[str, Any] = {
                "as_of": now.isoformat(),
                "coverage_status": coverage,
                "latest_scan": (
                    {
                        "id": latest_scan.id,
                        "mode": latest_scan.mode.value,
                        "status": latest_scan.status.value,
                        "completed_at": latest_scan.completed_at.isoformat() if latest_scan.completed_at else None,
                    }
                    if latest_scan
                    else None
                ),
                "checks": [
                    {
                        "name": check.check_name,
                        "status": check.status.value,
                        "failure": check.failure_summary,
                        "summary": _bounded_value(check.summary),
                    }
                    for check in checks
                ],
            }
            if latest_scan:
                references.append({"kind": "scan", "id": latest_scan.id, "label": "Latest scan"})

            if incident_id:
                incident = await uow.incidents.get(incident_id)
                if incident is None:
                    raise LookupError(f"incident {incident_id} does not exist")
                observations = await uow.incidents.current_observations(incident_id)
                builds = await uow.jenkins.builds_for_incident(incident_id)
                observations += jenkins_build_observations(builds)
                investigation = await uow.investigations.latest_for_incident(incident_id)
                actions = await uow.actions.for_incident(incident_id)
                context["scope"] = "incident"
                context["incident"] = _incident_context(incident, observations, investigation, actions)
                context["jenkins_builds"] = list(builds)
                references.append({"kind": "incident", "id": incident.id, "label": incident.title})
            else:
                page = await uow.incidents.list(limit=20, status="open")
                rows = []
                for current in page.items:
                    observations = await uow.incidents.current_observations(current.id)
                    investigation = await uow.investigations.latest_for_incident(current.id)
                    rows.append((current, observations, investigation))
                rows.sort(
                    key=lambda row: (
                        _severity_rank(row[0].severity.value),
                        len({item.resource_id for item in row[1]}),
                    ),
                    reverse=True,
                )
                context["scope"] = "global"
                context["active_incidents"] = [
                    _incident_context(current, observations, investigation, ())
                    for current, observations, investigation in rows[:12]
                ]
                references.extend(
                    {"kind": "incident", "id": current.id, "label": current.title} for current, _, _ in rows[:12]
                )

        if history or on_progress is not None:
            content = await self._reasoning.chat(
                message=message,
                incident=incident,
                context=context,
                history=history,
                on_progress=on_progress,
            )
        else:
            content = await self._reasoning.chat(message=message, incident=incident, context=context)
        return ChatResult(
            content=content,
            references=tuple(references),
            as_of=now,
            coverage_status=coverage,
        )


def evidence_digest(observations: tuple[Any, ...]) -> str:
    evidence = [
        {
            "stable_identity": observation.stable_identity,
            "severity": observation.severity.value,
            "evidence": to_primitive(observation.evidence),
        }
        for observation in sorted(observations, key=lambda item: item.stable_identity)
    ]
    payload = json.dumps(evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def jenkins_build_observations(builds: tuple[dict[str, Any], ...]) -> tuple[FindingObservation, ...]:
    observations = []
    for build in builds:
        result = str(build.get("result") or "FAILURE")
        priority_score = int(build.get("priority_score") or 0)
        severity = Severity.CRITICAL if result == "FAILURE" and priority_score >= 50 else Severity.WARNING
        started_at = build.get("started_at")
        if not isinstance(started_at, datetime):
            started_at = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
        evidence = {
            **dict(build),
            "job_name": build.get("job_name"),
            "build_number": build.get("build_number"),
            "scm": {
                "provider": build.get("source_provider"),
                "repository": build.get("repository"),
                "change_number": build.get("change_number"),
            },
        }
        observations.append(
            FindingObservation(
                scan_id=f"jenkins-build:{build['id']}",
                check_name="jenkins_monitor",
                rule_id="jenkins.build.failure.v2",
                resource_id=f"jenkins/{build.get('job_name')}#{build.get('build_number')}",
                severity=severity,
                category="jenkins_build",
                summary=str(
                    build.get("failure_summary")
                    or f"{build.get('job_name')} #{build.get('build_number')} failed"
                ),
                observed_at=started_at,
                identity_dimensions={"build_id": str(build["id"])},
                evidence=evidence,
            )
        )
    return tuple(observations)


def should_reinvestigate(
    *, incident: Incident, latest: Investigation | None, evidence_hash: str, now: datetime
) -> bool:
    if latest is None or latest.status is InvestigationStatus.FAILED:
        return True
    if latest.occurrence_id != incident.current_occurrence.id:
        return True
    if latest.evidence_hash != evidence_hash:
        return True
    previous_severity = latest.result.get("deterministic_severity")
    if isinstance(previous_severity, str) and previous_severity != incident.severity.value:
        return True
    return now - latest.created_at >= INVESTIGATION_MAX_AGE


def _coverage_status(checks: tuple[Any, ...]) -> str:
    if not checks:
        return "unavailable"
    succeeded = sum(check.status.value == "succeeded" for check in checks)
    if succeeded == len(checks):
        return "complete"
    if succeeded == 0:
        return "unavailable"
    return "partial"


def _incident_context(
    incident: Any, observations: tuple[Any, ...], investigation: Any, actions: tuple[Any, ...]
) -> dict[str, Any]:
    return {
        "id": incident.id,
        "title": incident.title,
        "status": incident.status.value,
        "severity": incident.severity.value,
        "affected_resource_count": _affected_resource_count(observations),
        "first_seen_at": incident.current_occurrence.opened_at.isoformat(),
        "last_seen_at": (
            incident.current_occurrence.last_observed_at.isoformat()
            if incident.current_occurrence.last_observed_at
            else None
        ),
        "source": _bounded_value(incident.source),
        "observations": [
            {
                "resource_id": item.resource_id,
                "category": item.category,
                "severity": item.severity.value,
                "summary": item.summary,
                "evidence": _bounded_value(item.evidence),
            }
            for item in observations[:20]
        ],
        "omitted_observation_count": max(0, len(observations) - 20),
        "latest_investigation": (
            {
                "status": investigation.status.value,
                "confidence": investigation.confidence.value if investigation.confidence else None,
                "result": _bounded_value(investigation.result),
                "completed_at": investigation.completed_at.isoformat() if investigation.completed_at else None,
            }
            if investigation
            else None
        ),
        "actions": [
            {"type": action.action_type.value, "status": action.status.value, "destination": action.destination}
            for action in actions
        ],
    }


def _affected_resource_count(observations: tuple[Any, ...]) -> int:
    queue_tasks = {
        str(item.evidence["queue_task"])
        for item in observations
        if item.category == "jenkins_queue" and item.evidence.get("queue_task")
    }
    if queue_tasks:
        return len(queue_tasks)
    return len({item.resource_id for item in observations})


def _bounded_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= 4:
        return "[truncated]"
    if isinstance(value, dict) or hasattr(value, "items"):
        return {str(key): _bounded_value(nested, depth=depth + 1) for key, nested in list(value.items())[:16]}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_bounded_value(item, depth=depth + 1) for item in list(value)[:12]]
    if isinstance(value, str):
        return value[:500]
    return value


def _severity_rank(value: str) -> int:
    return {"critical": 2, "warning": 1, "low": 0}.get(value, 0)
