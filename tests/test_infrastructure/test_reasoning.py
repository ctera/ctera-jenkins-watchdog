from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from jenkins_watchdog.domain.model import FindingObservation, Incident, InvestigationStatus, Severity
from jenkins_watchdog.infrastructure.reasoning import LiteLLMReasoningAdapter, _extract_assessment

NOW = datetime(2026, 7, 13, tzinfo=timezone.utc)


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


def test_structured_extraction_accepts_json_fence_and_rejects_malformed() -> None:
    payload = (
        '{"root_cause":"x","evidence":[],"impact":"y","suggested_fix":"z",'
        '"actionability":"actionable","classification":"infrastructure",'
        '"priority":"warning","confidence":"medium"}'
    )
    assert _extract_assessment(f"```json\n{payload}\n```")["confidence"] == "medium"
    with pytest.raises(ValueError):
        _extract_assessment('{"root_cause":"missing fields"}')
    with pytest.raises(ValueError):
        _extract_assessment(payload.replace('"medium"', '"certain"'))


@pytest.mark.asyncio
async def test_disabled_reasoning_persists_a_low_confidence_failure_value() -> None:
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

    assert result.status == InvestigationStatus.FAILED
    assert result.confidence.value == "low"
    assert "disabled" in (result.error_summary or "")


def response(content: str, *, total_tokens: int = 12):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
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
    )

    investigation = await adapter.investigate(target, (item,))
    triage = await adapter.triage(target, (item,))
    answer = await adapter.chat(message="why", incident=target)

    assert investigation.status is InvestigationStatus.SUCCEEDED
    assert investigation.model == "fallback"
    assert investigation.usage == {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12}
    assert investigation.result["deterministic_severity"] == "warning"
    assert triage == {
        "actionability": "actionable",
        "classification": "merge_request",
        "priority": "warning",
        "confidence": "medium",
    }
    assert answer == payload
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
