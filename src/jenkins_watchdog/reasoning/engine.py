"""LiteLLM tool-use reasoning engine — investigates findings by calling cluster tools."""

import asyncio
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import litellm

from jenkins_watchdog.api.models import Investigation
from jenkins_watchdog.checks.base import Finding
from jenkins_watchdog.config import settings
from jenkins_watchdog.tools import ALL_TOOL_DEFINITIONS, execute_tool

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent.parent.parent.parent / "prompts"

_RETRYABLE_EXCEPTIONS = (
    litellm.ServiceUnavailableError,
    litellm.InternalServerError,
    litellm.RateLimitError,
    litellm.Timeout,
)


def _get_model_chain() -> list[str]:
    """Build ordered model list from primary + fallbacks."""
    models = [settings.llm_model]
    if settings.llm_fallback_models:
        models.extend(m.strip() for m in settings.llm_fallback_models.split(",") if m.strip())
    return models


def _load_system_prompt() -> str:
    prompt_file = PROMPTS_DIR / "system.md"
    if prompt_file.exists():
        return prompt_file.read_text()
    return "You are a Jenkins platform engineer investigating CI/CD agent issues on a k3s cluster using tools."


def _format_investigation_prompt(finding: Finding) -> str:
    return (
        f"Investigate this Jenkins agent issue and determine the root cause:\n\n"
        f"- Severity: {finding.severity}\n"
        f"- Category: {finding.category}\n"
        f"- Resource: {finding.resource}\n"
        f"- Symptom: {finding.symptom}\n"
        f"- Context: {json.dumps(finding.context, default=str)}\n\n"
        f"Use tools to gather evidence. When done, explain what you found."
    )


ProgressCallback = Callable[[dict[str, Any]], Any]


async def investigate_finding(
    finding: Finding,
    on_progress: ProgressCallback | None = None,
    cluster_context: str = "",
) -> Investigation | None:
    """Run LiteLLM tool-use loop to investigate a single finding."""
    if not settings.anthropic_api_key:
        logger.warning("No Anthropic API key — skipping investigation for %s", finding.resource)
        return None

    async def _emit(event: dict) -> None:
        if on_progress:
            result = on_progress(event)
            if asyncio.iscoroutine(result):
                await result

    system_prompt = _load_system_prompt()
    if cluster_context:
        system_prompt = f"{system_prompt}\n\n{cluster_context}"

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": _format_investigation_prompt(finding)},
    ]
    tools_used: list[str] = []
    model_chain = _get_model_chain()
    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_cost = 0.0

    raw_reasoning_parts: list[str] = []

    for iteration in range(settings.max_tool_rounds):
        logger.debug("[investigate:%s] Round %d", finding.resource, iteration + 1)

        content, tool_calls, usage = await _call_with_fallback(
            model_chain=model_chain,
            messages=messages,
            tools=ALL_TOOL_DEFINITIONS,
        )
        total_prompt_tokens += usage[0]
        total_completion_tokens += usage[1]
        total_cost += usage[2]

        assistant_message: dict = {"role": "assistant", "content": content or None}
        if tool_calls:
            assistant_message["tool_calls"] = tool_calls
        messages.append(assistant_message)

        if content:
            raw_reasoning_parts.append(content)
            await _emit({"type": "reasoning", "content": content[:500]})

        if not tool_calls:
            break

        for tc in tool_calls:
            tool_name = tc["function"]["name"]
            try:
                tool_args = json.loads(tc["function"]["arguments"])
            except json.JSONDecodeError:
                tool_args = {}

            await _emit({"type": "tool_call", "tool": tool_name, "args": tool_args})
            logger.info("[investigate:%s] Calling tool: %s(%s)", finding.resource, tool_name, list(tool_args.keys()))
            result = await execute_tool(tool_name, tool_args)
            tools_used.append(tool_name)

            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result,
            })
    else:
        logger.warning("[investigate:%s] Hit max tool rounds (%d)", finding.resource, settings.max_tool_rounds)
        messages.append({
            "role": "user",
            "content": "Summarize your findings so far. What is the root cause, impact, and fix?",
        })
        content, _, usage = await _call_with_fallback(model_chain=model_chain, messages=messages, tools=None)
        total_prompt_tokens += usage[0]
        total_completion_tokens += usage[1]
        total_cost += usage[2]
        if content:
            raw_reasoning_parts.append(content)

    raw_reasoning = "\n\n".join(raw_reasoning_parts)
    inv = await _extract_structured_output(
        raw_reasoning=raw_reasoning,
        finding=finding,
        tools_used=tools_used,
        model_chain=model_chain,
    )
    total_prompt_tokens += inv.prompt_tokens
    total_completion_tokens += inv.completion_tokens
    total_cost += inv.estimated_cost_usd

    inv.prompt_tokens = total_prompt_tokens
    inv.completion_tokens = total_completion_tokens
    inv.estimated_cost_usd = round(total_cost, 4)
    inv.raw_reasoning = raw_reasoning
    return inv


