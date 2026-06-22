"""Tests for Kubernetes node checks."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jenkins_watchdog.checks.k8s_nodes import NodeCheck
from jenkins_watchdog.clients.k8s_metrics import MetricsUnavailableError, NodeMetrics


def _make_node(name: str, ready: bool = True):
    node = MagicMock()
    node.metadata.name = name
    cond = MagicMock()
    cond.type = "Ready"
    cond.status = "True" if ready else "False"
    cond.message = ""
    cond.reason = ""
    node.status.conditions = [cond]
    return node


@pytest.mark.asyncio
async def test_node_check_usage_skips_when_metrics_unavailable():
    check = NodeCheck()
    node = _make_node("k3s-agent-01")

    with (
        patch("jenkins_watchdog.checks.k8s_nodes.get_core_v1") as mock_v1,
        patch("jenkins_watchdog.checks.k8s_nodes.list_node_metrics", AsyncMock(side_effect=MetricsUnavailableError("404"))),
    ):
        mock_v1.return_value.list_node = MagicMock(return_value=MagicMock(items=[node]))
        with patch("jenkins_watchdog.checks.k8s_nodes.run_sync", AsyncMock(side_effect=lambda fn, *a, **kw: fn(*a, **kw))):
            findings = await check.run()

    assert not any("CPU at" in f.symptom for f in findings)


@pytest.mark.asyncio
async def test_node_check_high_memory_usage():
    check = NodeCheck()
    node = _make_node("k3s-agent-01")
    alloc_bytes = 100 * 1024**3
    used_bytes = int(alloc_bytes * 0.96)
    metrics = [NodeMetrics(name="k3s-agent-01", cpu_cores=1.0, memory_bytes=used_bytes)]

    with (
        patch("jenkins_watchdog.checks.k8s_nodes.get_core_v1") as mock_v1,
        patch("jenkins_watchdog.checks.k8s_nodes.list_node_metrics", AsyncMock(return_value=metrics)),
        patch(
            "jenkins_watchdog.checks.k8s_nodes.get_node_allocatable",
            AsyncMock(return_value={"k3s-agent-01": {"cpu_cores": 10.0, "memory_bytes": alloc_bytes}}),
        ),
    ):
        mock_v1.return_value.list_node = MagicMock(return_value=MagicMock(items=[node]))
        with patch("jenkins_watchdog.checks.k8s_nodes.run_sync", AsyncMock(side_effect=lambda fn, *a, **kw: fn(*a, **kw))):
            findings = await check.run()

    mem_findings = [f for f in findings if "Memory at" in f.symptom]
    assert len(mem_findings) == 1
    assert mem_findings[0].severity == "critical"
