from datetime import datetime, timedelta, timezone

import pytest

from jenkins_watchdog.domain.model import FindingObservation, Incident, IncidentStatus, Severity

NOW = datetime(2026, 7, 13, tzinfo=timezone.utc)


def make_observation(
    check_name: str = "failed-builds",
    *,
    severity: Severity = Severity.CRITICAL,
) -> FindingObservation:
    return FindingObservation(
        scan_id="scan-1",
        check_name=check_name,
        rule_id="jenkins.failed_build.v1",
        resource_id="job/main",
        severity=severity,
        category="jenkins_failed_build",
        summary="failed",
        observed_at=NOW,
        identity_dimensions={"error_signature": "compile-error"},
    )


def test_incident_resolves_only_when_responsible_checks_selected_and_successful():
    incident = Incident.open_new(
        id="incident-1",
        correlation_rule_id="jenkins_error_signature",
        correlation_key="compile-error",
        observation=make_observation("failed-builds"),
        opened_at=NOW,
    )

    unchanged = incident.reconcile_after_scan(
        selected_checks=frozenset({"k8s-nodes"}),
        successful_checks=frozenset({"k8s-nodes"}),
        reconciled_at=NOW + timedelta(minutes=1),
    )
    assert unchanged.status == IncidentStatus.OPEN

    failed = incident.reconcile_after_scan(
        selected_checks=frozenset({"failed-builds"}),
        successful_checks=frozenset(),
        reconciled_at=NOW + timedelta(minutes=1),
    )
    assert failed.status == IncidentStatus.OPEN

    resolved = incident.reconcile_after_scan(
        selected_checks=frozenset({"failed-builds"}),
        successful_checks=frozenset({"failed-builds"}),
        reconciled_at=NOW + timedelta(minutes=1),
    )
    assert resolved.status == IncidentStatus.RESOLVED
    assert resolved.current_occurrence.resolved_at == NOW + timedelta(minutes=1)


def test_suppressed_incident_collects_observations_and_unsuppresses_to_current_state():
    incident = Incident.open_new(
        id="incident-1",
        correlation_rule_id="jenkins_error_signature",
        correlation_key="compile-error",
        observation=make_observation("failed-builds"),
        opened_at=NOW,
    ).suppress(reason="maintenance window", actor="elior@example.com", suppressed_at=NOW)

    assert not incident.automation_allowed

    observed = incident.observe(make_observation("pipeline-patterns"))
    assert observed.status == IncidentStatus.SUPPRESSED
    assert observed.current_occurrence.responsible_checks == frozenset({"failed-builds", "pipeline-patterns"})

    unsuppressed_active = observed.unsuppress()
    assert unsuppressed_active.status == IncidentStatus.OPEN

    suppressed_resolved = observed.reconcile_after_scan(
        selected_checks=frozenset({"failed-builds", "pipeline-patterns"}),
        successful_checks=frozenset({"failed-builds", "pipeline-patterns"}),
        reconciled_at=NOW + timedelta(minutes=5),
    )
    assert suppressed_resolved.status == IncidentStatus.SUPPRESSED
    assert suppressed_resolved.unsuppress().status == IncidentStatus.RESOLVED


def test_suppression_requires_reason_and_actor():
    incident = Incident.open_new(
        id="incident-1",
        correlation_rule_id="stable_finding",
        correlation_key="key",
        observation=make_observation(),
        opened_at=NOW,
    )

    with pytest.raises(ValueError):
        incident.suppress(reason="", actor="elior@example.com", suppressed_at=NOW)

    with pytest.raises(ValueError):
        incident.suppress(reason="maintenance", actor="", suppressed_at=NOW)


def test_suppressed_incident_reopens_occurrence_when_observed_after_resolution():
    incident = Incident.open_new(
        id="incident-1",
        correlation_rule_id="stable_finding",
        correlation_key="key",
        observation=make_observation(),
        opened_at=NOW,
    ).suppress(reason="maintenance", actor="elior@example.com", suppressed_at=NOW)
    incident = incident.reconcile_after_scan(
        selected_checks=frozenset({"failed-builds"}),
        successful_checks=frozenset({"failed-builds"}),
        reconciled_at=NOW + timedelta(minutes=1),
    )

    reopened = incident.observe(make_observation())

    assert reopened.status == IncidentStatus.SUPPRESSED
    assert reopened.current_occurrence.active
    assert reopened.current_occurrence.number == 2
    assert reopened.unsuppress().status == IncidentStatus.OPEN


def test_reopened_occurrence_recomputes_severity_from_current_observations():
    incident = Incident.open_new(
        id="incident-1",
        correlation_rule_id="stable_finding",
        correlation_key="key",
        observation=make_observation(),
        opened_at=NOW,
    ).reconcile_after_scan(
        selected_checks=frozenset({"failed-builds"}),
        successful_checks=frozenset({"failed-builds"}),
        reconciled_at=NOW + timedelta(minutes=1),
    )

    reopened = incident.observe(make_observation(severity=Severity.WARNING))
    escalated = reopened.observe(make_observation("pipeline-patterns", severity=Severity.CRITICAL))

    assert reopened.severity is Severity.WARNING
    assert escalated.severity is Severity.CRITICAL
