"""Tests for base check infrastructure."""

from jenkins_watchdog.checks.base import Finding


def test_finding_fingerprint_deterministic():
    f1 = Finding(severity="critical", category="jenkins_agent", resource="ns/pod-1", symptom="OOMKilled")
    f2 = Finding(severity="critical", category="jenkins_agent", resource="ns/pod-1", symptom="OOMKilled")
    assert f1.fingerprint == f2.fingerprint


def test_finding_fingerprint_differs():
    f1 = Finding(severity="critical", category="jenkins_agent", resource="ns/pod-1", symptom="OOMKilled")
    f2 = Finding(severity="critical", category="jenkins_agent", resource="ns/pod-2", symptom="OOMKilled")
    assert f1.fingerprint != f2.fingerprint


def test_finding_to_dict():
    f = Finding(
        severity="warning",
        category="jenkins_agent",
        resource="jenkins-agent/worker-1",
        symptom="Agent offline",
        context={"reason": "timeout"},
    )
    d = f.to_dict()
    assert d["severity"] == "warning"
    assert d["category"] == "jenkins_agent"
    assert d["fingerprint"] == f.fingerprint
    assert "reason" in d["context"]


def test_finding_fingerprint_ignores_dynamic_numbers():
    f1 = Finding(severity="warning", category="jenkins_agent", resource="ns/pod-1", symptom="Memory at 85% of limit")
    f2 = Finding(severity="warning", category="jenkins_agent", resource="ns/pod-1", symptom="Memory at 92% of limit")
    assert f1.fingerprint == f2.fingerprint
