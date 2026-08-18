"""Triage's fail-open contract: it decides what to investigate, never what to skip."""

import pytest

from jenkins_watchdog.checks.base import Finding
from jenkins_watchdog.reasoning import triage
from jenkins_watchdog.reasoning.claude_agent import AgentTurn


class FakeRuntime:
    def __init__(self, *, configured=True, content="", raises=None, tokens=(0, 0)):
        self.configured = configured
        self._content = content
        self._raises = raises
        self._tokens = tokens
        self.calls: list[dict] = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises:
            raise self._raises
        return AgentTurn(content=self._content, prompt_tokens=self._tokens[0], completion_tokens=self._tokens[1])


@pytest.fixture
def findings():
    return [
        Finding(severity="critical", category="jenkins_agent", resource="jenkins/a", symptom="down"),
        Finding(severity="warning", category="jenkins_agent", resource="jenkins/b", symptom="slow"),
    ]


def _install(monkeypatch, runtime):
    monkeypatch.setattr(triage, "get_runtime", lambda: runtime)
    return runtime


async def test_no_token_sends_everything_to_investigation(monkeypatch, findings) -> None:
    """Triage is a cost optimisation. Without it, every finding is investigated."""
    _install(monkeypatch, FakeRuntime(configured=False))

    result = await triage.triage_findings(findings)

    assert result.to_investigate == findings


async def test_a_failed_call_fails_open(monkeypatch, findings) -> None:
    """A triage outage should cost tokens, not coverage."""
    _install(monkeypatch, FakeRuntime(raises=RuntimeError("401 OAuth access token is invalid")))

    result = await triage.triage_findings(findings)

    assert result.to_investigate == findings


async def test_triage_runs_without_tools(monkeypatch, findings) -> None:
    """Giving triage tools would turn the cheap pre-filter into a second investigation."""
    rt = _install(monkeypatch, FakeRuntime(content="[]"))

    await triage.triage_findings(findings)

    assert set(rt.calls[0]) == {"system_prompt", "prompt"}


async def test_usage_is_carried_through(monkeypatch, findings) -> None:
    """Triage tokens land in the scan's token_usage total alongside investigations."""
    _install(monkeypatch, FakeRuntime(content="[]", tokens=(80, 12)))

    result = await triage.triage_findings(findings)

    assert (result.prompt_tokens, result.completion_tokens) == (80, 12)


async def test_no_findings_short_circuits_before_spawning_anything(monkeypatch) -> None:
    rt = _install(monkeypatch, FakeRuntime(content="[]"))

    result = await triage.triage_findings([])

    assert result.to_investigate == []
    assert rt.calls == []
