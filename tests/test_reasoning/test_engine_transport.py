"""The investigation loop's contract with its callers, across the transport swap.

The progress event shapes and the graceful-degradation behaviour are consumed by the
dashboard and the scan runner respectively, and neither is checked by anything else.
"""

import pytest

from jenkins_watchdog.checks.base import Finding
from jenkins_watchdog.reasoning import engine
from jenkins_watchdog.reasoning.claude_agent import AgentTurn
from jenkins_watchdog.scan_options import ScanOptions


class FakeRuntime:
    def __init__(self, turn: AgentTurn | None = None, *, configured: bool = True, raises: Exception | None = None):
        self._turn = turn or AgentTurn(content="done")
        self.configured = configured
        self._raises = raises
        self.run_calls: list[dict] = []

    async def run_agent(self, **kwargs):
        self.run_calls.append(kwargs)
        if self._raises:
            raise self._raises
        for text in self._turn.narration:
            await kwargs["on_text"](text)
        return self._turn

    async def complete(self, **kwargs):
        return AgentTurn(content='{"root_cause":"x","evidence":[],"impact":"y","suggested_fix":"z","confidence":"high"}')


@pytest.fixture
def runtime(monkeypatch):
    def install(rt):
        monkeypatch.setattr(engine, "get_runtime", lambda: rt)
        return rt

    return install


async def test_no_token_skips_instead_of_raising(runtime) -> None:
    """A scan without a token must still report its findings.

    engine returns an empty result, investigate_finding turns that into "no
    investigation", and detection output is unaffected. Raising here would abort the scan.
    """
    runtime(FakeRuntime(configured=False))

    result = await engine.run_tool_loop(system_prompt="s", user_prompt="u")

    assert result.raw_reasoning == ""
    assert result.tools_used == []


async def test_an_agent_failure_degrades_rather_than_aborting_the_scan(runtime) -> None:
    runtime(FakeRuntime(raises=RuntimeError("401 OAuth access token is invalid")))

    result = await engine.run_tool_loop(system_prompt="s", user_prompt="u")

    assert result.raw_reasoning == ""


async def test_progress_events_keep_the_shapes_the_dashboard_reads(runtime) -> None:
    """Dashboard.tsx reads evt.tool and evt.args for tool_call, evt.content for reasoning.

    These keys differ deliberately from the chat SSE vocabulary (tool_name/tool_args);
    renaming either breaks the UI without failing any other test.
    """
    rt = runtime(FakeRuntime(AgentTurn(content="final", narration=("thinking about it",))))
    events: list[dict] = []

    await engine.run_tool_loop(system_prompt="s", user_prompt="u", on_progress=events.append)
    # The tool_call half goes through the same _emit; drive it directly.
    await rt.run_calls[0]["on_tool_call"]("jenkins_get_build_log", {"job_name": "app"})

    assert events[0] == {"type": "reasoning", "content": "thinking about it"}
    assert events[1] == {"type": "tool_call", "tool": "jenkins_get_build_log", "args": {"job_name": "app"}}


async def test_reasoning_content_is_truncated_for_the_event_stream(runtime) -> None:
    runtime(FakeRuntime(AgentTurn(content="final", narration=("x" * 900,))))
    events: list[dict] = []

    await engine.run_tool_loop(system_prompt="s", user_prompt="u", on_progress=events.append)

    assert len(events[0]["content"]) == 500


async def test_scan_options_are_threaded_to_the_runtime(runtime) -> None:
    """Deep-scan depth must reach the tool bridge, not be re-derived from the default."""
    rt = runtime(FakeRuntime())
    deep = ScanOptions.deep_scan()

    await engine.run_tool_loop(system_prompt="s", user_prompt="u", scan_options=deep, max_rounds=25)

    assert rt.run_calls[0]["scan_options"] is deep
    assert rt.run_calls[0]["max_turns"] == 25


async def test_investigate_finding_returns_none_when_the_agent_produced_nothing(runtime) -> None:
    runtime(FakeRuntime(configured=False))
    finding = Finding(severity="critical", category="jenkins_agent", resource="jenkins/pod", symptom="down")

    assert await engine.investigate_finding(finding) is None


async def test_investigate_finding_sums_usage_across_both_passes(runtime) -> None:
    """The loop and the extraction pass both bill; the dashboard shows the total."""
    runtime(FakeRuntime(AgentTurn(content="reasoning text", tools_used=["k8s_get_pods"], prompt_tokens=100, completion_tokens=20, cost_usd=0.01)))
    finding = Finding(severity="critical", category="jenkins_agent", resource="jenkins/pod", symptom="down")

    inv = await engine.investigate_finding(finding)

    assert inv is not None
    assert inv.tools_used == ["k8s_get_pods"]
    assert inv.prompt_tokens == 100
    assert inv.raw_reasoning == "reasoning text"
