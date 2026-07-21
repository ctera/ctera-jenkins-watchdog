from datetime import datetime, timezone

from jenkins_watchdog.domain.identity import stable_finding_identity
from jenkins_watchdog.domain.model import FindingObservation, Severity


def test_stable_identity_uses_full_sha256_and_canonical_dimensions():
    first = stable_finding_identity(
        "jenkins.failed_build.v1",
        "job/main",
        {"build": 42, "labels": ["linux", "agent"], "nested": {"b": 2, "a": 1}},
    )
    second = stable_finding_identity(
        "jenkins.failed_build.v1",
        "job/main",
        {"nested": {"a": 1, "b": 2}, "labels": ["linux", "agent"], "build": 42},
    )

    assert first == second
    assert len(first) == 64


def test_identity_excludes_presentation_fields():
    observed_at = datetime(2026, 7, 13, tzinfo=timezone.utc)
    first = FindingObservation(
        scan_id="scan-a",
        check_name="failed-builds",
        rule_id="jenkins.failed_build.v1",
        resource_id="job/main",
        severity=Severity.CRITICAL,
        category="jenkins_failed_build",
        summary="first message",
        observed_at=observed_at,
        identity_dimensions={"error_signature": "compile-error"},
        evidence={"duration_s": 10},
    )
    second = FindingObservation(
        scan_id="scan-b",
        check_name="failed-builds",
        rule_id="jenkins.failed_build.v1",
        resource_id="job/main",
        severity=Severity.LOW,
        category="jenkins_failed_build",
        summary="different message",
        observed_at=observed_at,
        identity_dimensions={"error_signature": "compile-error"},
        evidence={"duration_s": 99},
    )

    assert first.stable_identity == second.stable_identity
