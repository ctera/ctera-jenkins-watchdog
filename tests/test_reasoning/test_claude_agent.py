"""The agent runtime: tool bridge, isolation, redaction, and the aggregate context bound."""

from contextlib import asynccontextmanager

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

from jenkins_watchdog.reasoning.claude_agent import (
    ClaudeCodeRuntime,
    ToolCallRecorder,
    allowed_tool_names,
    bounded_tool_output,
    build_tool_server,
    plain_tool_name,
    qualified_tool_name,
    redact,
)
from jenkins_watchdog.reasoning.claude_auth import ClaudeCredentials
from jenkins_watchdog.scan_options import ScanOptions, get_scan_options

TOKEN = "sk-ant-oat01-test"

DEFINITIONS = [
    {
        "name": "jenkins_get_build_log",
        "description": "read a console log",
        "input_schema": {
            "type": "object",
            "properties": {"job_name": {"type": "string"}},
            "required": ["job_name"],
        },
    }
]


def _result(**kwargs):
    base = dict(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="s",
    )
    base.update(kwargs)
    return ResultMessage(**base)


def _runtime(session_factory, **kwargs) -> ClaudeCodeRuntime:
    return ClaudeCodeRuntime(
        credentials=ClaudeCredentials(token=TOKEN, config_dir="/tmp/watchdog-test-claude-home"),
        model="claude-sonnet-5",
        fallback_model="claude-opus-5",
        session_factory=session_factory,
        **kwargs,
    )


def _session_of(*messages):
    """A fake CLI session that replays the same messages for every prompt sent."""
    sent: list[str] = []

    class Session:
        async def send(self, prompt):
            sent.append(prompt)
            for message in messages:
                yield message

    @asynccontextmanager
    async def factory(options):
        factory.options = options
        yield Session()

    factory.sent = sent
    return factory


# --- tool bridge ------------------------------------------------------------


def test_tool_names_round_trip_through_the_mcp_prefix() -> None:
    """The trace and the frontend compare bare names.

    An SDK MCP tool is addressed as mcp__watchdog__<name>. If that prefixed form reached
    `tools_used` or the tool_call progress event, the dashboard would render mangled names
    and any downstream comparison against a bare name would silently never match.
    """
    assert qualified_tool_name("jenkins_get_build_log") == "mcp__watchdog__jenkins_get_build_log"
    assert plain_tool_name("mcp__watchdog__jenkins_get_build_log") == "jenkins_get_build_log"
    assert plain_tool_name("jenkins_get_build_log") == "jenkins_get_build_log"


def test_allowed_tools_are_all_qualified() -> None:
    names = allowed_tool_names()
    assert names, "the real registry should not be empty"
    assert all(n.startswith("mcp__watchdog__") for n in names)


async def test_the_bridge_records_bare_names_and_drops_null_arguments(monkeypatch) -> None:
    seen = {}

    async def fake_execute(name, arguments):
        seen["name"] = name
        seen["arguments"] = arguments
        return "output"

    monkeypatch.setattr("jenkins_watchdog.reasoning.claude_agent.execute_tool", fake_execute)
    recorder = ToolCallRecorder()
    _, invoke = build_tool_server(recorder, ScanOptions(), definitions=DEFINITIONS)

    result = await invoke("jenkins_get_build_log", {"job_name": "job", "build_number": None})

    assert seen["name"] == "jenkins_get_build_log"
    # None-valued arguments are dropped rather than passed through as nulls.
    assert seen["arguments"] == {"job_name": "job"}
    assert recorder.tools_used == ["jenkins_get_build_log"]
    assert result["is_error"] is False


async def test_scan_options_reach_the_tool_handler(monkeypatch) -> None:
    """Regression: deep scans must keep full build logs.

    ScanOptions is a ContextVar, and tools/jenkins.py reads it *inside* the handler to
    decide the 64KB full-log cap. Tools now run inside the SDK's own task, so if the
    active options did not reach the handler, deep scans would quietly fall back to
    truncated logs with nothing failing.
    """
    observed = {}

    async def fake_execute(name, arguments):
        options = get_scan_options()
        observed["deep"] = options.deep
        observed["full_build_logs"] = options.full_build_logs
        return "output"

    monkeypatch.setattr("jenkins_watchdog.reasoning.claude_agent.execute_tool", fake_execute)
    deep = ScanOptions.deep_scan()
    _, invoke = build_tool_server(ToolCallRecorder(deep=True), deep, definitions=DEFINITIONS)

    await invoke("jenkins_get_build_log", {"job_name": "job"})

    assert observed == {"deep": True, "full_build_logs": True}
    # And the ContextVar is restored afterwards, so one tool call cannot leak deep-scan
    # behaviour into the rest of the process.
    assert get_scan_options().deep is False


