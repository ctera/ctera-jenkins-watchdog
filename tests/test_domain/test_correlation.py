from datetime import datetime, timezone

from jenkins_watchdog.domain.model import FindingObservation, Severity
from jenkins_watchdog.domain.policies import correlate_observation


def observation(**dimensions):
    return FindingObservation(
        scan_id="scan-1",
        check_name="check",
        rule_id="rule.v1",
        resource_id="resource",
        severity=Severity.WARNING,
        category="jenkins_failed_build",
        summary="summary",
        observed_at=datetime(2026, 7, 13, tzinfo=timezone.utc),
        identity_dimensions=dimensions,
    )


def test_correlation_prefers_complete_scm_change():
    decision = correlate_observation(
        observation(
            scm_provider="github",
            repository="ctera/app",
            mr_number="123",
            error_signature="same-error",
        )
    )

    assert decision.rule_id == "exact_scm_change"
    assert decision.key == "github:ctera/app:123"


def test_correlation_accepts_detector_scm_dimension_names():
    decision = correlate_observation(
        observation(
            scm_provider="gitlab",
            scm_repository="ctera/platform",
            change_number="91",
        )
    )

    assert decision.rule_id == "exact_scm_change"
    assert decision.key == "gitlab:ctera/platform:91"


def test_correlation_requires_complete_scm_metadata_before_error_signature():
    decision = correlate_observation(observation(repository="ctera/app", error_signature="same-error"))

    assert decision.rule_id == "jenkins_error_signature"
    assert decision.key == "same-error"


def test_correlation_order_reaches_node_agent_pool_then_stable_finding():
    node_decision = correlate_observation(observation(kubernetes_node="worker-a"))
    assert node_decision.rule_id == "jenkins_kubernetes_node_symptom_v2"
    assert node_decision.key == "worker-a:unknown"

    pool_decision = correlate_observation(observation(agent_pool="linux", symptom_family="disconnect"))
    assert pool_decision.rule_id == "agent_pool_symptom_family_v2"
    assert pool_decision.key == "linux:disconnect"

    fallback = correlate_observation(observation())
    assert fallback.rule_id == "stable_finding"
    assert fallback.key == observation().stable_identity
