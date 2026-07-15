"""Deterministic v2 correlation policy."""

from __future__ import annotations

from dataclasses import dataclass

from jenkins_watchdog.domain.model import FindingObservation


@dataclass(frozen=True, slots=True)
class CorrelationDecision:
    rule_id: str
    key: str


def correlate_observation(observation: FindingObservation) -> CorrelationDecision:
    """Choose exactly one incident correlation key for a finding observation."""
    dims = observation.identity_dimensions

    provider = _clean(dims.get("scm_provider"))
    repository = _clean(dims.get("repository") or dims.get("scm_repository"))
    change = _clean(
        dims.get("change_id") or dims.get("change_number") or dims.get("mr_number") or dims.get("pull_request")
    )
    if provider and repository and change:
        return CorrelationDecision("exact_scm_change", f"{provider}:{repository}:{change}")

    error_signature = _clean(dims.get("error_signature"))
    if error_signature:
        return CorrelationDecision("jenkins_error_signature", error_signature)

    symptom_family = _clean(dims.get("symptom_family"))
    if observation.category == "jenkins_build" and symptom_family:
        return CorrelationDecision("jenkins_runtime_condition_v2", f"controller:{symptom_family}")

    if observation.category == "jenkins_queue" and symptom_family:
        return CorrelationDecision("jenkins_queue_condition_v2", "controller:queue_delay")

    if observation.category == "jenkins_agent" and symptom_family == "no resource limits set":
        container = _clean(dims.get("container")) or "unknown"
        return CorrelationDecision("jenkins_agent_configuration_v2", f"missing_limits:{container}")

    node = _clean(dims.get("jenkins_node") or dims.get("kubernetes_node") or dims.get("node"))
    if node:
        return CorrelationDecision("jenkins_kubernetes_node_symptom_v2", f"{node}:{symptom_family or 'unknown'}")

    agent_pool = _clean(dims.get("agent_pool"))
    if agent_pool and symptom_family:
        return CorrelationDecision("agent_pool_symptom_family_v2", f"{agent_pool}:{symptom_family}")

    return CorrelationDecision("stable_finding", observation.stable_identity)


def correlation_title(decision: CorrelationDecision, observation: FindingObservation) -> str:
    if decision.rule_id == "jenkins_runtime_condition_v2":
        return "Jenkins builds running longer than 2 hours"
    if decision.rule_id == "jenkins_agent_configuration_v2":
        return "Jenkins agent containers missing resource limits"
    if decision.rule_id == "jenkins_queue_condition_v2":
        return "Jenkins build queue is delayed"
    return observation.summary


def _clean(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()