async def test_tool_failures_are_flagged_without_ending_the_turn(monkeypatch) -> None:
    """execute_tool returns its failures as text and never raises.

    Marking them is_error lets the model try a different tool instead of reasoning over an
    error blob as though it were evidence.
    """

    async def failing(name, arguments):
        return f"Tool execution error ({name}): boom"

    monkeypatch.setattr("jenkins_watchdog.reasoning.claude_agent.execute_tool", failing)
    _, invoke = build_tool_server(ToolCallRecorder(), ScanOptions(), definitions=DEFINITIONS)

    result = await invoke("jenkins_get_build_log", {"job_name": "job"})

    assert result["is_error"] is True


# --- bounding and redaction -------------------------------------------------


def test_bounded_output_elides_the_middle_and_keeps_both_ends() -> None:
    """Head and tail carry the command and the traceback; the middle is filler."""
    value = "head" + "x" * 5_000 + "tail"

    bounded = bounded_tool_output(value, 1_000)

    assert len(bounded) < 1_100
    assert bounded.startswith("head")
    assert bounded.endswith("tail")
    assert bounded_tool_output("short", 1_000) == "short"


async def test_accumulated_tool_output_cannot_outgrow_the_context_budget(monkeypatch) -> None:
    """A per-result cap alone does not bound context.

    An investigation that reads one console log per failing build makes dozens of calls,
    and dozens times any fixed cap still exhausts the window.
    """

    async def big(name, arguments):
        return "y" * 12_000

    monkeypatch.setattr("jenkins_watchdog.reasoning.claude_agent.execute_tool", big)
    recorder = ToolCallRecorder()
    _, invoke = build_tool_server(recorder, ScanOptions(), definitions=DEFINITIONS)

    for _ in range(200):
        await invoke("jenkins_get_build_log", {"job_name": "job"})

    assert recorder.returned_chars <= 130_000
    assert len(recorder.tools_used) == 200


def test_deep_scans_get_a_larger_budget() -> None:
    regular, deep = ToolCallRecorder(deep=False), ToolCallRecorder(deep=True)
    chunk = "z" * 200_000

    regular.bound(chunk)
    deep.bound(chunk)

    assert deep.returned_chars > regular.returned_chars


def test_credentials_are_stripped_from_tool_output() -> None:
    """tools/source_code.py echoes resp.text[:500] verbatim on a non-2xx response.

    An echoed request URL or error body can carry the token that made the request, and
    from there it reaches the model's context and the stored investigation trace.
    """
    secret = "glpat-SUPERSECRETVALUE"

    cleaned = redact(f"401 Unauthorized for token={secret} on /api/v4/projects", [secret])

    assert secret not in cleaned
    assert "[redacted]" in cleaned


def test_redaction_ignores_values_too_short_to_be_credentials() -> None:
    """Blind replacement of a short value would corrupt the evidence it appears in."""
    recorder = ToolCallRecorder(_secrets=())
    assert recorder.bound("build 42 failed") == "build 42 failed"


# --- runtime ----------------------------------------------------------------


