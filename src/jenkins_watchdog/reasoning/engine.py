"""LiteLLM tool-use reasoning engine — investigates findings by calling cluster tools."""

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import litellm

from jenkins_watchdog.api.models import Investigation
from jenkins_watchdog.checks.base import Finding
from jenkins_watchdog.config import settings
from jenkins_watchdog.scan_options import get_scan_options
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
    models = [settings.llm_model]
    if settings.llm_fallback_models:
        models.extend(m.strip() for m in settings.llm_fallback_models.split(",") if m.strip())
    return models


def _load_system_prompt(*, deep: bool = False) -> str:
    prompt_file = PROMPTS_DIR / "system.md"
    base = prompt_file.read_text() if prompt_file.exists() else (
        "You are a Jenkins platform engineer investigating CI/CD issues on a k3s cluster using tools."
    )
    if not deep:
        return base
    return (
        f"{base}\n\n"
        "## Deep scan mode\n"
        "This is a thorough deep scan. Take your time and use more tool rounds:\n"
        "- Read full build console logs, not just tails — find the FIRST error and trace upstream\n"
        "- Compare multiple recent builds to confirm recurring vs one-off failures\n"
        "- Assess blast radius: which jobs, MRs, or agents are affected\n"
        "- Provide fix verification steps: how to confirm the fix worked (re-run build, check metric, etc.)\n"
        "- Cross-correlate with other findings in this scan before concluding\n"
    )


def _format_investigation_prompt(finding: Finding, all_findings: list[Finding] | None = None, *, deep: bool = False) -> str:
    prompt = (
        f"Investigate this Jenkins/CI issue and determine the root cause:\n\n"
        f"- Severity: {finding.severity}\n"
        f"- Category: {finding.category}\n"
        f"- Resource: {finding.resource}\n"
        f"- Symptom: {finding.symptom}\n"
        f"- Context: {json.dumps(finding.context, default=str)}\n\n"
    )

    if finding.context.get("correlated_findings"):
        prompt += (
            "## Correlated findings (same incident):\n"
            + "\n".join(f"- {c}" for c in finding.context["correlated_findings"])
            + "\n\n"
        )

    if finding.category in ("jenkins_failed_build", "jenkins_pipeline_pattern"):
        prompt += (
            "## Investigation checklist for pipeline failures:\n"
            "1. Read build console log (jenkins_get_build_log) — find the FIRST error, not just the last line\n"
            "2. Compare with previous builds (jenkins_get_job_build_history) — is this recurring?\n"
            "3. Check build parameters (jenkins_get_build) — wrong branch, missing params?\n"
            "4. If agent/infrastructure suspected: check which node ran the build, then k8s pod logs/events\n"
            "5. Classify: test failure vs compilation vs infra vs config — explain WHY\n\n"
        )

    prompt += "Use tools to gather evidence. When done, explain what you found."

    if deep:
        prompt += (
            "\n\n## Deep scan requirements:\n"
            "- Perform full root cause analysis with impact assessment\n"
            "- Include concrete fix verification steps (what to re-run or check after applying the fix)\n"
            "- Use jenkins_get_build_log without relying on truncated output\n"
        )

    if all_findings and len(all_findings) > 1:
        others = [f for f in all_findings if f.fingerprint != finding.fingerprint]
        if others:
            lines = [f"- [{f.severity}] {f.resource}: {f.symptom}" for f in others[:15]]
            prompt += (
                f"\n\n## Other findings in this scan (for correlation):\n"
                + "\n".join(lines)
                + "\n\nConsider whether this issue is related to or caused by any of the above."
            )

    return prompt


ProgressCallback = Callable[[dict[str, Any]], Any]


@dataclass
class ToolLoopResult:
    raw_reasoning: str = ""
    tools_used: list[str] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0


async def run_tool_loop(
    *,
    system_prompt: str,
    user_prompt: str,
    max_rounds: int | None = None,
    max_tokens: int | None = None,
    model_chain: list[str] | None = None,
    on_progress: ProgressCallback | None = None,
    label: str = "tool_loop",
    summary_prompt: str = "Summarize your findings so far. What is the root cause, impact, and fix?",
) -> ToolLoopResult:
    """Reusable LLM tool-use loop for investigations."""
    if not settings.anthropic_api_key:
        logger.warning("[%s] No Anthropic API key — skipping", label)
        return ToolLoopResult()

    async def _emit(event: dict) -> None:
        if on_progress:
            result = on_progress(event)
            if asyncio.iscoroutine(result):
                await result

    max_rounds = max_rounds or settings.max_tool_rounds
    model_chain = model_chain or _get_model_chain()

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    tools_used: list[str] = []
    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_cost = 0.0
    raw_reasoning_parts: list[str] = []

    for iteration in range(max_rounds):
        logger.debug("[%s] Round %d", label, iteration + 1)

        content, tool_calls, usage = await _call_with_fallback(
            model_chain=model_chain,
            messages=messages,
            tools=ALL_TOOL_DEFINITIONS,
            max_tokens=max_tokens,
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
            logger.info("[%s] Calling tool: %s(%s)", label, tool_name, list(tool_args.keys()))
            result = await execute_tool(tool_name, tool_args)
            tools_used.append(tool_name)

            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result,
            })
    else:
        logger.warning("[%s] Hit max tool rounds (%d)", label, max_rounds)
        messages.append({"role": "user", "content": summary_prompt})
        content, _, usage = await _call_with_fallback(
            model_chain=model_chain, messages=messages, tools=None, max_tokens=max_tokens,
        )
        total_prompt_tokens += usage[0]
        total_completion_tokens += usage[1]
        total_cost += usage[2]
        if content:
            raw_reasoning_parts.append(content)

    return ToolLoopResult(
        raw_reasoning="\n\n".join(raw_reasoning_parts),
        tools_used=tools_used,
        prompt_tokens=total_prompt_tokens,
        completion_tokens=total_completion_tokens,
        cost_usd=total_cost,
    )