_EXTRACTION_PROMPT = """Extract the investigation findings into this exact JSON format.
Use ONLY these 6 fields — no others:

{"root_cause":"One clear sentence explaining WHY this is happening","evidence":["specific data point 1","specific data point 2"],"impact":"What breaks or degrades if not fixed","suggested_fix":"Exact actionable fix: what to change, to what value","fix_location":"K8s resource or file path to modify","confidence":"high|medium|low"}

Rules:
- root_cause: one sentence explaining the ACTUAL MECHANISM (not just symptoms)
- evidence: JSON array of strings — concrete data points that PROVE the root cause
- impact: what happens if this is NOT fixed
- suggested_fix: actionable — specific values, commands, or config changes
- fix_location: exact K8s resource, file path, or Jenkins config to modify
- confidence: "high" ONLY if root cause confirmed by multiple data sources. "medium" if supported by data. "low" if uncertain.

Investigation findings to extract from:
"""


async def _extract_structured_output(
    raw_reasoning: str,
    finding: Finding,
    tools_used: list[str],
    model_chain: list[str],
) -> Investigation:
    """Pass 2: Extract structured JSON from raw investigation reasoning."""
    messages = [
        {"role": "user", "content": f"{_EXTRACTION_PROMPT}\n{raw_reasoning[:6000]}"},
    ]

    try:
        content, _, usage = await _call_with_fallback(
            model_chain=model_chain,
            messages=messages,
            tools=None,
            max_tokens=1024,
        )
        inv = _parse_investigation(content or "", finding.fingerprint, tools_used)
        inv.prompt_tokens = usage[0]
        inv.completion_tokens = usage[1]
        inv.estimated_cost_usd = usage[2]
        return inv
    except Exception as e:
        logger.error("[extract:%s] Extraction pass failed: %s", finding.resource, e)
        return Investigation(
            finding_fingerprint=finding.fingerprint,
            root_cause=raw_reasoning[:500] if raw_reasoning else "Extraction failed",
            evidence=[],
            impact="Unable to extract structured output",
            suggested_fix="Review raw reasoning",
            confidence="low",
            tools_used=tools_used,
            raw_reasoning=raw_reasoning,
        )


