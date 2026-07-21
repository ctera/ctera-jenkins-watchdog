from __future__ import annotations

import asyncio

import pytest

from jenkins_watchdog.checks.base import Finding
from jenkins_watchdog.domain.model import CheckStatus, ScanMode
from jenkins_watchdog.infrastructure.checks import LegacyCheckRunner
from jenkins_watchdog.scan_options import ScanOptions, get_scan_options


class SuccessfulCheck:
    name = "jenkins_agent_connectivity"

    async def run(self):
        return [
            Finding(
                severity="warning",
                category="jenkins_agent",
                resource="jenkins-agent/linux-pool-abc123-defg",
                symptom="Agent offline for 12 minutes",
                context={
                    "scm": {
                        "provider": "github",
                        "repository": "ctera/app",
                        "change_number": 42,
                    },
                    "built_on": "worker-1",
                    "error_signature": "signature",
                },
            )
        ]


class FailingCheck:
    name = "jenkins_agent_connectivity"

    async def run(self):
        raise RuntimeError("credentials leaked? secret-value")


class SlowCheck:
    name = "jenkins_agent_connectivity"

    async def run(self):
        await asyncio.sleep(1)
        return []


class OptionCheck:
    name = "jenkins_failed_builds"

    def __init__(self) -> None:
        self.options = []

    async def run(self):
        self.options.append(get_scan_options())
        return []


def test_check_adapter_selects_owning_checks_and_rejects_unmapped_names() -> None:
    runner = LegacyCheckRunner((SuccessfulCheck(),), timeout_seconds=1)

    assert runner.checks_for_categories(()) == runner.check_names
    assert runner.checks_for_categories(("jenkins_controller",)) == ("jenkins_agent_connectivity",)
    assert runner.checks_for_categories(("k8s_node",)) == ()

    with pytest.raises(ValueError, match="category ownership"):
        LegacyCheckRunner((type("Unknown", (), {"name": "unknown"})(),), timeout_seconds=1)


@pytest.mark.asyncio
async def test_check_adapter_builds_versioned_observation_dimensions() -> None:
    result = await LegacyCheckRunner((SuccessfulCheck(),), timeout_seconds=1).run(
        "scan-id", "jenkins_agent_connectivity", ScanMode.REGULAR
    )

    assert result.status is CheckStatus.SUCCEEDED
    [observation] = result.findings
    assert observation.rule_id.endswith(".v1")
    assert observation.identity_dimensions["scm_provider"] == "github"
    assert observation.identity_dimensions["scm_repository"] == "ctera/app"
    assert observation.identity_dimensions["change_number"] == 42
    assert observation.identity_dimensions["node"] == "worker-1"
    assert observation.identity_dimensions["agent_pool"] == "linux-pool"
    assert observation.identity_dimensions["symptom_family"] == "agent_offline"


@pytest.mark.asyncio
async def test_check_adapter_returns_structured_failure_and_timeout() -> None:
    failed = await LegacyCheckRunner((FailingCheck(),), timeout_seconds=1).run(
        "scan-id", "jenkins_agent_connectivity", ScanMode.REGULAR
    )
    timed_out = await LegacyCheckRunner((SlowCheck(),), timeout_seconds=0.01).run(
        "scan-id", "jenkins_agent_connectivity", ScanMode.REGULAR
    )

    assert failed.status is CheckStatus.FAILED
    assert failed.failure_summary.startswith("RuntimeError")
    assert timed_out.status is CheckStatus.TIMED_OUT
    assert "timed out" in (timed_out.failure_summary or "")


@pytest.mark.asyncio
async def test_check_adapter_applies_a_check_specific_timeout() -> None:
    result = await LegacyCheckRunner(
        (SlowCheck(),),
        timeout_seconds=1,
        timeout_overrides={"jenkins_agent_connectivity": 0.01},
    ).run("scan-id", "jenkins_agent_connectivity", ScanMode.REGULAR)

    assert result.status is CheckStatus.TIMED_OUT
    assert result.failure_summary == "timed out after 0.01s"


@pytest.mark.asyncio
async def test_check_adapter_activates_mode_specific_options_without_leaking_context() -> None:
    check = OptionCheck()
    regular = ScanOptions(jenkins_failed_build_window_hours=2, jenkins_build_depth=4)
    runner = LegacyCheckRunner((check,), timeout_seconds=1, regular_options=regular)

    await runner.run("regular", check.name, ScanMode.REGULAR)
    await runner.run("deep", check.name, ScanMode.DEEP)

    assert check.options[0] == regular
    assert check.options[1].deep
    assert check.options[1].jenkins_build_depth == 50
    assert get_scan_options() == ScanOptions()
