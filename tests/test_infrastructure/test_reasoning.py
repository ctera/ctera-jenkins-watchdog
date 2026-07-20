from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from jenkins_watchdog.application.types import TriageCandidate
from jenkins_watchdog.domain.model import FindingObservation, Incident, InvestigationStatus, ScanMode, Severity
from jenkins_watchdog.infrastructure.reasoning import (
    _MANDATORY_EVIDENCE_PREFIX,
    LiteLLMReasoningAdapter,
    _apply_quality_gates,
    _bound_round_results,
    _compact_tool_messages,
    _extract_assessment,
    _investigation_system_prompt,
    _usage,
)
from jenkins_watchdog.infrastructure.tools import ToolExecution, _redact

NOW = datetime(2026, 7, 13, tzinfo=timezone.utc)


def test_persisted_tool_output_redacts_common_credentials() -> None:
    value = _redact('Authorization: Bearer abc.def password="hunter2" api_key=secret-value')
    assert "abc.def" not in value
    assert "hunter2" not in value
    assert "secret-value" not in value
    assert value.count("[REDACTED]") == 3


def observation() -> FindingObservation:
    return FindingObservation(
        scan_id="scan",
        check_name="check",
        rule_id="rule.v1",
        resource_id="resource",
        severity=Severity.WARNING,
        category="k8s_node",
        summary="pressure",
        observed_at=NOW,
    )


def test_structured_extraction_accepts_json_fence_and_handles_malformed_values() -> None:
    payload = (
        '{"root_cause":"x","evidence":[],"impact":"y","suggested_fix":"z",'
        '"actionability":"actionable","classification":"infrastructure",'
        '"priority":"warning","confidence":"medium"}'
    )
    assert _extract_assessment(f"```json\n{payload}\n```")["confidence"] == "medium"
    with pytest.raises(ValueError):
        _extract_assessment('{"root_cause":"missing fields"}')
    conservative = _extract_assessment(payload.replace('"medium"', '"certain"'))
    assert conservative["confidence"] == "low"
    assert "unrecognized confidence" in conservative["quality_gate"]


def test_platform_instructions_are_restored_from_the_shared_prompt() -> None:
    prompt = _investigation_system_prompt(ScanMode.REGULAR)
    assert "Pipeline failures first" in prompt
    assert "Read actual build logs for pipeline failures" in prompt
    assert "## Current task" in prompt


def test_usage_extracts_provider_cache_tokens() -> None:
    item = response("ok")
    item.usage.prompt_tokens_details = SimpleNamespace(cached_tokens=3)
    item.usage.cache_creation_input_tokens = 2
    assert _usage(item) == {
        "prompt_tokens": 5,
        "completion_tokens": 7,
        "total_tokens": 12,
        "cache_read_input_tokens": 3,
        "cache_creation_input_tokens": 2,
    }


@pytest.mark.asyncio
async def test_disabled_reasoning_persists_a_low_confidence_partial_value() -> None:
    item = observation()
    incident = Incident.open_new(
        id="incident",
        correlation_rule_id="stable_finding",
        correlation_key=item.stable_identity,
        observation=item,
        opened_at=NOW,
    )
    adapter = LiteLLMReasoningAdapter(
        model="model",
        fallback_models=(),
        api_key="",
        temperature=0.1,
        max_tokens=100,
        max_retries=0,
    )

    result = await adapter.investigate(incident, (item,))

    assert result.status == InvestigationStatus.PARTIAL
    assert result.confidence.value == "low"
    assert "disabled" in (result.error_summary or "")
    assert result.result["completion_status"] == "partial"
    assert result.result["evidence"] == ("resource: pressure",)


def response(content: str, *, total_tokens: int = 12, tool_calls=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=tool_calls or []))],
        usage=SimpleNamespace(prompt_tokens=5, completion_tokens=7, total_tokens=total_tokens),
    )


