"""Tests for Kubernetes node checks."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

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
async def test_node_check_reports_metrics_unavailable():
    node = _make_node("k3s-agent-01")
    v1 = SimpleNamespace(list_node=lambda: SimpleNamespace(items=[node]))

    async def run_sync(func, *args, **kwargs):
        del kwargs
        return func(*args)

    kubernetes = SimpleNamespace(core_v1=lambda: v1, run_sync=run_sync)
    metrics = SimpleNamespace(list_node_metrics=AsyncMock(side_effect=MetricsUnavailableError("404")))
    with pytest.raises(MetricsUnavailableError):
        await NodeCheck(kubernetes, metrics).run()


@pytest.mark.asyncio
async def test_node_check_high_memory_usage():
    node = _make_node("k3s-agent-01")
    alloc_bytes = 100 * 1024**3
    used_bytes = int(alloc_bytes * 0.96)
    metrics = [NodeMetrics(name="k3s-agent-01", cpu_cores=1.0, memory_bytes=used_bytes)]

    v1 = SimpleNamespace(list_node=lambda: SimpleNamespace(items=[node]))

    async def run_sync(func, *args, **kwargs):
        del kwargs
        return func(*args)

    kubernetes = SimpleNamespace(core_v1=lambda: v1, run_sync=run_sync)
    metrics_client = SimpleNamespace(
        list_node_metrics=AsyncMock(return_value=metrics),
        get_node_allocatable=AsyncMock(return_value={"k3s-agent-01": {"cpu_cores": 10.0, "memory_bytes": alloc_bytes}}),
    )
    findings = await NodeCheck(kubernetes, metrics_client).run()

    mem_findings = [f for f in findings if "Memory at" in f.symptom]
    assert len(mem_findings) == 1
    assert mem_findings[0].severity == "critical"