async def investigate_finding(
    finding: Finding,
    on_progress: ProgressCallback | None = None,
    cluster_context: str = "",
    all_findings: list[Finding] | None = None,
) -> Investigation | None:
    """Run LiteLLM tool-use loop to investigate a single finding."""
    scan_opts = get_scan_options()
    system_prompt = _load_system_prompt(deep=scan_opts.deep)
    if cluster_context:
        system_prompt = f"{system_prompt}\n\n{cluster_context}"

    summary_prompt = (
        "Summarize your findings so far. What is the root cause, impact, fix, and how to verify the fix worked?"
        if scan_opts.deep
        else "Summarize your findings so far. What is the root cause, impact, and fix?"
    )

    loop_result = await run_tool_loop(
        system_prompt=system_prompt,
        user_prompt=_format_investigation_prompt(finding, all_findings, deep=scan_opts.deep),
        max_rounds=scan_opts.max_tool_rounds,
        on_progress=on_progress,
        label=f"investigate:{finding.resource}",
        summary_prompt=summary_prompt,
    )

    if not loop_result.raw_reasoning and not loop_result.tools_used:
        return None

    model_chain = _get_model_chain()
    inv = await _extract_structured_output(
        raw_reasoning=loop_result.raw_reasoning,
        finding=finding,
        tools_used=loop_result.tools_used,
        model_chain=model_chain,
        deep=scan_opts.deep,
    )

    inv.prompt_tokens = loop_result.prompt_tokens + inv.prompt_tokens
    inv.completion_tokens = loop_result.completion_tokens + inv.completion_tokens
    inv.estimated_cost_usd = round(loop_result.cost_usd + inv.estimated_cost_usd, 4)
    inv.raw_reasoning = loop_result.raw_reasoning
    return inv


_EXTRACTION_PROMPT = """Extract the investigation findings into this exact JSON format.
Use ONLY these 6 fields — no others:

{"root_cause":"One clear sentence explaining WHY this is happening","evidence":["specific data point 1","specific data point 2"],"impact":"What breaks or degrades if not fixed","suggested_fix":"Exact actionable fix: what to change, to what value","fix_location":"K8s resource or file path to modify","confidence":"high|medium|low"}

Rules:
- root_cause: one sentence explaining the ACTUAL MECHANISM (not just symptoms). Bad: "Build failed". Good: "Maven test phase fails because integration test cannot reach mock server — connection refused on port 8081"
- evidence: JSON array of strings — concrete data points (log lines, build numbers, node names, metric values) that PROVE the root cause
- impact: what happens if NOT fixed — blocked MRs, recurring failures, agent pool exhaustion, etc.
- suggested_fix: actionable — specific Jenkinsfile change, parameter value, resource limit, or config. NEVER say "investigate further" or "check logs"
- fix_location: exact Jenkins job, Jenkinsfile path, K8s resource, or pipeline stage to modify
- confidence: "high" ONLY if root cause confirmed by build log + supporting data. "medium" if supported by logs but mechanism unclear. "low" if uncertain or might be transient.

Quality gates — set confidence="low" if ANY apply:
- You did not read the actual build console log for pipeline failures
- The failure might be a flaky test with no recurring pattern
- You are treating a downstream symptom (agent offline) as root cause when the build log shows a test failure
- Your fix targets infrastructure when the build log shows an application/test error

Investigation findings to extract from:
"""


_EXTRACTION_PROMPT_DEEP = """Extract the investigation findings into this exact JSON format.
Use ONLY these 7 fields — no others:

{"root_cause":"One clear sentence explaining WHY this is happening","evidence":["specific data point 1","specific data point 2"],"impact":"What breaks or degrades if not fixed","suggested_fix":"Exact actionable fix: what to change, to what value","fix_location":"K8s resource or file path to modify","fix_verification":"Steps to confirm the fix worked (re-run job, check metric, etc.)","confidence":"high|medium|low"}

Rules:
- root_cause: one sentence explaining the ACTUAL MECHANISM (not just symptoms)
- evidence: JSON array of strings — concrete data points that PROVE the root cause
- impact: what happens if NOT fixed
- suggested_fix: actionable — specific Jenkinsfile change, parameter value, resource limit, or config
- fix_location: exact Jenkins job, Jenkinsfile path, K8s resource, or pipeline stage to modify
- fix_verification: concrete steps to validate the fix after applying it
- confidence: "high" ONLY if root cause confirmed by build log + supporting data

Investigation findings to extract from:
"""


async def _extract_structured_output(
    raw_reasoning: str,
    finding: Finding,
    tools_used: list[str],
    model_chain: list[str],
    *,
    deep: bool = False,
) -> Investigation:
    extraction_prompt = _EXTRACTION_PROMPT_DEEP if deep else _EXTRACTION_PROMPT
    messages = [
        {"role": "user", "content": f"{extraction_prompt}\n{raw_reasoning[:12000 if deep else 8000]}"},
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
            suggested_fix = str(data.get("suggested_fix", "No fix suggested"))
            fix_verification = data.get("fix_verification")
            if fix_verification:
                suggested_fix = f"{suggested_fix}\n\nVerification: {fix_verification}"
            return Investigation(
                finding_fingerprint=fingerprint,
                root_cause=str(data.get("root_cause", "Unknown")),
                evidence=_coerce_evidence(data.get("evidence", [])),
                impact=str(data.get("impact", "Unknown impact")),
                suggested_fix=suggested_fix,
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