@pytest.mark.asyncio
async def test_reasoning_falls_back_accounts_usage_and_supports_triage_and_chat() -> None:
    payload = (
        '{"root_cause":"compiler","evidence":["line"],"impact":"blocked",'
        '"suggested_fix":"fix type","actionability":"actionable",'
        '"classification":"merge_request","priority":"warning","confidence":"medium"}'
    )
    models = []

    async def complete(**kwargs):
        models.append(kwargs["model"])
        if kwargs["model"] == "primary" and models.count("primary") == 1:
            raise RuntimeError("primary unavailable")
        if "one decision for every input" in kwargs["messages"][-1]["content"]:
            return response('{"decisions":[{"incident_id":"incident","action":"investigate","reason":"new failure"}]}')
        return response(payload)

    item = observation()
    target = Incident.open_new(
        id="incident",
        correlation_rule_id="stable_finding",
        correlation_key=item.stable_identity,
        observation=item,
        opened_at=NOW,
    )
    adapter = LiteLLMReasoningAdapter(
        model="primary",
        fallback_models=("fallback",),
        api_key="key",
        temperature=0.1,
        max_tokens=100,
        max_retries=0,
        completion=complete,
        cost_calculator=lambda **_: 0.001,
    )

    investigation = await adapter.investigate(target, (item,))
    triage = await adapter.triage_batch((TriageCandidate(target, (item,)),))
    answer = await adapter.chat(message="why", incident=target, context={"as_of": NOW})

    assert investigation.status is InvestigationStatus.SUCCEEDED
    assert investigation.model == "fallback"
    assert investigation.usage["call_count"] == 1
    assert investigation.usage["total_tokens"] == 12
    assert investigation.usage["estimated_cost_usd"] == 0.001
    assert investigation.result["deterministic_severity"] == "warning"
    assert triage.routes[0].action == "investigate"
    assert triage.model_calls[0].purpose == "triage"
    assert answer.content == payload
    assert answer.model_calls[0].purpose == "chat"
    assert models[:2] == ["primary", "fallback"]


@pytest.mark.asyncio
async def test_reasoning_chat_rejects_empty_response() -> None:
    async def complete(**kwargs):
        del kwargs
        return response(" ")

    adapter = LiteLLMReasoningAdapter(
        model="model",
        fallback_models=(),
        api_key="key",
        temperature=0.1,
        max_tokens=100,
        max_retries=0,
        completion=complete,
    )

    with pytest.raises(ValueError, match="empty"):
        await adapter.chat(message="why")