async def test_the_subprocess_gets_the_token_and_no_other_credential(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-xxx")
    monkeypatch.setenv("WATCHDOG_JENKINS_TOKEN", "jenkins-secret")
    factory = _session_of(
        AssistantMessage(content=[TextBlock(text="ok")], model="claude-sonnet-5"),
        _result(result="ok"),
    )

    await _runtime(factory).complete(system_prompt="sys", prompt="hi")
    options = factory.options

    assert options.env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
    assert options.env["ANTHROPIC_API_KEY"] == ""
    assert options.env["WATCHDOG_JENKINS_TOKEN"] == ""
    assert options.env["CLAUDE_CONFIG_DIR"]
    # Isolation on all three axes.
    assert options.strict_mcp_config is True
    # [] disables filesystem settings; None would load ALL of them, pulling this repo's own
    # CLAUDE.md into an incident investigator's prompt.
    assert options.setting_sources == []
    assert options.tools == []
    # Fail closed: a tool the model invents is denied, not executed.
    assert options.permission_mode == "default"
    # The fallback the LiteLLM model chain used to provide is preserved by the SDK.
    assert options.model == "claude-sonnet-5"
    assert options.fallback_model == "claude-opus-5"


async def test_a_failed_turn_raises_instead_of_looking_empty() -> None:
    """An auth failure must be loud.

    A failed turn still yields a ResultMessage, so returning its text would make a 401
    indistinguishable from a model that had nothing to say — and callers degrade
    gracefully on an empty answer.
    """
    factory = _session_of(
        _result(subtype="error_during_execution", is_error=True, errors=["401 OAuth access token is invalid"])
    )

    with pytest.raises(RuntimeError, match="401"):
        await _runtime(factory).complete(system_prompt="sys", prompt="hi")


async def test_a_silent_cli_times_out_rather_than_hanging_forever() -> None:
    import asyncio

    class Session:
        async def send(self, prompt):
            await asyncio.sleep(5)
            yield None

    @asynccontextmanager
    async def factory(options):
        yield Session()

    with pytest.raises(TimeoutError, match="no output"):
        await _runtime(factory, idle_timeout_s=0.05).complete(system_prompt="sys", prompt="hi")


async def test_the_answer_comes_from_the_result_not_the_narration() -> None:
    """The agent narrates while exploring; that planning must not reach the assessment.

    Joined assistant blocks would put "Let me check the build log" into the operator-facing
    root cause and into the structured-extraction input.
    """
    factory = _session_of(
        AssistantMessage(content=[TextBlock(text="Let me check the build log")], model="m"),
        AssistantMessage(content=[TextBlock(text="Now checking agents")], model="m"),
        _result(result="Root cause: the agent pod was OOMKilled.", usage={"input_tokens": 10, "output_tokens": 4}),
    )

    turn = await _runtime(factory).run_agent(
        system_prompt="sys", prompt="investigate", scan_options=ScanOptions(), max_turns=5
    )

    assert turn.content == "Root cause: the agent pod was OOMKilled."
    assert "Let me check" not in turn.content
    # The narration is still available for callers that want it.
    assert turn.narration == ("Let me check the build log", "Now checking agents")
    assert (turn.prompt_tokens, turn.completion_tokens) == (10, 4)


async def test_progress_events_carry_bare_tool_names_and_arguments() -> None:
    """These feed the dashboard's tool_call / reasoning stream."""
    factory = _session_of(
        AssistantMessage(
            content=[
                TextBlock(text="checking"),
                ToolUseBlock(
                    id="t1",
                    name="mcp__watchdog__jenkins_get_build_log",
                    input={"job_name": "build-app"},
                ),
            ],
            model="m",
        ),
        _result(result="done"),
    )
    texts: list[str] = []
    calls: list[tuple[str, dict]] = []

    await _runtime(factory).run_agent(
        system_prompt="sys",
        prompt="go",
        scan_options=ScanOptions(),
        max_turns=5,
        on_text=lambda t: _append(texts, t),
        on_tool_call=lambda n, a: _append(calls, (n, a)),
    )

    assert texts == ["checking"]
    assert calls == [("jenkins_get_build_log", {"job_name": "build-app"})]


async def test_hitting_the_turn_limit_asks_for_a_summary() -> None:
    """The old loop's for/else: run out of rounds, then ask for the conclusion.

    Without it, an investigation that used all its turns returns whatever half-thought the
    agent stopped on.
    """
    factory = _session_of(
        AssistantMessage(content=[TextBlock(text="still working")], model="m"),
        _result(stop_reason="max_turns", result="", usage={"input_tokens": 5, "output_tokens": 1}),
    )

    turn = await _runtime(factory).run_agent(
        system_prompt="sys",
        prompt="go",
        scan_options=ScanOptions(),
        max_turns=1,
        summary_prompt="Summarize your findings so far.",
    )

    assert factory.sent == ["go", "Summarize your findings so far."]
    # Usage from both turns is accumulated, not just the last one.
    assert (turn.prompt_tokens, turn.completion_tokens) == (10, 2)


async def _append(sink, value):
    sink.append(value)
