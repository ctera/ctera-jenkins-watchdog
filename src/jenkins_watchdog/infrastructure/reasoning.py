"""LiteLLM implementation of the consolidated v2 reasoning port."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from uuid import uuid4

from jenkins_watchdog.application.reasoning import evidence_digest
from jenkins_watchdog.domain.model import (
    Confidence,
    FindingObservation,
    Incident,
    Investigation,
    InvestigationStatus,
)
from jenkins_watchdog.domain.serialization import to_primitive

INPUT_VERSION = "v1"
PROMPT_VERSION = "v1"
Completion = Callable[..., Awaitable[Any]]


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
        completion: Completion | None = None,
    ) -> None:
        self._models = (model, *fallback_models)
        self._api_key = api_key
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._max_retries = max_retries
        self._completion = completion

    async def triage(self, incident: Incident, observations: tuple[FindingObservation, ...]) -> dict[str, Any]:
        assessment, _, _ = await self._complete(incident, observations, concise=True)
        return {key: assessment[key] for key in ("actionability", "classification", "priority", "confidence")}

    async def investigate(self, incident: Incident, observations: tuple[FindingObservation, ...]) -> Investigation:
        created_at = datetime.now(timezone.utc)
        evidence_hash = evidence_digest(observations)
        try:
            result, model, usage = await self._complete(incident, observations, concise=False)
            confidence = Confidence(str(result["confidence"]).lower())
            result["deterministic_severity"] = incident.severity.value
            return Investigation(
                id=str(uuid4()),
                incident_id=incident.id,
                occurrence_id=incident.current_occurrence.id,
                status=InvestigationStatus.SUCCEEDED,
                evidence_hash=evidence_hash,
                input_version=INPUT_VERSION,
                prompt_version=PROMPT_VERSION,
                model=model,
                confidence=confidence,
                usage=usage,
                result=result,
                created_at=created_at,
                completed_at=datetime.now(timezone.utc),
            )
        except Exception as exc:
            return Investigation(
                id=str(uuid4()),
                incident_id=incident.id,
                occurrence_id=incident.current_occurrence.id,
                status=InvestigationStatus.FAILED,
                evidence_hash=evidence_hash,
                input_version=INPUT_VERSION,
                prompt_version=PROMPT_VERSION,
                model=self._models[0],
                confidence=Confidence.LOW,
                usage={},
                result={"deterministic_severity": incident.severity.value},
                error_summary=f"{type(exc).__name__}: {exc}"[:500],
                created_at=created_at,
                completed_at=datetime.now(timezone.utc),
            )

    async def chat(self, *, message: str, incident: Incident | None = None) -> str:
        if not self._api_key:
            raise RuntimeError("reasoning integration is disabled")
        context = ""
        if incident:
            context = (
                f"\nIncident {incident.id}: {incident.title}; severity={incident.severity.value}; "
                f"status={incident.status.value}; classification={incident.classification or 'unknown'}."
            )
        response = await self._acompletion(
            model=self._models[0],
            messages=[
                {
                    "role": "system",
                    "content": "Answer operational questions precisely. Never claim an action was executed.",
                },
                {"role": "user", "content": f"{message}{context}"},
            ],
            api_key=self._api_key,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            num_retries=self._max_retries,
        )
        content = response.choices[0].message.content
        if not isinstance(content, str) or not content.strip():
            raise ValueError("reasoning response was empty")
        return content

    async def _complete(
        self,
        incident: Incident,
        observations: tuple[FindingObservation, ...],
        *,
        concise: bool,
    ) -> tuple[dict[str, Any], str, dict[str, int]]:
        if not self._api_key:
            raise RuntimeError("reasoning integration is disabled")
        prompt = _prompt(incident, observations, concise=concise)
        last_error: Exception | None = None
        for model in self._models:
            try:
                response = await self._acompletion(
                    model=model,
                    messages=[
                        {
                            "role": "system",
                            "content": "Return exactly one JSON object. Triage is advisory and cannot change finding severity or lifecycle.",
                        },
                        {"role": "user", "content": prompt},
                    ],
                    api_key=self._api_key,
                    temperature=self._temperature,
                    max_tokens=self._max_tokens,
                    num_retries=self._max_retries,
                    response_format={"type": "json_object"},
                )
                content = response.choices[0].message.content
                result = _extract_assessment(content)
                return result, model, _usage(response)
            except Exception as exc:
                last_error = exc
        raise RuntimeError("all reasoning models failed") from last_error

    async def _acompletion(self, **kwargs: Any) -> Any:
        if self._completion is None:
            from litellm import acompletion

            self._completion = acompletion
        return await self._completion(**kwargs)


def _prompt(incident: Incident, observations: tuple[FindingObservation, ...], *, concise: bool) -> str:
    payload = {
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
            for item in observations
        ],
        "concise": concise,
    }
    return (
        "Assess this Jenkins/Kubernetes incident. Return keys root_cause, evidence (array), impact, "
        "suggested_fix, actionability (actionable|informational|unknown), classification "
        "(merge_request|infrastructure|unknown), priority (low|warning|critical), and confidence "
        f"(low|medium|high). Input: {json.dumps(payload, separators=(',', ':'))}"
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
    if str(value["confidence"]).lower() not in {item.value for item in Confidence}:
        raise ValueError("reasoning confidence is invalid")
    return value


def _usage(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    result: dict[str, int] = {}
    for target, source in (
        ("prompt_tokens", "prompt_tokens"),
        ("completion_tokens", "completion_tokens"),
        ("total_tokens", "total_tokens"),
    ):
        value = getattr(usage, source, None)
        if isinstance(value, int):
            result[target] = value
    return result