@pytest.mark.asyncio
async def test_investigation_repairs_malformed_extraction_without_repeating_tools() -> None:
    payload = (
        '{"root_cause":"compiler","evidence":["line"],"impact":"blocked",'
        '"suggested_fix":"fix type","actionability":"actionable",'
        '"classification":"merge_request","priority":"warning","confidence":"medium"}'
    )
    calls = 0

    async def complete(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return response("Assessment follows in an unsupported format.")
        if calls == 2:
            return response("")
        return response(payload)

    item = observation()
    target = Incident.open_new(
        id="repair-incident",
        correlation_rule_id="stable_finding",
        correlation_key=item.stable_identity,
        observation=item,
        opened_at=NOW,
    )
    tools = Tools("jenkins_get_build_log")
    adapter = LiteLLMReasoningAdapter(
        model="model",
        fallback_models=(),
        api_key="key",
        temperature=0.1,
        max_tokens=100,
        max_retries=0,
        tools=tools,
        completion=complete,
    )

    result = await adapter.investigate(target, (item,))

    assert result.status is InvestigationStatus.SUCCEEDED
    assert result.result["root_cause"] == "compiler"
    assert result.usage["total_tokens"] == 36
    assert calls == 3
    assert tools.calls == 0


class Tools:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0
        self.definitions = (
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": "read evidence",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        )

    async def execute(self, name, arguments, *, mode):
        del mode
        self.calls += 1
        return ToolExecution(name=name, arguments=arguments, output='{"evidence":"direct"}', ok=True, duration_ms=8)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "expected_confidence"),
    [
        ("jenkins_get_build_log", "high"),
        ("jenkins_get_job_build_history", "low"),
    ],
)
async def test_tool_loop_persists_trace_and_enforces_pipeline_log_quality_gate(
    tool_name: str,
    expected_confidence: str,
) -> None:
    payload = (
        '{"root_cause":"compiler error","evidence":["direct"],"impact":"blocked",'
        '"suggested_fix":"fix type","actionability":"actionable",'
        '"classification":"merge_request","priority":"critical","confidence":"high"}'
    )
    calls = 0

    async def complete(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return response(
                "I will inspect direct evidence.",
                tool_calls=[
                    {
                        "id": "call-1",
                        "function": {"name": tool_name, "arguments": '{"job_name":"job","build_number":1}'},
                    }
                ],
            )
        return response(payload)

    item = replace(observation(), category="jenkins_build")
    incident = Incident.open_new(
        id="incident",
        correlation_rule_id="jenkins_failure",
        correlation_key="signature",
        observation=item,
        opened_at=NOW,
    )
    adapter = LiteLLMReasoningAdapter(
        model="model",
        fallback_models=(),
        api_key="key",
        temperature=0.1,
        max_tokens=100,
        max_retries=0,
        tools=Tools(tool_name),
        completion=complete,
    )

    result = await adapter.investigate(
        incident,
        (item,),
        context={"jenkins_builds": [{"id": "build", "started_at": NOW}]},
    )

    assert result.status is InvestigationStatus.SUCCEEDED
    assert result.confidence.value == expected_confidence
    assert result.result["tools_used"] == (tool_name,)
    assert result.result["tool_trace"][0]["output"] == '{"evidence":"direct"}'
    assert result.result["agent_summary"] == payload
    if expected_confidence == "low":
        assert "console log" in result.result["quality_gate"]


@pytest.mark.asyncio
async def test_pipeline_investigation_requires_representative_log_before_model_concludes() -> None:
    payload = (
        '{"root_cause":"version gate","evidence":["console"],"impact":"blocked",'
        '"suggested_fix":"upgrade","actionability":"actionable",'
        '"classification":"configuration","priority":"critical","confidence":"high"}'
    )

    async def complete(**kwargs):
        messages = kwargs["messages"]
        assert any("Mandatory pipeline evidence" in str(message.get("content")) for message in messages)
        return response(payload)

    item = replace(observation(), category="jenkins_build")
    incident = Incident.open_new(
        id="incident",
        correlation_rule_id="jenkins_failure",
        correlation_key="signature",
        observation=item,
        opened_at=NOW,
    )
    tools = Tools("jenkins_get_build_log")
    adapter = LiteLLMReasoningAdapter(
        model="model",
        fallback_models=(),
        api_key="key",
        temperature=0.1,
        max_tokens=100,
        max_retries=0,
        tools=tools,
        completion=complete,
    )

    result = await adapter.investigate(
        incident,
        (item,),
        context={
            "jenkins_builds": [
                {
                    "job_name": "DeployGenesisAndRunSyncTests",
                    "build_number": 14279,
                    "propagated_failure": False,
                }
            ]
        },
    )

    assert result.status is InvestigationStatus.SUCCEEDED
    assert result.confidence.value == "high"
    assert tools.calls == 1
    assert result.result["tools_used"] == ("jenkins_get_build_log",)
    assert result.result["tool_trace"][0]["round"] == 0
    assert result.result["tool_trace"][0]["arguments"] == {
        "job_name": "DeployGenesisAndRunSyncTests",
        "build_number": 14279,
        "tail_lines": 1500,
        "full": False,
    }
    assert "quality_gate" not in result.result


@pytest.mark.asyncio
async def test_tool_loop_refuses_a_model_call_when_the_prompt_cannot_fit_the_token_limit() -> None:
    payload = (
        '{"root_cause":"bounded evidence","evidence":[],"impact":"unknown",'
        '"suggested_fix":"inspect manually","actionability":"unknown",'
        '"classification":"unknown","priority":"warning","confidence":"medium"}'
    )
    completions = 0

    async def complete(**kwargs):
        nonlocal completions
        completions += 1
        if kwargs.get("tools"):
            return response(
                "",
                total_tokens=12,
                tool_calls=[
                    {
                        "id": "over-budget-call",
                        "function": {"name": "jenkins_get_build_log", "arguments": "{}"},
                    }
                ],
            )
        return response(payload, total_tokens=8)

    item = replace(observation(), category="jenkins_build")
    incident = Incident.open_new(
        id="budget-incident",
        correlation_rule_id="jenkins_failure",
        correlation_key="signature",
        observation=item,
        opened_at=NOW,
    )
    tools = Tools("jenkins_get_build_log")
    adapter = LiteLLMReasoningAdapter(
        model="model",
        fallback_models=(),
        api_key="key",
        temperature=0.1,
        max_tokens=100,
        max_retries=0,
        token_budget=1,
        deep_token_budget=1,
        tools=tools,
        completion=complete,
    )

    result = await adapter.investigate(incident, (item,))

    assert result.status is InvestigationStatus.PARTIAL
    assert tools.calls == 0
    assert completions == 0
    assert "token limit reached" in (result.error_summary or "")
    assert result.usage == {}


@pytest.mark.asyncio
async def test_tool_loop_reserves_a_final_answer_within_the_investigation_token_limit() -> None:
    payload = (
        '{"root_cause":"bounded evidence","evidence":[],"impact":"unknown",'
        '"suggested_fix":"inspect manually","actionability":"unknown",'
        '"classification":"unknown","priority":"warning","confidence":"medium"}'
    )
    max_token_requests = []

    async def complete(**kwargs):
        max_token_requests.append(kwargs["max_tokens"])
        if kwargs.get("tools"):
            return response(
                "",
                total_tokens=100,
                tool_calls=[
                    {
                        "id": "bounded-call",
                        "function": {"name": "jenkins_get_build_log", "arguments": "{}"},
                    }
                ],
            )
        return response(payload, total_tokens=80)

    item = replace(observation(), category="jenkins_build")
    incident = Incident.open_new(
        id="bounded-budget-incident",
        correlation_rule_id="jenkins_failure",
        correlation_key="bounded-signature",
        observation=item,
        opened_at=NOW,
    )
    tools = Tools("jenkins_get_build_log")
    adapter = LiteLLMReasoningAdapter(
        model="model",
        fallback_models=(),
        api_key="key",
        temperature=0.1,
        max_tokens=200,
        max_retries=0,
        token_budget=300,
        deep_token_budget=300,
        tools=tools,
        completion=complete,
        token_counter=lambda **_: 10,
    )

    result = await adapter.investigate(incident, (item,))

    assert result.status is InvestigationStatus.PARTIAL
    assert tools.calls == 1
    assert len(max_token_requests) == 2
    assert all(value < 200 for value in max_token_requests)
    assert result.usage["total_tokens"] == 180
    assert result.usage["total_tokens"] <= 300
    assert result.result["root_cause"] == "bounded evidence"


def test_tool_history_compaction_and_failed_test_report_cap_confidence() -> None:
    messages = [
        {"role": "tool", "content": "head" + "x" * 10_000 + "tail"},
        {"role": "user", "content": "keep"},
    ]
    _compact_tool_messages(messages, mode=ScanMode.REGULAR)
    assert len(messages[0]["content"]) < 2_100
    assert messages[0]["content"].startswith("head")
    assert messages[0]["content"].endswith("tail")
    assert messages[1]["content"] == "keep"

    result = {
        "classification": "merge_request",
        "confidence": "high",
        "root_cause": "12 unit tests in the Gradle common:test task failed.",
    }
    _apply_quality_gates(
        result,
        (replace(observation(), category="jenkins_build"),),
        [
            {"tool": "jenkins_get_build_log", "ok": True},
            {"tool": "jenkins_get_test_report", "ok": False},
        ],
        context={"jenkins_builds": [{"id": "build"}]},
    )
    assert result["confidence"] == "medium"
    assert "failed-test report" in result["quality_gate"]


def test_compaction_shrinks_mandatory_evidence_but_spares_the_current_round() -> None:
    messages = [
        {"role": "user", "content": _MANDATORY_EVIDENCE_PREFIX + " log " + "x" * 20_000},
        {"role": "tool", "content": "old" + "y" * 20_000},
        {"role": "tool", "content": "fresh" + "z" * 20_000},
    ]

    _compact_tool_messages(messages, mode=ScanMode.REGULAR, keep_from=2)

    # The pre-flight build log arrives as a user message but is still tool output, and it was
    # previously re-sent at full size on every round.
    assert len(messages[0]["content"]) < 2_100
    assert len(messages[1]["content"]) < 2_100
    assert len(messages[2]["content"]) == len("fresh") + 20_000


def test_a_single_fan_out_round_cannot_outgrow_the_context_budget() -> None:
    """Regression: one round requesting many build logs exhausted any token budget."""
    messages = [{"role": "user", "content": "prompt"}]
    messages.extend({"role": "tool", "content": "x" * 12_000} for _ in range(13))

    _bound_round_results(messages, start=1, mode=ScanMode.REGULAR)

    total = sum(len(message["content"]) for message in messages[1:])
    assert total <= 48_000


def test_history_stays_bounded_as_tool_messages_accumulate() -> None:
    """Regression: a fixed per-message cap still blew the budget once rounds piled up."""
    messages = [{"role": "user", "content": "prompt"}]
    messages.extend({"role": "tool", "content": "x" * 12_000} for _ in range(200))
    messages.append({"role": "tool", "content": "fresh" + "z" * 12_000})

    _compact_tool_messages(messages, mode=ScanMode.REGULAR, keep_from=201)

    history = sum(len(m["content"]) for m in messages[1:201])
    assert history <= 120_000
    assert len(messages[201]["content"]) == len("fresh") + 12_000


def test_a_small_round_keeps_full_detail() -> None:
    messages = [{"role": "user", "content": "prompt"}, {"role": "tool", "content": "x" * 12_000}]

    _bound_round_results(messages, start=1, mode=ScanMode.REGULAR)

    assert len(messages[1]["content"]) == 12_000


def test_pipeline_quality_gate_distinguishes_failed_log_access_from_no_attempt() -> None:
    result = {"classification": "infrastructure", "confidence": "high", "root_cause": "version mismatch"}

    _apply_quality_gates(
        result,
        (replace(observation(), category="jenkins_build"),),
        [{"tool": "jenkins_get_build_log", "ok": False, "attempts": 3}],
        context={"jenkins_builds": [{"id": "build"}]},
    )

    assert result["confidence"] == "low"
    assert "attempted but failed after 3 attempts" in result["quality_gate"]
    assert "retained scan evidence" in result["quality_gate"]


def test_pipeline_quality_gate_does_not_treat_pre_test_abort_as_test_failure() -> None:
    result = {
        "classification": "configuration",
        "confidence": "high",
        "root_cause": "The version gate failed all builds before any deployment or test execution occurred.",
    }

    _apply_quality_gates(
        result,
        (replace(observation(), category="jenkins_build"),),
        [{"tool": "jenkins_get_build_log", "ok": True, "attempts": 1}],
        context={"jenkins_builds": [{"id": "build"}]},
    )

    assert result["confidence"] == "high"
    assert "quality_gate" not in result


@pytest.mark.asyncio
async def test_tool_backed_chat_returns_only_final_answer_not_interim_planning() -> None:
    calls = 0

    async def complete(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return response(
                "I will inspect the build.",
                tool_calls=[
                    {
                        "id": "chat-tool",
                        "function": {"name": "jenkins_get_build_log", "arguments": "{}"},
                    }
                ],
            )
        return response("The verified final answer.")

    adapter = LiteLLMReasoningAdapter(
        model="model",
        fallback_models=(),
        api_key="key",
        temperature=0.1,
        max_tokens=100,
        max_retries=0,
        tools=Tools("jenkins_get_build_log"),
        completion=complete,
    )

    answer = await adapter.chat(message="why did it fail?")

    assert answer.content == "The verified final answer."
