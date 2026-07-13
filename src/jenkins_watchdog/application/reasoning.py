"""Reasoning orchestration and deterministic reinvestigation policy."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from jenkins_watchdog.application.ports import ReasoningPort, UnitOfWorkFactory
from jenkins_watchdog.domain.model import Incident, Investigation, InvestigationStatus
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

    async def investigate_if_needed(self, incident_id: str, *, force: bool = False) -> Investigation | None:
        async with self._uow_factory() as uow:
            incident = await uow.incidents.get(incident_id)
            if incident is None:
                raise LookupError(f"incident {incident_id} does not exist")
            observations = await uow.incidents.observations(incident_id)
            latest = await uow.investigations.latest_for_incident(incident_id)
        evidence_hash = evidence_digest(observations)
        if not force and not should_reinvestigate(
            incident=incident,
            latest=latest,
            evidence_hash=evidence_hash,
            now=self._now(),
        ):
            return latest

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

    async def chat(self, *, message: str, incident_id: str | None = None) -> str:
        incident = None
        if incident_id:
            async with self._uow_factory() as uow:
                incident = await uow.incidents.get(incident_id)
            if incident is None:
                raise LookupError(f"incident {incident_id} does not exist")
        return await self._reasoning.chat(message=message, incident=incident)


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
