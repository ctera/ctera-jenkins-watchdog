"""Tests for state management."""

from jenkins_watchdog.checks.base import Finding
from jenkins_watchdog.state import FindingsDiff, compute_diff


def test_compute_diff_new_findings():
    previous = []
    current = [
        Finding(severity="critical", category="jenkins_agent", resource="ns/pod-1", symptom="OOMKilled"),
    ]
    diff = compute_diff(previous, current)
    assert len(diff.new) == 1
    assert len(diff.ongoing) == 0
    assert len(diff.resolved) == 0


def test_compute_diff_ongoing_findings():
    f = Finding(severity="critical", category="jenkins_agent", resource="ns/pod-1", symptom="OOMKilled")
    previous = [f.to_dict()]
    current = [f]
    diff = compute_diff(previous, current)
    assert len(diff.new) == 0
    assert len(diff.ongoing) == 1
    assert len(diff.resolved) == 0


def test_compute_diff_resolved():
    f = Finding(severity="critical", category="jenkins_agent", resource="ns/pod-1", symptom="OOMKilled")
    previous = [f.to_dict()]
    current = []
    diff = compute_diff(previous, current)
    assert len(diff.new) == 0
    assert len(diff.ongoing) == 0
    assert len(diff.resolved) == 1


def test_findings_diff_properties():
    diff = FindingsDiff(new=["a", "b"], ongoing=["c"], resolved=[])
    assert diff.has_new_findings is True
    assert diff.new_count == 2

    empty_diff = FindingsDiff()
    assert empty_diff.has_new_findings is False
    assert empty_diff.new_count == 0
