from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from jenkins_watchdog.checks.base import Finding
from jenkins_watchdog.clients.log_analysis import classify_failure, error_signature, extract_error_lines
from jenkins_watchdog.domain.model import FindingObservation, Severity
from jenkins_watchdog.domain.policies import correlate_observation
from jenkins_watchdog.infrastructure.checks import _to_observation
from jenkins_watchdog.infrastructure.reasoning import _extract_assessment

FIXTURES = Path(__file__).parent / "fixtures" / "characterization"
NOW = datetime(2026, 7, 13, tzinfo=timezone.utc)


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_detector_characterization_fixture_preserves_structured_dimensions() -> None:
    for case in load("detectors.json"):
        observation = _to_observation(
            scan_id="scan",
            check_name="fixture_check",
            finding=Finding(**case["finding"]),
            observed_at=NOW,
        )
        for name, value in case["expected_dimensions"].items():
            assert observation.identity_dimensions[name] == value


def test_correlation_characterization_fixture_preserves_rule_order() -> None:
    for index, case in enumerate(load("correlation.json")):
        observation = FindingObservation(
            scan_id="scan",
            check_name="fixture_check",
            rule_id="fixture.rule.v1",
            resource_id=f"resource-{index}",
            severity=Severity.WARNING,
            category="jenkins_failed_build",
            summary="fixture",
            observed_at=NOW,
            identity_dimensions=case["dimensions"],
        )
        decision = correlate_observation(observation)
        assert (decision.rule_id, decision.key) == (case["rule_id"], case["key"])


def test_triage_characterization_fixture_preserves_strict_extraction() -> None:
    for case in load("triage.json"):
        if case["valid"]:
            assert _extract_assessment(case["content"])["confidence"] == case["confidence"]
        else:
            with pytest.raises((ValueError, json.JSONDecodeError)):
                _extract_assessment(case["content"])


def test_log_parser_characterization_fixture_preserves_classification_and_signature() -> None:
    for case in load("log_parser.json"):
        lines = extract_error_lines(case["console"])
        assert any(case["contains"] in line for line in lines)
        assert classify_failure(lines) == case["classification"]
        assert len(error_signature(lines)) == 12
