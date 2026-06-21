"""SSE chat endpoint — conversational Jenkins investigation with tool-use."""

import asyncio
import json
import logging
import uuid

import litellm
from fastapi import APIRouter
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from jenkins_watchdog.clients.valkey import get_valkey_client
from jenkins_watchdog.config import settings
from jenkins_watchdog.tools import ALL_TOOL_DEFINITIONS, execute_tool

logger = logging.getLogger(__name__)

router = APIRouter()

_SESSION_TTL_SECONDS = 3600
_SESSION_KEY_PREFIX = "watchdog:chat:"

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
    return [{"role": "system", "content": SYSTEM_PROMPT}]


async def _save_session(session_id: str, messages: list[dict]) -> None:
    client = await get_valkey_client()
    await client.set(
        f"{_SESSION_KEY_PREFIX}{session_id}",
        json.dumps(messages, default=str),
        ex=_SESSION_TTL_SECONDS,
    )


def _get_model_chain() -> list[str]:
    models = [settings.llm_model]
    if settings.llm_fallback_models:
        models.extend(m.strip() for m in settings.llm_fallback_models.split(",") if m.strip())
    return models


@router.post("/chat")
async def chat(request: ChatRequest):
    session_id = request.session_id or str(uuid.uuid4())[:8]
    messages = await _load_session(session_id)
    messages.append({"role": "user", "content": request.message})

    async def event_stream():
        model_chain = _get_model_chain()
        tools_used: list[str] = []

        for iteration in range(settings.max_tool_rounds):
            try:
                response = await _call_llm(model_chain, messages)
            except Exception as e:
                yield {"data": json.dumps({"type": "error", "content": str(e)})}
                return

            choice = response.choices[0].message
            content = choice.content or ""
            tool_calls = choice.tool_calls or []

            assistant_msg: dict = {"role": "assistant", "content": content or None}
            if tool_calls:
                assistant_msg["tool_calls"] = [
                    {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in tool_calls
                ]
            messages.append(assistant_msg)

            if content:
                yield {"data": json.dumps({"type": "token", "content": content})}

            if not tool_calls:
                await _save_session(session_id, messages)
                yield {"data": json.dumps({"type": "done", "session_id": session_id})}
                return

            for tc in tool_calls:
                tool_name = tc.function.name
                try:
                    tool_args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    tool_args = {}

                yield {"data": json.dumps({"type": "tool_start", "tool_name": tool_name, "tool_args": tool_args})}

                result = await execute_tool(tool_name, tool_args)
                tools_used.append(tool_name)
                success = not result.startswith("Error") and not result.startswith("Unknown tool")

                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
                yield {"data": json.dumps({"type": "tool_result", "tool_name": tool_name, "success": success})}

        await _save_session(session_id, messages)
        yield {"data": json.dumps({"type": "token", "content": "\n\n(Reached tool call limit — showing partial results)"})}
        yield {"data": json.dumps({"type": "done", "session_id": session_id})}

    return EventSourceResponse(event_stream(), media_type="text/event-stream")


async def _call_llm(model_chain: list[str], messages: list[dict]):
    last_error = None
    for model in model_chain:
        try:
            return await litellm.acompletion(
                model=model,
                messages=messages,
                tools=ALL_TOOL_DEFINITIONS,
                temperature=settings.llm_temperature,
                max_tokens=settings.llm_max_tokens,
                api_key=settings.anthropic_api_key,
            )
        except Exception as e:
            last_error = e
            logger.warning("Chat LLM call failed for model %s: %s", model, e)
            await asyncio.sleep(1)
    raise last_error or RuntimeError("All models failed")
