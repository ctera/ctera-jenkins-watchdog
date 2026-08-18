"""Claude Code reasoning engine — investigates findings by calling cluster tools."""

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from jenkins_watchdog.api.models import Investigation
from jenkins_watchdog.checks.base import Finding
from jenkins_watchdog.config import settings
from jenkins_watchdog.reasoning.claude_agent import ClaudeCodeRuntime
from jenkins_watchdog.reasoning.prompt_files import read_prompt
from jenkins_watchdog.scan_options import ScanOptions, get_scan_options

logger = logging.getLogger(__name__)

_runtime: ClaudeCodeRuntime | None = None


def get_runtime() -> ClaudeCodeRuntime:
    """The shared Claude Code runtime.

    Built lazily rather than at import: its semaphore binds to the event loop that creates
    it, and importing this module does not mean an event loop exists yet.
    """
    global _runtime
    if _runtime is None:
        _runtime = ClaudeCodeRuntime()
    return _runtime


def _load_system_prompt(*, deep: bool = False) -> str:
    base = read_prompt("system.md") or (
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
                "\n\n## Other findings in this scan (for correlation):\n"
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
    scan_options: ScanOptions | None = None,
    on_progress: ProgressCallback | None = None,
    label: str = "tool_loop",
    summary_prompt: str = "Summarize your findings so far. What is the root cause, impact, and fix?",
) -> ToolLoopResult:
    """Reusable agentic tool-use loop for investigations.

    The SDK owns the turns now, but the contract is unchanged: on a missing credential this
    logs and returns an empty result rather than raising, and investigate_finding turns
    that into "no investigation" so a scan still reports its findings.
    """
    runtime = get_runtime()
    if not runtime.configured:
        logger.warning("[%s] No Claude Code OAuth token — skipping", label)
        return ToolLoopResult()

    async def _emit(event: dict) -> None:
        if on_progress:
            result = on_progress(event)
            if asyncio.iscoroutine(result):
                await result

    scan_options = scan_options or get_scan_options()
    # An SDK turn counts assistant messages, not tool rounds, so this is a looser bound
    # than max_tool_rounds was — a turn may carry several parallel tool calls.
    max_turns = max_rounds or settings.max_tool_rounds

    async def on_text(text: str) -> None:
        await _emit({"type": "reasoning", "content": text[:500]})

    async def on_tool_call(name: str, args: dict[str, Any]) -> None:
        logger.info("[%s] Calling tool: %s(%s)", label, name, list(args.keys()))
        await _emit({"type": "tool_call", "tool": name, "args": args})

    try:
        turn = await runtime.run_agent(
            system_prompt=system_prompt,
            prompt=user_prompt,
            scan_options=scan_options,
            max_turns=max_turns,
            summary_prompt=summary_prompt,
            on_text=on_text,
            on_tool_call=on_tool_call,
        )
    except Exception as e:
        logger.error("[%s] Agent run failed: %s", label, e)
        return ToolLoopResult()

    return ToolLoopResult(
        raw_reasoning=turn.content,
        tools_used=turn.tools_used,
        prompt_tokens=turn.prompt_tokens,
        completion_tokens=turn.completion_tokens,
        cost_usd=turn.cost_usd,
    )


async def investigate_finding(
    finding: Finding,
    on_progress: ProgressCallback | None = None,
    cluster_context: str = "",
    all_findings: list[Finding] | None = None,
) -> Investigation | None:
    """Run the Claude Code tool-use loop to investigate a single finding."""
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
        scan_options=scan_opts,
        on_progress=on_progress,
        label=f"investigate:{finding.resource}",
        summary_prompt=summary_prompt,
    )

    if not loop_result.raw_reasoning and not loop_result.tools_used:
        return None

    inv = await _extract_structured_output(
        raw_reasoning=loop_result.raw_reasoning,
        finding=finding,
        tools_used=loop_result.tools_used,
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
    *,
    deep: bool = False,
) -> Investigation:
    extraction_prompt = _EXTRACTION_PROMPT_DEEP if deep else _EXTRACTION_PROMPT
    prompt = f"{extraction_prompt}\n{raw_reasoning[:12000 if deep else 8000]}"

    try:
        turn = await get_runtime().complete(
            system_prompt="You extract structured JSON from an investigation write-up.",
            prompt=prompt,
        )
        inv = _parse_investigation(turn.content or "", finding.fingerprint, tools_used)
        inv.prompt_tokens = turn.prompt_tokens
        inv.completion_tokens = turn.completion_tokens
        inv.estimated_cost_usd = turn.cost_usd
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
