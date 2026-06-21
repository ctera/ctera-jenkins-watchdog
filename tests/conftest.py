"""Shared test fixtures."""

import pytest


@pytest.fixture
def sample_findings():
    """Sample findings for testing."""
    from jenkins_watchdog.checks.base import Finding

    return [
        Finding(
            severity="critical",
            category="jenkins_agent",
            resource="jenkins/jenkins-agent-abc123",
            symptom="CrashLoopBackOff (container: jnlp)",
            context={"restart_count": 17},
        ),
        Finding(
            severity="warning",
            category="jenkins_agent",
            resource="jenkins-agent/worker-1",
            symptom="Agent offline: connection timed out",
            context={"offline_reason": "connection timed out"},
        ),
    ]
