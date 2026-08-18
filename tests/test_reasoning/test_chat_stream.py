"""The SSE contract the frontend consumes.

frontend/src/services/api.ts declares ChatEvent as token | tool_start | tool_result |
done | error, and Chat.tsx / Findings.tsx switch on exactly those. Nothing else in the
repo checks these shapes, so a renamed key breaks the UI silently.
"""

import json

import pytest

from jenkins_watchdog.reasoning.chat_stream import render_transcript, stream_chat
from jenkins_watchdog.reasoning.claude_agent import AgentTurn


class FakeRuntime:
    """Replays a scripted turn through the real callback plumbing."""

    def __init__(self, *, texts=(), tool_calls=(), tool_results=(), content="final answer", raises=None):
        self._texts = texts
        self._tool_calls = tool_calls
        self._tool_results = tool_results
        self._content = content
        self._raises = raises
        self.calls: list[dict] = []

    async def run_agent(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises:
            raise self._raises
        for text in self._texts:
            await kwargs["on_text"](text)
        for name, args in self._tool_calls:
            await kwargs["on_tool_call"](name, args)
        for name, ok in self._tool_results:
            await kwargs["on_tool_result"](name, ok)
        return AgentTurn(content=self._content, narration=tuple(self._texts))


async def _collect(**kwargs):
    return [json.loads(e["data"]) async for e in stream_chat(**kwargs)]


def _kwargs(runtime, transcript=None, **over):
    base = dict(
        runtime=runtime,
        system_prompt="sys",
        transcript=transcript if transcript is not None else [],
        user_message="why is it failing?",
        max_turns=5,
        done_payload={"session_id": "abc"},
    )
    base.update(over)
    return base


async def test_event_vocabulary_matches_the_frontend() -> None:
    runtime = FakeRuntime(
        texts=("looking at the build",),
        tool_calls=(("jenkins_get_build_log", {"job_name": "app"}),),
        tool_results=(("jenkins_get_build_log", True),),
    )

    events = await _collect(**_kwargs(runtime))

    assert events[0] == {"type": "token", "content": "looking at the build"}
    assert events[1] == {
        "type": "tool_start",
        "tool_name": "jenkins_get_build_log",
        "tool_args": {"job_name": "app"},
    }
    assert events[2] == {"type": "tool_result", "tool_name": "jenkins_get_build_log", "success": True}
    assert events[-1] == {"type": "done", "session_id": "abc"}


async def test_a_failed_tool_reports_success_false() -> None:
    """The old paths tested result.startswith("Error"), which never matched either error
    string execute_tool actually returns — so every failed tool call rendered as a
    success. The outcome is now decided where it is known.
    """
    runtime = FakeRuntime(tool_results=(("k8s_get_pods", False),))

    events = await _collect(**_kwargs(runtime))

    assert {"type": "tool_result", "tool_name": "k8s_get_pods", "success": False} in events


async def test_the_final_answer_is_streamed_once() -> None:
    """The conclusion only exists when the turn ends, so it is not covered by on_text."""
    runtime = FakeRuntime(texts=("thinking",), content="the agent pod was OOMKilled")

    events = await _collect(**_kwargs(runtime))
    tokens = [e["content"] for e in events if e["type"] == "token"]

    assert tokens == ["thinking", "the agent pod was OOMKilled"]


async def test_an_answer_already_streamed_is_not_repeated() -> None:
    runtime = FakeRuntime(texts=("the whole answer",), content="the whole answer")

    events = await _collect(**_kwargs(runtime))

    assert [e["content"] for e in events if e["type"] == "token"] == ["the whole answer"]


async def test_a_failure_becomes_an_error_frame_not_a_500() -> None:
    """The stream is already open, so the only way to report is an error event."""
    runtime = FakeRuntime(raises=RuntimeError("401 OAuth access token is invalid"))

    events = await _collect(**_kwargs(runtime))

    assert events[-1]["type"] == "error"
    assert "401" in events[-1]["content"]


async def test_the_transcript_grows_by_one_exchange() -> None:
    runtime = FakeRuntime(content="because the disk filled")
    transcript: list[dict] = []

    await _collect(**_kwargs(runtime, transcript=transcript))

    assert transcript == [
        {"role": "user", "content": "why is it failing?"},
        {"role": "assistant", "content": "because the disk filled"},
    ]


async def test_the_transcript_is_not_written_when_the_turn_fails() -> None:
    """A failed turn must not persist a user message with no answer."""
    runtime = FakeRuntime(raises=RuntimeError("boom"))
    transcript: list[dict] = []

    await _collect(**_kwargs(runtime, transcript=transcript))

    assert transcript == []


async def test_prior_turns_are_replayed_as_prompt_context() -> None:
    """Sessions stay in Valkey rather than using CLI session resume: the CLI writes its
    sessions under CLAUDE_CONFIG_DIR, an emptyDir in the cluster that neither survives a
    restart nor is visible to a second replica.
    """
    runtime = FakeRuntime()
    transcript = [
        {"role": "user", "content": "what broke?"},
        {"role": "assistant", "content": "the jnlp container"},
    ]

    await _collect(**_kwargs(runtime, transcript=transcript))
    prompt = runtime.calls[0]["prompt"]

    assert "User: what broke?" in prompt
    assert "Assistant: the jnlp container" in prompt
    assert prompt.endswith("User: why is it failing?")


def test_render_transcript_skips_empty_and_non_conversational_entries() -> None:
    rendered = render_transcript(
        [
            {"role": "system", "content": "ignored"},
            {"role": "assistant", "content": ""},
            {"role": "user", "content": "kept"},
        ],
        "now",
    )

    assert "ignored" not in rendered
    assert rendered == "User: kept\n\nUser: now"


@pytest.mark.parametrize("payload", [{"session_id": "s1"}, {"fingerprint": "abc123"}])
async def test_done_carries_the_caller_s_identifier(payload) -> None:
    """chat.py sends session_id; the finding chat sends fingerprint."""
    events = await _collect(**_kwargs(FakeRuntime(), done_payload=payload))

    assert events[-1] == {"type": "done", **payload}
