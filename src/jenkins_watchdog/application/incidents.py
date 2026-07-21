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
from jenkins_watchdog.domain.source import SourceAttribution, SourceKind, SourceStatus


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
                    source = _merge_source(linked.source, _source_association(jenkins_build_observations((build,))[0]))
                    linked = linked.associate_source(source, now=now)
                    await uow.incidents.save(linked)
                    await uow.commit()
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

    async def associate_jenkins_source(
        self,
        incident_id: str,
        source: SourceAttribution,
        *,
        now: datetime,
    ) -> Incident | None:
        async with self._uow_factory() as uow:
            incident = await uow.incidents.get(incident_id)
            if incident is None:
                return None
            merged = _merge_source(incident.source, _source_from_attribution(source))
            incident = incident.associate_source(merged, now=now)
            await uow.incidents.save(incident)
            await uow.commit()
            return incident


def _source_association(observation: FindingObservation) -> dict[str, Any]:
    evidence = observation.evidence
    attribution = evidence.get("source_attribution")
    if isinstance(attribution, Mapping):
        return _source_from_evidence(attribution, evidence)
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


def _source_from_attribution(source: SourceAttribution) -> dict[str, Any]:
    return _source_from_evidence(source.evidence(), {})


def _source_from_evidence(metadata: Mapping[str, Any], evidence: Mapping[str, Any]) -> dict[str, Any]:
    kind = str(metadata.get("kind") or "unresolved")
    status = str(metadata.get("status") or "unresolved")
    common = {
        "confirmed": status in {SourceStatus.RESOLVED.value, SourceStatus.VERIFIED.value},
        "verified": status == SourceStatus.VERIFIED.value,
        "status": status,
        "profile_id": metadata.get("profile_id"),
        "profile_registered": bool(metadata.get("profile_registered")),
        "resolution_method": metadata.get("resolution_method"),
        "reason": metadata.get("reason"),
    }
    if kind == SourceKind.CHANGE_REQUEST.value:
        provider = metadata.get("provider")
        repository = metadata.get("repository")
        change_number = metadata.get("change_number")
        if provider and repository and change_number:
            return {
                "kind": "merge_request",
                **common,
                "provider": str(provider),
                "repository": str(repository),
                "change_number": str(change_number),
                "url": metadata.get("url"),
                "branch": metadata.get("branch"),
                "commit_sha": metadata.get("commit_sha"),
                "title": metadata.get("title"),
                "state": metadata.get("state"),
                "allow_mr_comments": bool(metadata.get("allow_mr_comments")) and common["verified"],
                "job_name": evidence.get("job_name"),
                "build_number": evidence.get("build_number") or evidence.get("latest_build"),
            }
    if kind == SourceKind.REPOSITORY_REVISION.value and metadata.get("provider") and metadata.get("repository"):
        return {
            "kind": "repository",
            **common,
            "provider": str(metadata["provider"]),
            "repository": str(metadata["repository"]),
            "url": metadata.get("url"),
            "branch": metadata.get("branch"),
            "commit_sha": metadata.get("commit_sha"),
            "title": metadata.get("title"),
        }
    if kind == SourceKind.PIPELINE.value:
        return {
            "kind": "pipeline",
            **common,
            "provider": "jenkins",
            "job_name": _pipeline_job_name(metadata, evidence),
            "trigger_kind": evidence.get("trigger_kind") or metadata.get("state"),
            "url": metadata.get("url"),
        }
    return {
        "kind": "unknown",
        "confirmed": False,
        "provider": metadata.get("provider"),
        "repository": metadata.get("repository"),
        "reason": metadata.get("reason") or f"source_attribution_{status}",
    }


def _pipeline_job_name(metadata: Mapping[str, Any], evidence: Mapping[str, Any]) -> Any:
    value = evidence.get("root_job") or evidence.get("job_name") or metadata.get("title")
    if not isinstance(value, str):
        return value
    job_name, marker, build_number = value.rpartition(" #")
    return job_name if marker and build_number.isdigit() else value


def _merge_source(existing: Mapping[str, Any], incoming: Mapping[str, Any]) -> dict[str, Any]:
    if not existing:
        return dict(incoming)
    existing_kind = str(existing.get("kind") or "unknown")
    incoming_kind = str(incoming.get("kind") or "unknown")
    concrete_kinds = {"merge_request", "repository", "pipeline"}
    if existing_kind == "infrastructure" and incoming_kind in concrete_kinds:
        return dict(incoming)
    if incoming_kind == "infrastructure" and existing_kind in concrete_kinds:
        return dict(existing)
    existing_confirmed = bool(existing.get("confirmed"))
    incoming_confirmed = bool(incoming.get("confirmed"))
    if not incoming_confirmed:
        return dict(existing) if existing_confirmed else dict(incoming)
    if not existing_confirmed:
        return dict(incoming)
    sources = _individual_sources(existing)
    incoming_sources = _individual_sources(incoming)
    by_identity = {_source_identity(source): source for source in sources}
    for source in incoming_sources:
        identity = _source_identity(source)
        by_identity[identity] = {**by_identity.get(identity, {}), **source}
    values = list(by_identity.values())
    if any(source.get("kind") not in {"infrastructure", "unknown"} for source in values):
        values = [source for source in values if source.get("kind") != "infrastructure"]
    if len(values) == 1:
        return values[0]
    values.sort(key=lambda item: _source_identity(item))
    return {
        "kind": "multiple",
        "confirmed": True,
        "verified": all(bool(item.get("verified")) for item in values),
        "source_count": len(values),
        "sources": values[:50],
    }


def _individual_sources(source: Mapping[str, Any]) -> list[dict[str, Any]]:
    if source.get("kind") == "multiple":
        return [dict(item) for item in source.get("sources") or [] if isinstance(item, Mapping)]
    return [dict(source)]


def _source_identity(source: Mapping[str, Any]) -> str:
    kind = str(source.get("kind") or "unknown")
    fields = {
        "merge_request": (source.get("provider"), source.get("repository"), source.get("change_number")),
        "repository": (source.get("provider"), source.get("repository"), source.get("commit_sha") or source.get("branch")),
        "pipeline": (source.get("provider"), source.get("job_name"), source.get("trigger_kind")),
        "infrastructure": (source.get("provider"), source.get("resource"), source.get("job_name")),
    }.get(kind, (source.get("provider"), source.get("repository"), source.get("reason")))
    return "|".join((kind, *(str(item or "") for item in fields)))
