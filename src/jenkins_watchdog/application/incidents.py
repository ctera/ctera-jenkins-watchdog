"""Transactional finding correlation and deterministic incident reconciliation."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import uuid4

from jenkins_watchdog.application.ports import UnitOfWorkFactory
from jenkins_watchdog.application.reasoning import jenkins_build_observations
from jenkins_watchdog.domain.model import FindingObservation, Incident
from jenkins_watchdog.domain.policies import correlate_observation, correlation_title


class IncidentService:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def correlate_and_reconcile(
        self,
        *,
        scan_id: str,
        selected_checks: frozenset[str],
        successful_checks: frozenset[str],
        now: datetime,
    ) -> frozenset[str]:
        async with self._uow_factory() as uow:
            observations = await uow.findings.unlinked_for_scan(scan_id)
            for observation in observations:
                decision = correlate_observation(observation)
                incident = await uow.incidents.get_by_correlation(decision.rule_id, decision.key)
                if incident is None:
                    incident = Incident.open_new(
                        id=str(uuid4()),
                        correlation_rule_id=decision.rule_id,
                        correlation_key=decision.key,
                        observation=observation,
                        opened_at=observation.observed_at,
                        title=correlation_title(decision, observation),
                    )
                else:
                    incident = incident.observe(observation)
                source = _source_association(observation)
                source = _merge_source(incident.source, source)
                incident = incident.associate_source(source, now=observation.observed_at)
                await uow.incidents.save(incident)
                await uow.incidents.link_observation(incident, observation)

            observed_ids = await uow.incidents.observed_ids_for_scan(scan_id)
            for incident in await uow.incidents.active():
                if incident.id in observed_ids:
                    continue
                reconciled = incident.reconcile_after_scan(
                    selected_checks=selected_checks,
                    successful_checks=successful_checks,
                    reconciled_at=now,
                )
                if reconciled != incident:
                    await uow.incidents.save(reconciled)
            await uow.commit()
            return await uow.incidents.observed_ids_for_scan(scan_id)

    async def correlate_jenkins_build(self, build: dict[str, Any], *, now: datetime) -> Incident:
        linked_id = build.get("incident_id")
        if linked_id:
            async with self._uow_factory() as uow:
                linked = await uow.incidents.get(str(linked_id))
            if linked is not None:
                return linked

        observation = jenkins_build_observations((build,))[0]
        signature = str(build.get("failure_signature") or "")
        logical_run = str(build.get("logical_run_key") or f"{build.get('job_name')}#{build.get('build_number')}")
        correlation_key = f"signature:{signature}" if signature else f"logical-run:{logical_run}"
        async with self._uow_factory() as uow:
            incident = await uow.incidents.get_by_correlation("jenkins_failure", correlation_key)
            if incident is None:
                incident = Incident.open_new(
                    id=str(uuid4()),
                    correlation_rule_id="jenkins_failure",
                    correlation_key=correlation_key,
                    observation=observation,
                    opened_at=observation.observed_at,
                    title=str(
                        build.get("failure_summary")
                        or f"{build.get('job_name')} #{build.get('build_number')} failed"
                    ),
                )
            else:
                incident = incident.observe(observation)
            incident = incident.associate_source(
                _merge_source(incident.source, _source_association(observation)),
                now=now,
            )
            await uow.incidents.save(incident)
            await uow.jenkins.link_incident(str(build["id"]), incident.id)
            await uow.commit()
        return incident


def _source_association(observation: FindingObservation) -> dict[str, Any]:
    evidence = observation.evidence
    scm = evidence.get("scm")
    metadata: Mapping[str, Any] = scm if isinstance(scm, Mapping) else evidence
    provider = metadata.get("provider") or metadata.get("scm_provider")
    repository = metadata.get("repository") or metadata.get("scm_repository")
    change_number = metadata.get("change_number") or metadata.get("mr_number") or metadata.get("pr_number")
    present = [provider not in (None, ""), repository not in (None, ""), change_number not in (None, "")]
    normalized_provider = str(provider).lower() if provider not in (None, "") else ""
    if all(present) and normalized_provider not in {"github", "gitlab"}:
        return {"kind": "unknown", "confirmed": False, "reason": "unsupported_scm_provider"}
    if all(present):
        return {
            "kind": "merge_request",
            "confirmed": True,
            "provider": normalized_provider,
            "repository": str(repository),
            "change_number": str(change_number),
            "job_name": evidence.get("job_name"),
            "build_number": evidence.get("build_number") or evidence.get("latest_build"),
        }
    if any(present):
        return {"kind": "unknown", "confirmed": False, "reason": "partial_scm_metadata"}
    if observation.category.startswith("k8s_") or observation.category in {
        "jenkins_agent",
        "jenkins_controller",
        "jenkins_queue",
        "jenkins_build",
    }:
        return {"kind": "infrastructure", "confirmed": True}
    return {"kind": "unknown", "confirmed": False, "reason": "no_complete_source_metadata"}


def _merge_source(existing: Mapping[str, Any], incoming: Mapping[str, Any]) -> dict[str, Any]:
    if incoming.get("reason") in {
        "partial_scm_metadata",
        "conflicting_source_metadata",
        "unsupported_scm_provider",
    }:
        return dict(incoming)
    if not existing:
        return dict(incoming)
    existing_confirmed = bool(existing.get("confirmed"))
    incoming_confirmed = bool(incoming.get("confirmed"))
    if not incoming_confirmed:
        return dict(existing)
    if not existing_confirmed:
        return dict(incoming)
    identity_fields = ("kind", "provider", "repository", "change_number")
    conflicts = any(
        existing.get(field) not in (None, "")
        and incoming.get(field) not in (None, "")
        and existing.get(field) != incoming.get(field)
        for field in identity_fields
    )
    if conflicts:
        return {"kind": "unknown", "confirmed": False, "reason": "conflicting_source_metadata"}
    return {**dict(existing), **dict(incoming)}
