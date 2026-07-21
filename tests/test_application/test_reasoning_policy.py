from datetime import datetime, timedelta, timezone

from jenkins_watchdog.application.reasoning import should_reinvestigate
from jenkins_watchdog.domain.model import (
    Confidence,
    FindingObservation,
    Incident,
    Investigation,
    InvestigationStatus,
    Severity,
)

NOW = datetime(2026, 7, 13, tzinfo=timezone.utc)


def incident() -> Incident:
    observation = FindingObservation(
        scan_id="scan",
        check_name="check",
        rule_id="rule.v1",
        resource_id="resource",
        severity=Severity.WARNING,
        category="k8s_node",
        summary="pressure",
        observed_at=NOW,
    )
    return Incident.open_new(
        id="incident",
        correlation_rule_id="stable_finding",
        correlation_key=observation.stable_identity,
        observation=observation,
        opened_at=NOW,
    )


def investigation(target: Incident, **overrides) -> Investigation:
    values = {
        "id": "investigation",
        "incident_id": target.id,
        "occurrence_id": target.current_occurrence.id,
        "status": InvestigationStatus.SUCCEEDED,
        "evidence_hash": "evidence",
        "input_version": "v1",
        "prompt_version": "v1",
        "model": "model",
        "confidence": Confidence.HIGH,
        "usage": {},
        "result": {"deterministic_severity": "warning"},
        "created_at": NOW,
        "completed_at": NOW,
    }
    values.update(overrides)
    return Investigation(**values)


def test_reinvestigation_reasons_and_fresh_noop() -> None:
    target = incident()
    latest = investigation(target)

    assert not should_reinvestigate(
        incident=target, latest=latest, evidence_hash="evidence", now=NOW + timedelta(hours=23)
    )
    assert should_reinvestigate(incident=target, latest=latest, evidence_hash="changed", now=NOW + timedelta(hours=1))
    assert should_reinvestigate(incident=target, latest=latest, evidence_hash="evidence", now=NOW + timedelta(hours=24))
    assert should_reinvestigate(
        incident=target,
        latest=investigation(target, status=InvestigationStatus.FAILED),
        evidence_hash="evidence",
        now=NOW,
    )
    # A truncated investigation never reached a conclusion, so it must not be cached as fresh.
    assert should_reinvestigate(
        incident=target,
        latest=investigation(target, status=InvestigationStatus.PARTIAL),
        evidence_hash="evidence",
        now=NOW,
    )


def test_reinvestigates_on_reopen_or_severity_change() -> None:
    target = incident()
    old = investigation(target)
    reopened = target.reconcile_after_scan(
        selected_checks=frozenset({"check"}),
        successful_checks=frozenset({"check"}),
        reconciled_at=NOW + timedelta(minutes=1),
    ).observe(
        FindingObservation(
            scan_id="scan-2",
            check_name="check",
            rule_id="rule.v1",
            resource_id="resource",
            severity=Severity.CRITICAL,
            category="k8s_node",
            summary="pressure",
            observed_at=NOW + timedelta(minutes=2),
        )
    )

    assert should_reinvestigate(incident=reopened, latest=old, evidence_hash="evidence", now=NOW + timedelta(minutes=3))
