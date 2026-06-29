"""Tests for investigation gate logic."""

from jenkins_watchdog.checks.base import Finding
from jenkins_watchdog.reasoning.gate import should_investigate
from jenkins_watchdog.state import FindingsDiff


def _finding(severity: str = "warning") -> Finding:
    return Finding(
        severity=severity,
        category="jenkins_failed_build",
        resource="jenkins-job/test",
        symptom="failed",
        context={},
    )


def test_deep_mode_investigates_ongoing_warnings():
    finding = _finding("warning")
    diff = FindingsDiff(new=[], ongoing=[finding], resolved=[])
    assert should_investigate(finding, diff, {}, deep=True) is True


def test_regular_mode_skips_high_confidence_ongoing():
    finding = _finding("critical")
    diff = FindingsDiff(new=[], ongoing=[finding], resolved=[])
    existing = {finding.fingerprint: {"confidence": "high"}}
    assert should_investigate(finding, diff, existing, deep=False) is False


def test_deep_mode_reinvestigates_high_confidence_ongoing():
    finding = _finding("critical")
    diff = FindingsDiff(new=[], ongoing=[finding], resolved=[])
    existing = {finding.fingerprint: {"confidence": "high"}}
    assert should_investigate(finding, diff, existing, deep=True) is True
