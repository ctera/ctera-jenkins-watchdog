"""LLM triage pass — classify findings before deep investigation."""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import litellm

from jenkins_watchdog.checks.base import Finding
from jenkins_watchdog.config import settings

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent.parent.parent.parent / "prompts"

_TRIAGE_PROMPT = """You are triaging findings from a Jenkins CI/CD cluster scan. For each finding, classify it as one of:

- **INVESTIGATE**: Requires deep tool-based investigation (build logs, K8s events, agent state). Use for genuinely concerning pipeline or infrastructure issues.
- **DISMISS**: Known-normal behavior, false positive, or noise. Provide a brief reason.
- **CORRELATE**: Group with another finding (same root cause). Provide the index of the primary finding.

## Rules
1. Use the "Known normal behaviors" section from platform context — matching items are DISMISS.
2. A single offline agent with no failed builds and no queue congestion is often transient — DISMISS unless multiple agents or builds are affected.
3. Agent pod termination after successful build completion is normal — DISMISS unless paired with build failures.
4. Same error_signature or shared_failure_signature across jobs → CORRELATE to the most severe pipeline finding.
5. Build failure + agent pod crash on same node + K8s OOMKilling event → CORRELATE into one infrastructure finding.
6. consecutive_failures (3+) or shared_failure_signature → INVESTIGATE (high priority).
7. classified_failure with error_lines in context already has partial analysis — still INVESTIGATE if critical or regression.
8. Queue congestion with stuck builds → INVESTIGATE.
9. Parameter anomalies alone on MR jobs → INVESTIGATE only if builds are also failing.

## Response format
Return a JSON array with one object per finding, in the same order as input:
```json
[
  {"action": "INVESTIGATE"},
  {"action": "DISMISS", "reason": "Agent cycled normally after build"},
  {"action": "CORRELATE", "group": 0}
]
```

Only return the JSON array, no other text.

## Findings to triage:
"""


@dataclass
class DismissedFinding:
    finding: Finding
    reason: str


@dataclass
class CorrelationGroup:
    primary: Finding
    related: list[Finding] = field(default_factory=list)


@dataclass
class TriageResult:
    to_investigate: list[Finding] = field(default_factory=list)
    dismissed: list[DismissedFinding] = field(default_factory=list)
    correlations: list[CorrelationGroup] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated_cost_usd: float = 0.0


def _load_triage_context() -> str:
    prompt_file = PROMPTS_DIR / "system.md"
    if not prompt_file.exists():
        return ""

    content = prompt_file.read_text()
    sections = []
    for header in (
        "## Known normal behaviors",
        "## Jenkins pipeline failure patterns",
        "## Investigation priorities",
    ):
        start = content.find(header)
        if start < 0:
            continue
        next_h2 = content.find("\n## ", start + len(header))
        section = content[start : next_h2] if next_h2 > 0 else content[start:]
        sections.append(section.strip())

    return "\n\n".join(sections)


def _format_findings_for_triage(findings: list[Finding]) -> str:
    lines = []
    for i, f in enumerate(findings):
        ctx_summary = ""
        if f.context.get("error_signature"):
            ctx_summary += f" sig={f.context['error_signature']}"
        if f.context.get("pattern"):
            ctx_summary += f" pattern={f.context['pattern']}"
        if f.context.get("failure_class"):
            ctx_summary += f" class={f.context['failure_class']}"
        lines.append(
            f"[{i}] severity={f.severity} category={f.category} "
            f"resource={f.resource} symptom={f.symptom}{ctx_summary}"
        )
    return "\n".join(lines)


async def triage_findings(
    findings: list[Finding],
    cluster_context: str = "",
) -> TriageResult:
    """Send all findings to the LLM for cheap triage classification."""
    if not findings:
        return TriageResult()

    if not settings.anthropic_api_key:
        logger.warning("No API key — skipping triage, all findings proceed to investigation")
        return TriageResult(to_investigate=list(findings))

    context = _load_triage_context()
    prompt = _TRIAGE_PROMPT + _format_findings_for_triage(findings)

    messages = [
        {"role": "system", "content": f"Platform context:\n{context}\n\n{cluster_context}"},
        {"role": "user", "content": prompt},
    ]

    try:
        response = await litellm.acompletion(
            model=settings.llm_model,
            messages=messages,
            tools=None,
            temperature=0.0,
            max_tokens=2048,
            api_key=settings.anthropic_api_key,
        )
    except Exception as e:
        logger.error("Triage LLM call failed, all findings proceed to investigation: %s", e)
        return TriageResult(to_investigate=list(findings))

    content = response.choices[0].message.content or ""

    usage = getattr(response, "usage", None)
    prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
    completion_tokens = getattr(usage, "completion_tokens", 0) or 0
    try:
        cost = litellm.completion_cost(completion_response=response)
    except Exception:
        cost = (prompt_tokens * 3.0 / 1_000_000) + (completion_tokens * 15.0 / 1_000_000)

    classifications = _parse_triage_response(content, len(findings))
    logger.info(
        "Triage complete: %d findings → classifications: %s",
        len(findings),
        [c.get("action", "?") for c in classifications],
    )

    result = TriageResult(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        estimated_cost_usd=cost,
    )

    correlation_groups: dict[int, CorrelationGroup] = {}

    for i, classification in enumerate(classifications):
        if i >= len(findings):
            break

        action = classification.get("action", "INVESTIGATE").upper()

        if action == "DISMISS":
            result.dismissed.append(
                DismissedFinding(
                    finding=findings[i],
                    reason=classification.get("reason", "Dismissed by triage"),
                )
            )
        elif action == "CORRELATE":
            group_idx = classification.get("group", 0)
            if not isinstance(group_idx, int) or group_idx < 0 or group_idx >= len(findings):
                result.to_investigate.append(findings[i])
                continue
            if group_idx not in correlation_groups:
                correlation_groups[group_idx] = CorrelationGroup(primary=findings[group_idx])
            if i != group_idx:
                correlation_groups[group_idx].related.append(findings[i])
        else:
            result.to_investigate.append(findings[i])

    for group_idx, group in correlation_groups.items():
        if group.primary not in result.to_investigate and group.primary not in [d.finding for d in result.dismissed]:
            result.to_investigate.append(group.primary)
        group.primary.context["correlated_findings"] = [
            f"{f.resource}: {f.symptom}" for f in group.related
        ]
        group.primary.context["correlation_group_size"] = 1 + len(group.related)

    result.correlations = list(correlation_groups.values())
    return result


def _parse_triage_response(text: str, expected_count: int) -> list[dict]:
    bracket_start = text.find("[")
    bracket_end = text.rfind("]")
    if bracket_start >= 0 and bracket_end > bracket_start:
        try:
            parsed = json.loads(text[bracket_start : bracket_end + 1])
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass

    logger.warning("Failed to parse triage response, defaulting all to INVESTIGATE")
    return [{"action": "INVESTIGATE"} for _ in range(expected_count)]