async def _call_with_fallback(
    model_chain: list[str],
    messages: list[dict],
    tools: list[dict] | None,
    max_tokens: int | None = None,
) -> tuple[str, list[dict], tuple[int, int, float]]:
    """Call LiteLLM with model fallback and retries."""
    last_error: Exception | None = None
    max_attempts = settings.llm_max_retries + 1

    for model in model_chain:
        for attempt in range(max_attempts):
            try:
                response = await litellm.acompletion(
                    model=model,
                    messages=messages,
                    tools=tools if tools else None,
                    temperature=settings.llm_temperature,
                    max_tokens=max_tokens or settings.llm_max_tokens,
                    api_key=settings.anthropic_api_key,
                )

                choice = response.choices[0].message
                content = choice.content or ""
                tool_calls = []

                if choice.tool_calls:
                    for tc in choice.tool_calls:
                        tool_calls.append({
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        })

                usage = getattr(response, "usage", None)
                prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
                completion_tokens = getattr(usage, "completion_tokens", 0) or 0

                try:
                    cost = litellm.completion_cost(completion_response=response)
                except Exception:
                    cost = (prompt_tokens * 3.0 / 1_000_000) + (completion_tokens * 15.0 / 1_000_000)

                return content, tool_calls, (prompt_tokens, completion_tokens, cost)

            except _RETRYABLE_EXCEPTIONS as e:
                last_error = e
                is_last_attempt = attempt == max_attempts - 1
                if not is_last_attempt:
                    backoff = min(2 ** attempt, 8)
                    logger.warning("Model %s attempt %d failed (retrying in %ds): %s", model, attempt + 1, backoff, e)
                    await asyncio.sleep(backoff)
                else:
                    logger.warning("Model %s exhausted retries, trying fallback: %s", model, e)
                    break

            except Exception as e:
                last_error = e
                logger.error("Non-retryable error on model %s: %s", model, e)
                break

    raise last_error or RuntimeError("All models failed")


def _extract_json_from_text(text: str) -> str | None:
    json_start = text.find("```json")
    if json_start >= 0:
        json_end = text.find("```", json_start + 7)
        if json_end >= 0:
            return text[json_start + 7 : json_end].strip()
        return text[json_start + 7 :].strip()

    brace_start = text.find("{")
    brace_end = text.rfind("}")
    if brace_start >= 0 and brace_end > brace_start:
        return text[brace_start : brace_end + 1]

    return None


def _repair_truncated_json(json_str: str) -> dict | None:
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        pass

    repaired = json_str.rstrip()
    if repaired.endswith(","):
        repaired = repaired[:-1]
    open_braces = repaired.count("{") - repaired.count("}")
    open_brackets = repaired.count("[") - repaired.count("]")
    if not repaired.endswith('"') and repaired.count('"') % 2 == 1:
        repaired += '"'
    repaired += "]" * open_brackets + "}" * open_braces

    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        return None


def _coerce_evidence(raw) -> list[str]:
    if isinstance(raw, list):
        return [str(item) for item in raw]
    if isinstance(raw, dict):
        return [f"{k}: {v}" for k, v in raw.items()]
    if isinstance(raw, str):
        return [raw]
    return []


_FIELD_ALIASES = {
    "fix": "suggested_fix",
    "suggestion": "suggested_fix",
    "recommended_fix": "suggested_fix",
    "remediation": "suggested_fix",
    "resolution": "suggested_fix",
    "location": "fix_location",
    "where": "fix_location",
    "file": "fix_location",
}


def _normalize_fields(data: dict) -> dict:
    normalized = {}
    for key, value in data.items():
        canonical = _FIELD_ALIASES.get(key, key)
        if canonical not in normalized:
            normalized[canonical] = value
    return normalized


def _parse_investigation(text: str, fingerprint: str, tools_used: list[str]) -> Investigation:
    json_str = _extract_json_from_text(text)

    if json_str:
        data = _repair_truncated_json(json_str)
        if data and isinstance(data, dict) and "root_cause" in data:
            data = _normalize_fields(data)
            confidence = data.get("confidence", "medium")
            if confidence not in ("high", "medium", "low"):
                confidence = "medium"
            return Investigation(
                finding_fingerprint=fingerprint,
                root_cause=str(data.get("root_cause", "Unknown")),
                evidence=_coerce_evidence(data.get("evidence", [])),
                impact=str(data.get("impact", "Unknown impact")),
                suggested_fix=str(data.get("suggested_fix", "No fix suggested")),
                fix_location=data.get("fix_location"),
                confidence=confidence,
                tools_used=tools_used,
                raw_reasoning=text,
            )
        logger.warning("Failed to parse investigation JSON for %s", fingerprint)

    return Investigation(
        finding_fingerprint=fingerprint,
        root_cause=text[:500] if text else "Parse error",
        evidence=[],
        impact="Unable to parse structured output",
        suggested_fix="Review raw reasoning",
        confidence="low",
        tools_used=tools_used,
        raw_reasoning=text,
    )
