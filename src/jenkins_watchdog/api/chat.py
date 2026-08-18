"""SSE chat endpoint — conversational Jenkins investigation with tool-use."""

import json
import logging
import uuid

from fastapi import APIRouter
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from jenkins_watchdog.clients.valkey import get_valkey_client
from jenkins_watchdog.config import settings
from jenkins_watchdog.reasoning.chat_stream import stream_chat
from jenkins_watchdog.reasoning.engine import get_runtime

logger = logging.getLogger(__name__)

router = APIRouter()

_SESSION_TTL_SECONDS = 3600
# Prefix bumped with the transport swap: sessions stored by the LiteLLM path hold
# OpenAI-shaped messages with tool_calls/tool_call_id, which this path cannot replay.
# Ignoring them is cheaper and safer than parsing them, and they expire on their own.
_SESSION_KEY_PREFIX = "watchdog:chat:v2:"

SYSTEM_PROMPT = """You are an expert Jenkins and Kubernetes platform engineer investigating a Jenkins CI/CD environment running on a k3s cluster.
You have access to tools that query real-time state: Kubernetes API, Prometheus metrics, and Jenkins API.

When the user asks about agent health, build issues, or any infrastructure question:
1. Use the available tools to gather real evidence
2. Correlate findings across multiple data sources
3. Provide specific, actionable answers with evidence

Be concise but thorough. Show your reasoning. If something looks wrong, say what to fix and how."""


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


async def _load_session(session_id: str) -> list[dict]:
    client = await get_valkey_client()
    data = await client.get(f"{_SESSION_KEY_PREFIX}{session_id}")
    if data:
        return json.loads(data)
    return []


async def _save_session(session_id: str, messages: list[dict]) -> None:
    client = await get_valkey_client()
    await client.set(
        f"{_SESSION_KEY_PREFIX}{session_id}",
        json.dumps(messages, default=str),
        ex=_SESSION_TTL_SECONDS,
    )


@router.post("/chat")
async def chat(request: ChatRequest):
    session_id = request.session_id or str(uuid.uuid4())[:8]
    transcript = await _load_session(session_id)

    async def event_stream():
        runtime = get_runtime()
        if not runtime.configured:
            yield {
                "data": json.dumps(
                    {
                        "type": "error",
                        "content": (
                            "Reasoning is disabled: WATCHDOG_CLAUDE_CODE_OAUTH_TOKEN is not set. "
                            "Mint one with `claude setup-token`."
                        ),
                    }
                )
            }
            return

        async for event in stream_chat(
            runtime=runtime,
            system_prompt=SYSTEM_PROMPT,
            transcript=transcript,
            user_message=request.message,
            max_turns=settings.max_tool_rounds,
            done_payload={"session_id": session_id},
        ):
            yield event

        await _save_session(session_id, transcript)

    return EventSourceResponse(event_stream(), media_type="text/event-stream")
