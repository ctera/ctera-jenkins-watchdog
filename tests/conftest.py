"""Shared test fixtures."""

import pytest

from jenkins_watchdog.config import Settings
from jenkins_watchdog.config import settings as _settings


@pytest.fixture(autouse=True)
def isolated_settings():
    """Never let a test read the developer's real .env.

    ``config.py`` instantiates ``settings = Settings()`` at import time with
    ``env_file=".env"``, and modules capture that singleton with ``from ... import
    settings``. So without this, a test that touches any settings-reading code path picks
    up whatever credentials happen to be on the developer's machine -- passing locally,
    failing in CI, and putting real tokens one assertion-failure message away from a log.

    Fields are reset to their declared defaults for the duration of each test and restored
    afterwards, so the singleton every module already holds is the isolated one.
    """
    original = {name: getattr(_settings, name) for name in Settings.model_fields}
    for name, field in Settings.model_fields.items():
        setattr(_settings, name, field.get_default(call_default_factory=True))
    try:
        yield _settings
    finally:
        for name, value in original.items():
            setattr(_settings, name, value)


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
