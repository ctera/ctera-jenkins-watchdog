"""LiteLLM reasoning adapter with a bounded read-only operational tool loop."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4

from jenkins_watchdog.application.ports import ReasoningProgress
from jenkins_watchdog.application.reasoning import evidence_digest
from jenkins_watchdog.application.types import (
    ReasoningReply,
    TriageBatchResult,
    TriageCandidate,
    TriageRoute,
)
from jenkins_watchdog.domain.model import (
    Confidence,
    FindingObservation,
    Incident,
    Investigation,
    InvestigationStatus,
    LLMCall,
    ScanMode,
)
from jenkins_watchdog.domain.serialization import to_primitive
from jenkins_watchdog.infrastructure.tools import ReadOnlyToolRegistry, ToolExecution

INPUT_VERSION = "v2"
PROMPT_VERSION = "tool-agent-v3"
Completion = Callable[..., Awaitable[Any]]
CostCalculator = Callable[..., Any]

_PIPELINE_CATEGORIES = frozenset({"jenkins_failed_build", "jenkins_pipeline_pattern", "jenkins_build"})
_LOG_TOOLS = frozenset({"jenkins_get_build_log", "jenkins_analyze_build_failure"})
_TEST_FAILURE_CLAIM = re.compile(
    r"(?:\btests?\b[^.!?\n]{0,120}\b(?:failed|failing|failure)\b|"
    r"\b(?:failed|failing)\b(?:\s+\d+)?\s+(?:(?:unit|integration|system|e2e)\s+)?tests?\b|"
    r"\btest(?:ing)?[_ -]?failure\b)",
    re.IGNORECASE,
)


class LiteLLMReasoningAdapter:
    def __init__(
        self,
        *,
        model: str,
        fallback_models: tuple[str, ...],
        api_key: str,
        temperature: float,
        max_tokens: int,
        max_retries: int,
        max_tool_rounds: int = 15,
        max_deep_tool_rounds: int = 25,
        token_budget: int = 24_000,
        deep_token_budget: int = 40_000,
        tools: ReadOnlyToolRegistry | None = None,
        completion: Completion | None = None,
        cost_calculator: CostCalculator | None = None,
    ) -> None:
        self._models = (model, *fallback_models)
        self._api_key = api_key
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._max_retries = max_retries
        self._max_tool_rounds = max_tool_rounds
        self._max_deep_tool_rounds = max_deep_tool_rounds
        self._token_budget = max(0, token_budget)
        self._deep_token_budget = max(0, deep_token_budget)
        self._tools = tools
        self._completion = completion
        self._cost_calculator = cost_calculator

    async def triage_batch(self, candidates: tuple[TriageCandidate, ...]) -> TriageBatchResult:
        if not candidates:
            return TriageBatchResult(routes=())
        calls: list[LLMCall] = []
        payload = [
            {
                "incident_id": candidate.incident.id,
                "title": candidate.incident.title,
                "severity": candidate.incident.severity.value,
                "source": to_primitive(candidate.incident.source),
                "observations": [
                    {
                        "result": item.evidence.get("result"),
                        "novelty": item.evidence.get("novelty"),
                        "summary": item.summary,
                        "category": item.category,
                    }
                    for item in candidate.observations[:8]
                ],
            }
            for candidate in candidates
        ]
        response, _ = await self._call_with_fallback(
            messages=[
                {
                    "role": "system",
                    "content": _with_platform_prompt(
                        "Route uncertain Jenkins incidents for investigation. Return exactly one JSON object. "
                        "Select only incidents where live root-cause analysis is likely actionable; defer recovered, "
                        "expected, low-value, or non-blocking UNSTABLE results."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Return decisions as an array with incident_id, action (investigate or defer), and a short "
                        "reason. Provide one decision for every input. Input: "
                        f"{json.dumps(payload, separators=(',', ':'), ensure_ascii=False)}"
                    ),
                },
            ],
            tools=None,
            temperature=0.0,
            response_format={"type": "json_object"},
            purpose="triage",
            model_calls=calls,
        )
        parsed = _extract_triage_routes(response.choices[0].message.content, candidates)
        return TriageBatchResult(routes=parsed, model_calls=tuple(calls))

    async def investigate(
        self,
        incident: Incident,
        observations: tuple[FindingObservation, ...],
        *,
        context: dict[str, Any] | None = None,
        mode: ScanMode = ScanMode.REGULAR,
        on_progress: ReasoningProgress | None = None,
    ) -> Investigation:
        created_at = datetime.now(timezone.utc)
        evidence_hash = evidence_digest(observations)
        investigation_id = str(uuid4())
        calls: list[LLMCall] = []
        try:
            if self._tools is None:
                result, model, usage = await self._complete(
                    incident,
                    observations,
                    concise=False,
                    purpose="investigation",
                    model_calls=calls,
                )
                trace: list[dict[str, Any]] = []
                raw_reasoning = ""
            else:
                raw_reasoning, trace, model, usage = await self._run_tool_loop(
                    system_prompt=_investigation_system_prompt(mode),
                    user_prompt=_investigation_prompt(incident, observations, context=context, mode=mode),
                    mode=mode,
                    on_progress=on_progress,
                    final_only=True,
                    initial_tool_calls=_required_pipeline_tool_calls(observations, context=context, mode=mode),
                    purpose="investigation",
                    model_calls=calls,
                )
                result, extraction_model, extraction_usage = await self._extract(
                    raw_reasoning,
                    mode=mode,
                    model_calls=calls,
                )
                model = extraction_model or model
                usage = _merge_usage(usage, extraction_usage)
                result["tool_trace"] = trace
                result["tools_used"] = list(dict.fromkeys(item["tool"] for item in trace))
                result["agent_summary"] = raw_reasoning[:24_000 if mode is ScanMode.DEEP else 12_000]
                result["mode"] = mode.value
                _apply_quality_gates(result, observations, trace, context=context)
            confidence = Confidence(str(result["confidence"]).lower())
            result["deterministic_severity"] = incident.severity.value
            associated_calls = _associate_calls(calls, incident_id=incident.id, investigation_id=investigation_id)
            return Investigation(
                id=investigation_id,
                incident_id=incident.id,
                occurrence_id=incident.current_occurrence.id,
                status=InvestigationStatus.SUCCEEDED,
                evidence_hash=evidence_hash,
                input_version=INPUT_VERSION,
                prompt_version=PROMPT_VERSION,
                model=model,
                confidence=confidence,
                usage=_aggregate_calls(associated_calls) or usage,
                result=result,
                model_calls=associated_calls,
                created_at=created_at,
                completed_at=datetime.now(timezone.utc),
            )
        except Exception as exc:
            associated_calls = _associate_calls(calls, incident_id=incident.id, investigation_id=investigation_id)
            return Investigation(
                id=investigation_id,
                incident_id=incident.id,
                occurrence_id=incident.current_occurrence.id,
                status=InvestigationStatus.FAILED,
                evidence_hash=evidence_hash,
                input_version=INPUT_VERSION,
                prompt_version=PROMPT_VERSION,
                model=self._models[0],
                confidence=Confidence.LOW,
                usage=_aggregate_calls(associated_calls),
                result={"deterministic_severity": incident.severity.value, "mode": mode.value},
                model_calls=associated_calls,
                error_summary=f"{type(exc).__name__}: {exc}"[:500],
                created_at=created_at,
                completed_at=datetime.now(timezone.utc),
            )

    async def chat(
        self,
        *,
        message: str,
        incident: Incident | None = None,
        context: dict[str, Any] | None = None,
        history: tuple[dict[str, str], ...] = (),
        on_progress: ReasoningProgress | None = None,
    ) -> ReasoningReply:
        if not self._api_key:
            raise RuntimeError("reasoning integration is disabled")
        operational_context = context or {}
        if incident and not operational_context:
            operational_context = {
                "scope": "incident",
                "incident": {
                    "id": incident.id,
                    "title": incident.title,
                    "severity": incident.severity.value,
                    "status": incident.status.value,
                    "classification": incident.classification or "unknown",
                },
            }
        calls: list[LLMCall] = []
        if self._tools is None:
            response, _ = await self._call_with_fallback(
                messages=[
                    {"role": "system", "content": _chat_system_prompt()},
                    {
                        "role": "user",
                        "content": (
                            f"Question: {message}\nOperational snapshot: "
                            f"{json.dumps(to_primitive(operational_context), separators=(',', ':'), ensure_ascii=False)}"
                        ),
                    },
                ],
                temperature=0.0,
                tools=None,
                purpose="chat",
                model_calls=calls,
            )
            content = response.choices[0].message.content
            if not isinstance(content, str) or not content.strip():
                raise ValueError("reasoning response was empty")
            return ReasoningReply(
                content=content,
                model_calls=_associate_calls(calls, incident_id=incident.id if incident else None),
            )

        history_messages = [
            {"role": item["role"], "content": item["content"]}
            for item in history[-12:]
            if item.get("role") in {"user", "assistant"} and item.get("content")
        ]
        prompt = (
            f"Operator question: {message}\n\nCurrent durable snapshot (use tools for live facts):\n"
            f"{json.dumps(to_primitive(operational_context), separators=(',', ':'), ensure_ascii=False)}"
        )
        content, _, _, _ = await self._run_tool_loop(
            system_prompt=_chat_system_prompt(),
            user_prompt=prompt,
            mode=ScanMode.REGULAR,
            on_progress=on_progress,
            history=history_messages,
            summary_prompt=(
                "Answer the operator now using the evidence gathered. Keep the answer under 900 words, clearly "
                "separate verified facts from inference, state unavailable coverage, and complete the answer."
            ),
            final_only=True,
            purpose="chat",
            model_calls=calls,
        )
        if not content.strip():
            raise ValueError("reasoning response was empty")
        return ReasoningReply(
            content=content,
            model_calls=_associate_calls(calls, incident_id=incident.id if incident else None),
        )

    async def _run_tool_loop(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        mode: ScanMode,
        on_progress: ReasoningProgress | None,
        history: list[dict[str, str]] | None = None,
        summary_prompt: str | None = None,
        final_only: bool = False,
        initial_tool_calls: tuple[tuple[str, dict[str, Any]], ...] = (),
        purpose: str,
        model_calls: list[LLMCall],
    ) -> tuple[str, list[dict[str, Any]], str, dict[str, int]]:
        if not self._api_key:
            raise RuntimeError("reasoning integration is disabled")
        if self._tools is None:
            raise RuntimeError("operational tools are unavailable")
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            *(history or []),
            {"role": "user", "content": user_prompt},
        ]
        raw_parts: list[str] = []
        trace: list[dict[str, Any]] = []
        total_usage: dict[str, int] = {}
        last_model = self._models[0]
        max_rounds = self._max_deep_tool_rounds if mode is ScanMode.DEEP else self._max_tool_rounds
        token_budget = self._deep_token_budget if mode is ScanMode.DEEP else self._token_budget

        for name, arguments in initial_tool_calls:
            await _emit(
                on_progress,
                {"type": "tool_call", "round": 0, "tool": name, "arguments": arguments, "required": True},
            )
            execution = await self._tools.execute(name, arguments, mode=mode)
            trace.append(_trace_entry(execution, round_number=0))
            await _emit(
                on_progress,
                {
                    "type": "tool_result",
                    "round": 0,
                    "tool": name,
                    "ok": execution.ok,
                    "duration_ms": execution.duration_ms,
                    "preview": execution.output[:500],
                    "required": True,
                },
            )
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Mandatory pipeline evidence collected before reasoning. Use this live tool result in the "
                        f"assessment. Tool: {name}; arguments: "
                        f"{json.dumps(arguments, separators=(',', ':'), ensure_ascii=False)}\n{execution.output}"
                    ),
                }
            )

        for round_number in range(1, max_rounds + 1):
            response, last_model = await self._call_with_fallback(
                messages=messages,
                tools=list(self._tools.definitions),
                temperature=self._temperature,
                purpose=purpose,
                model_calls=model_calls,
            )
            total_usage = _merge_usage(total_usage, _usage(response))
            message = response.choices[0].message
            content = getattr(message, "content", None)
            tool_calls = _tool_calls(message)
            if tool_calls:
                _compact_tool_messages(messages, mode=mode)
            if tool_calls and token_budget and total_usage.get("total_tokens", 0) >= token_budget:
                if isinstance(content, str) and content.strip():
                    raw_parts.append(content.strip())
                await _emit(
                    on_progress,
                    {
                        "type": "reasoning",
                        "round": round_number,
                        "content": f"Token budget reached ({token_budget}); producing the final assessment.",
                    },
                )
                break
            assistant: dict[str, Any] = {"role": "assistant", "content": content or None}
            if tool_calls:
                assistant["tool_calls"] = tool_calls
            messages.append(assistant)
            if isinstance(content, str) and content.strip():
                raw_parts.append(content.strip())
                await _emit(on_progress, {"type": "reasoning", "round": round_number, "content": content[:1000]})
            if not tool_calls:
                final_content = content.strip() if isinstance(content, str) else ""
                output = final_content if final_only else "\n\n".join(raw_parts)
                return output, trace, last_model, total_usage

            for call in tool_calls:
                name = str(call["function"]["name"])
                arguments = _tool_arguments(call["function"].get("arguments"))
                await _emit(
                    on_progress,
                    {"type": "tool_call", "round": round_number, "tool": name, "arguments": arguments},
                )
                execution = await self._tools.execute(name, arguments, mode=mode)
                trace.append(_trace_entry(execution, round_number=round_number))
                await _emit(
                    on_progress,
                    {
                        "type": "tool_result",
                        "round": round_number,
                        "tool": name,
                        "ok": execution.ok,
                        "duration_ms": execution.duration_ms,
                        "preview": execution.output[:500],
                    },
                )
                messages.append(
                    {"role": "tool", "tool_call_id": call["id"], "content": execution.output}
                )

        messages.append(
            {
                "role": "user",
                "content": summary_prompt
                or "Stop calling tools. Return the final structured investigation JSON using the evidence gathered.",
            }
        )
        response, last_model = await self._call_with_fallback(
            messages=messages,
            tools=None,
            temperature=0.0,
            purpose=purpose,
            model_calls=model_calls,
        )
        total_usage = _merge_usage(total_usage, _usage(response))
        content = response.choices[0].message.content
        if isinstance(content, str) and content.strip():
            raw_parts.append(content.strip())
        output = content.strip() if final_only and isinstance(content, str) else "\n\n".join(raw_parts)
        return output, trace, last_model, total_usage

    async def _extract(
        self,
        raw_reasoning: str,
        *,
        mode: ScanMode,
        model_calls: list[LLMCall],
    ) -> tuple[dict[str, Any], str, dict[str, int]]:
        try:
            return _extract_assessment(raw_reasoning), "", {}
        except (ValueError, json.JSONDecodeError):
            pass
        usage: dict[str, int] = {}
        feedback = ""
        last_error: ValueError | json.JSONDecodeError | None = None
        for _ in range(2):
            response, model = await self._call_with_fallback(
                messages=[
                    {
                        "role": "system",
                        "content": "Extract only evidence supported by the agent trace. Return exactly one JSON object.",
                    },
                    {
                        "role": "user",
                        "content": (
                            f"{_extraction_prompt(mode)}{feedback}\n\nAgent trace:\n{raw_reasoning[:24_000]}"
                        ),
                    },
                ],
                tools=None,
                temperature=0.0,
                response_format={"type": "json_object"},
                purpose="extraction",
                model_calls=model_calls,
            )
            usage = _merge_usage(usage, _usage(response))
            try:
                return _extract_assessment(response.choices[0].message.content), model, usage
            except (ValueError, json.JSONDecodeError) as exc:
                last_error = exc
                feedback = (
                    "\nThe previous extraction was invalid. Ensure every required field is present, evidence is "
                    "an array, and confidence is low, medium, or high."
                )
        assert last_error is not None
        raise last_error

    async def _complete(
        self,
        incident: Incident,
        observations: tuple[FindingObservation, ...],
        *,
        concise: bool,
        purpose: str,
        model_calls: list[LLMCall],
    ) -> tuple[dict[str, Any], str, dict[str, int]]:
        if not self._api_key:
            raise RuntimeError("reasoning integration is disabled")
        response, model = await self._call_with_fallback(
            messages=[
                {
                    "role": "system",
                    "content": _with_platform_prompt(
                        "Return exactly one JSON object. Agent conclusions are advisory and cannot change "
                        "deterministic incident state."
                    ),
                },
                {"role": "user", "content": _snapshot_prompt(incident, observations, concise=concise)},
            ],
            tools=None,
            temperature=self._temperature,
            response_format={"type": "json_object"},
            purpose=purpose,
            model_calls=model_calls,
        )
        return _extract_assessment(response.choices[0].message.content), model, _usage(response)

    async def _call_with_fallback(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        temperature: float,
        response_format: dict[str, str] | None = None,
        purpose: str,
        model_calls: list[LLMCall],
    ) -> tuple[Any, str]:
        last_error: Exception | None = None
        for model in self._models:
            try:
                kwargs: dict[str, Any] = {
                    "model": model,
                    "messages": messages,
                    "api_key": self._api_key,
                    "temperature": temperature,
                    "max_tokens": self._max_tokens,
                    "num_retries": self._max_retries,
                }
                if tools:
                    kwargs["tools"] = tools
                if response_format:
                    kwargs["response_format"] = response_format
                response = await self._acompletion(**kwargs)
                model_calls.append(self._record_call(response, model=model, purpose=purpose))
                return response, model
            except Exception as exc:
                last_error = exc
        raise RuntimeError("all reasoning models failed") from last_error

    def _record_call(self, response: Any, *, model: str, purpose: str) -> LLMCall:
        usage = _usage(response)
        cost, source = self._estimated_cost(response, model=model)
        return LLMCall(
            id=str(uuid4()),
            purpose=purpose,
            model=model,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            cache_read_input_tokens=usage.get("cache_read_input_tokens", 0),
            cache_creation_input_tokens=usage.get("cache_creation_input_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
            estimated_cost_usd=cost,
            cost_source=source,
            created_at=datetime.now(timezone.utc),
        )

    def _estimated_cost(self, response: Any, *, model: str) -> tuple[Decimal | None, str]:
        try:
            calculator = self._cost_calculator
            if calculator is None:
                if getattr(response, "model", None) is None and not getattr(response, "_hidden_params", None):
                    return None, "unavailable"
                from litellm import completion_cost

                calculator = completion_cost
                self._cost_calculator = calculator
            value = calculator(completion_response=response, model=model)
            cost = Decimal(str(value))
            if not cost.is_finite() or cost < 0:
                raise InvalidOperation
            return cost, "litellm"
        except Exception:
            return None, "unavailable"

    async def _acompletion(self, **kwargs: Any) -> Any:
        if self._completion is None:
            from litellm import acompletion

            self._completion = acompletion
        return await self._completion(**kwargs)


def _investigation_system_prompt(mode: ScanMode) -> str:
    depth = (
        "Deep mode: read the full console log, compare several builds, inspect change diffs when present, assess "
        "blast radius, and include concrete verification steps."
        if mode is ScanMode.DEEP
        else "Regular mode: gather enough direct evidence to identify the mechanism without unnecessary calls."
    )
    return _with_platform_prompt(
        "You are a senior Jenkins and Kubernetes incident investigator. You have read-only tools. "
        "Actively gather current evidence; do not infer causality from names or labels. For pipeline failures, read "
        "the build log, inspect stages/tests/parameters, and compare job history before concluding. If an MR/PR is "
        "present, inspect its metadata and diff when credentials allow. Distinguish the first/root failure from "
        "propagated downstream failures. Never claim to execute a fix. Return a final JSON assessment. "
        f"{depth}"
    )


def _chat_system_prompt() -> str:
    return _with_platform_prompt(
        "You are the live Jenkins Watchdog operator assistant with read-only Jenkins, Kubernetes, Prometheus, and "
        "SCM tools. Use tools whenever the question asks about current state or needs evidence not in the durable "
        "snapshot. Explain which evidence supports the answer, distinguish observations from prior agent assessments, "
        "state unavailable coverage, cite build/job/incident identifiers, and never claim an action was executed."
    )


def _investigation_prompt(
    incident: Incident,
    observations: tuple[FindingObservation, ...],
    *,
    context: dict[str, Any] | None,
    mode: ScanMode,
) -> str:
    payload = _snapshot_payload(incident, observations)
    payload["mode"] = mode.value
    payload["operational_context"] = to_primitive(context or {})
    return (
        "Investigate this incident to root cause. Use direct tools, then return keys root_cause, evidence (array), "
        "impact, suggested_fix, fix_location, fix_verification, actionability (actionable|informational|unknown), "
        "classification (merge_request|infrastructure|test_failure|configuration|unknown), priority "
        "(low|warning|critical), and confidence (low|medium|high).\nInput: "
        f"{json.dumps(payload, separators=(',', ':'), ensure_ascii=False)}"
    )


def _snapshot_prompt(incident: Incident, observations: tuple[FindingObservation, ...], *, concise: bool) -> str:
    payload = _snapshot_payload(incident, observations)
    payload["concise"] = concise
    return (
        "Assess this Jenkins/Kubernetes incident. Return keys root_cause, evidence (array), impact, suggested_fix, "
        "actionability (actionable|informational|unknown), classification (merge_request|infrastructure|unknown), "
        "priority (low|warning|critical), and confidence (low|medium|high). Input: "
        f"{json.dumps(payload, separators=(',', ':'))}"
    )


def _snapshot_payload(incident: Incident, observations: tuple[FindingObservation, ...]) -> dict[str, Any]:
    ordered = sorted(observations, key=lambda item: item.observed_at, reverse=True)
    return {
        "incident": {
            "id": incident.id,
            "title": incident.title,
            "severity": incident.severity.value,
            "source": to_primitive(incident.source),
        },
        "observations": [
            {
                "rule_id": item.rule_id,
                "resource_id": item.resource_id,
                "category": item.category,
                "summary": item.summary,
                "evidence": to_primitive(item.evidence),
            }
            for item in ordered[:30]
        ],
        "omitted_observation_count": max(0, len(observations) - 30),
    }


def _extraction_prompt(mode: ScanMode) -> str:
    verification = " Include fix_verification with concrete validation steps." if mode is ScanMode.DEEP else ""
    return (
        "Extract root_cause, evidence (array of concrete facts), impact, suggested_fix, fix_location, "
        "fix_verification, actionability, classification, priority, and confidence. High confidence requires direct "
        "evidence of the causal mechanism; pipeline failures require an actual console-log read. Do not invent facts."
        f"{verification}"
    )


def _extract_assessment(content: Any) -> dict[str, Any]:
    if not isinstance(content, str):
        raise ValueError("reasoning content is not text")
    cleaned = content.strip()
    match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", cleaned, flags=re.DOTALL | re.IGNORECASE)
    if match:
        cleaned = match.group(1)
    value = json.loads(cleaned)
    required = {
        "root_cause",
        "evidence",
        "impact",
        "suggested_fix",
        "actionability",
        "classification",
        "priority",
        "confidence",
    }
    if not isinstance(value, dict) or not required.issubset(value):
        raise ValueError("reasoning response is missing required fields")
    if not isinstance(value["evidence"], list):
        raise ValueError("reasoning evidence must be an array")
    confidence = str(value["confidence"]).strip().lower()
    if confidence not in {item.value for item in Confidence}:
        value["confidence"] = Confidence.LOW.value
        warning = "Agent returned an unrecognized confidence value; confidence was downgraded to low."
        existing_gate = str(value.get("quality_gate") or "").strip()
        value["quality_gate"] = f"{existing_gate} {warning}".strip()
    else:
        value["confidence"] = confidence
    value.setdefault("fix_location", None)
    value.setdefault("fix_verification", None)
    return value


def _extract_triage_routes(
    content: Any,
    candidates: tuple[TriageCandidate, ...],
) -> tuple[TriageRoute, ...]:
    expected = {candidate.incident.id for candidate in candidates}
    try:
        value = json.loads(str(content or ""))
    except json.JSONDecodeError as exc:
        raise ValueError("triage response was not valid JSON") from exc
    decisions = value.get("decisions") if isinstance(value, dict) else None
    if not isinstance(decisions, list):
        raise ValueError("triage response is missing decisions")
    routes: dict[str, TriageRoute] = {}
    for item in decisions:
        if not isinstance(item, dict):
            continue
        incident_id = str(item.get("incident_id") or "")
        action = str(item.get("action") or "").lower()
        if incident_id not in expected or action not in {"investigate", "defer"}:
            continue
        routes[incident_id] = TriageRoute(
            incident_id=incident_id,
            action=action,
            reason=str(item.get("reason") or "No triage explanation was provided.")[:500],
        )
    return tuple(
        routes.get(
            candidate.incident.id,
            TriageRoute(
                incident_id=candidate.incident.id,
                action="defer",
                reason="Batch triage returned no valid decision for this incident.",
            ),
        )
        for candidate in candidates
    )


@lru_cache(maxsize=1)
def _platform_prompt() -> str:
    path = Path(__file__).resolve().parents[3] / "prompts" / "system.md"
    return path.read_text(encoding="utf-8").strip()


def _with_platform_prompt(instruction: str) -> str:
    return f"{_platform_prompt()}\n\n## Current task\n\n{instruction}"


def _tool_calls(message: Any) -> list[dict[str, Any]]:
    calls = getattr(message, "tool_calls", None) or []
    normalized = []
    for call in calls:
        if isinstance(call, dict):
            function = call.get("function") or {}
            normalized.append(
                {
                    "id": str(call.get("id") or uuid4()),
                    "type": "function",
                    "function": {
                        "name": function.get("name"),
                        "arguments": function.get("arguments") or "{}",
                    },
                }
            )
            continue
        function = getattr(call, "function", None)
        normalized.append(
            {
                "id": str(getattr(call, "id", uuid4())),
                "type": "function",
                "function": {
                    "name": getattr(function, "name", ""),
                    "arguments": getattr(function, "arguments", "{}"),
                },
            }
        )
    return normalized


def _tool_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _trace_entry(execution: ToolExecution, *, round_number: int) -> dict[str, Any]:
    return {
        "round": round_number,
        "tool": execution.name,
        "arguments": execution.arguments,
        "ok": execution.ok,
        "duration_ms": execution.duration_ms,
        "attempts": execution.attempts,
        "output": execution.output,
    }


def _apply_quality_gates(
    result: dict[str, Any],
    observations: tuple[FindingObservation, ...],
    trace: list[dict[str, Any]],
    *,
    context: dict[str, Any] | None,
) -> None:
    pipeline = _is_pipeline_investigation(observations, context=context)
    tools_used = {str(item.get("tool")) for item in trace if item.get("ok")}
    if pipeline and not tools_used.intersection(_LOG_TOOLS):
        result["confidence"] = Confidence.LOW.value
        log_attempts = [item for item in trace if item.get("tool") in _LOG_TOOLS]
        if log_attempts:
            attempt_count = max(int(item.get("attempts") or 1) for item in log_attempts)
            attempts_text = f" after {attempt_count} attempts" if attempt_count > 1 else ""
            result["quality_gate"] = (
                f"Jenkins console-log access was attempted but failed{attempts_text}; the proposed root cause is "
                "based on retained scan evidence and remains unverified against the live console log."
            )
        else:
            result["quality_gate"] = (
                "Pipeline root cause is unverified because the agent did not read a build console log."
            )
        return
    test_report_verified = any(item.get("tool") == "jenkins_get_test_report" and item.get("ok") for item in trace)
    if _claims_test_failure(result) and not test_report_verified and result.get("confidence") == Confidence.HIGH.value:
        result["confidence"] = Confidence.MEDIUM.value
        result["quality_gate"] = (
            "The build log confirms test failures, but Jenkins did not provide the failed-test report needed to verify "
            "the individual test mechanism."
        )


def _required_pipeline_tool_calls(
    observations: tuple[FindingObservation, ...],
    *,
    context: dict[str, Any] | None,
    mode: ScanMode,
) -> tuple[tuple[str, dict[str, Any]], ...]:
    if not _is_pipeline_investigation(observations, context=context):
        return ()
    build = _representative_pipeline_build(observations, context=context)
    if build is None:
        return ()
    job_name, build_number = build
    return (
        (
            "jenkins_get_build_log",
            {
                "job_name": job_name,
                "build_number": build_number,
                "tail_lines": 1500,
                "full": mode is ScanMode.DEEP,
            },
        ),
    )


def _is_pipeline_investigation(
    observations: tuple[FindingObservation, ...], *, context: dict[str, Any] | None
) -> bool:
    return any(item.category in _PIPELINE_CATEGORIES for item in observations) or bool(
        (context or {}).get("jenkins_builds")
    )


def _representative_pipeline_build(
    observations: tuple[FindingObservation, ...], *, context: dict[str, Any] | None
) -> tuple[str, int] | None:
    builds = (context or {}).get("jenkins_builds") or ()
    candidates = [item for item in builds if isinstance(item, Mapping)]
    direct = [item for item in candidates if not item.get("propagated_failure")]
    propagated = [item for item in candidates if item.get("propagated_failure")]
    for build in (*direct, *propagated):
        parsed = _build_reference(build)
        if parsed is not None:
            return parsed

    for observation in sorted(observations, key=lambda item: item.observed_at, reverse=True):
        parsed = _build_reference(observation.evidence)
        if parsed is not None:
            return parsed
        failed_builds = observation.evidence.get("failed_builds") or ()
        for build in failed_builds:
            if isinstance(build, Mapping) and (parsed := _build_reference(build)) is not None:
                return parsed
    return None


def _build_reference(value: Mapping[str, Any]) -> tuple[str, int] | None:
    job_name = value.get("job_name") or value.get("job_full_name")
    build_number = value.get("build_number") or value.get("latest_build") or value.get("number")
    try:
        number = int(build_number)
    except (TypeError, ValueError):
        return None
    if not job_name or number < 1:
        return None
    return str(job_name), number


def _claims_test_failure(result: dict[str, Any]) -> bool:
    classification = str(result.get("classification") or "").lower()
    if "test_failure" in classification:
        return True
    if classification not in {"merge_request", "unknown"}:
        return False
    diagnosis = " ".join(
        str(value)
        for key in ("root_cause", "evidence", "suggested_fix", "fix_location")
        if (value := result.get(key))
    )
    return _TEST_FAILURE_CLAIM.search(diagnosis) is not None


def _compact_tool_messages(messages: list[dict[str, Any]], *, mode: ScanMode) -> None:
    limit = 4_000 if mode is ScanMode.DEEP else 2_000
    for message in messages:
        if message.get("role") != "tool":
            continue
        content = message.get("content")
        if not isinstance(content, str) or len(content) <= limit:
            continue
        half = limit // 2
        message["content"] = (
            f"{content[:half]}\n... [prior tool result compacted to {limit} characters] ...\n{content[-half:]}"
        )


async def _emit(callback: ReasoningProgress | None, event: dict[str, Any]) -> None:
    if callback is not None:
        await callback(event)


def _usage(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    result: dict[str, int] = {}
    for target in (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    ):
        value = _field(usage, target)
        if isinstance(value, int):
            result[target] = value
    details = _field(usage, "prompt_tokens_details")
    cached = _field(details, "cached_tokens") if details is not None else None
    if isinstance(cached, int) and "cache_read_input_tokens" not in result:
        result["cache_read_input_tokens"] = cached
    if "total_tokens" not in result:
        result["total_tokens"] = result.get("prompt_tokens", 0) + result.get("completion_tokens", 0)
    return result


def _merge_usage(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
    result = dict(left)
    for key, value in right.items():
        result[key] = result.get(key, 0) + value
    return result


def _field(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _associate_calls(
    calls: list[LLMCall],
    *,
    incident_id: str | None,
    investigation_id: str | None = None,
) -> tuple[LLMCall, ...]:
    return tuple(
        replace(call, incident_id=incident_id, investigation_id=investigation_id)
        for call in calls
    )


def _aggregate_calls(calls: tuple[LLMCall, ...]) -> dict[str, Any]:
    if not calls:
        return {}
    costs = [call.estimated_cost_usd for call in calls if call.estimated_cost_usd is not None]
    return {
        "call_count": len(calls),
        "prompt_tokens": sum(call.prompt_tokens for call in calls),
        "completion_tokens": sum(call.completion_tokens for call in calls),
        "cache_read_input_tokens": sum(call.cache_read_input_tokens for call in calls),
        "cache_creation_input_tokens": sum(call.cache_creation_input_tokens for call in calls),
        "total_tokens": sum(call.total_tokens for call in calls),
        "estimated_cost_usd": float(sum(costs, Decimal("0"))) if costs else None,
        "priced_call_count": len(costs),
    }
