"""The SSE chat bridge, shared by the global chat and the per-finding chat.

Both endpoints stream the same event vocabulary to the frontend --
``token`` / ``tool_start`` / ``tool_result`` / ``done`` / ``error`` -- and both used to
own a near-identical copy of the tool loop. There is one copy now.

Why a queue: tools execute inside the SDK's own task, so the callbacks that know a tool
started or finished cannot ``yield`` into this generator directly. The agent runs as a
task and pushes events onto a queue that the generator drains, which also means the
stream stays live while a slow tool runs instead of buffering to the end of the turn.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from jenkins_watchdog.reasoning.claude_agent import ClaudeCodeRuntime
from jenkins_watchdog.scan_options import ScanOptions, get_scan_options

logger = logging.getLogger(__name__)

_SENTINEL = object()


def render_transcript(transcript: list[dict], user_message: str) -> str:
    """Replay prior turns as prompt context.

    Sessions stay in Valkey rather than using the CLI's own session resume: the CLI writes
    its session files under CLAUDE_CONFIG_DIR, which in the cluster is an emptyDir -- they
    would not survive a restart and would not be visible to a second replica.
    """
    lines = []
    for message in transcript:
        role = message.get("role")
        content = (message.get("content") or "").strip()
        if not content or role not in ("user", "assistant"):
            continue
        lines.append(f"{'User' if role == 'user' else 'Assistant'}: {content}")
    lines.append(f"User: {user_message}")
    return "\n\n".join(lines)


async def stream_chat(
    *,
    runtime: ClaudeCodeRuntime,
    system_prompt: str,
    transcript: list[dict],
    user_message: str,
    max_turns: int,
    done_payload: dict[str, Any],
    scan_options: ScanOptions | None = None,
) -> AsyncIterator[dict]:
    """Drive one chat turn, yielding SSE payloads and appending to ``transcript``.

    The caller persists ``transcript`` after the stream completes -- doing it here would
    make the two endpoints' different TTLs and key prefixes this module's problem.
    """
    queue: asyncio.Queue = asyncio.Queue()

    async def emit(event: dict) -> None:
        await queue.put(event)

    async def on_text(text: str) -> None:
        await emit({"type": "token", "content": text})

    async def on_tool_call(name: str, args: dict) -> None:
        await emit({"type": "tool_start", "tool_name": name, "tool_args": args})

    async def on_tool_result(name: str, ok: bool) -> None:
        await emit({"type": "tool_result", "tool_name": name, "success": ok})

    async def run() -> None:
        try:
            return await runtime.run_agent(
                system_prompt=system_prompt,
                prompt=render_transcript(transcript, user_message),
                scan_options=scan_options or get_scan_options(),
                max_turns=max_turns,
                on_text=on_text,
                on_tool_call=on_tool_call,
                on_tool_result=on_tool_result,
            )
        finally:
            await queue.put(_SENTINEL)

    task = asyncio.create_task(run())
    try:
        while True:
            event = await queue.get()
            if event is _SENTINEL:
                break
            yield {"data": json.dumps(event)}

        turn = await task
    except Exception as e:  # noqa: BLE001 - any failure is an SSE error frame, never a 500
        logger.error("Chat turn failed: %s", e)
        if not task.done():
            task.cancel()
        yield {"data": json.dumps({"type": "error", "content": str(e)})}
        return

    transcript.append({"role": "user", "content": user_message})
    transcript.append({"role": "assistant", "content": turn.content})

    # The agent's conclusion has not been streamed yet: on_text carries the narration it
    # writes while exploring, and the final answer only exists once the turn ends.
    if turn.content and turn.content not in turn.narration:
        yield {"data": json.dumps({"type": "token", "content": turn.content})}

    yield {"data": json.dumps({"type": "done", **done_payload})}
