"""Tests for Jenkins agent resource checks."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jenkins_watchdog.checks.agent_resources import AgentResourceCheck
from jenkins_watchdog.clients.k8s_metrics import ContainerUsage, MetricsUnavailableError, PodMetrics


def _make_pod(name: str, containers: list[tuple[str, str | None, str | None]]):
    pod = MagicMock()
    pod.metadata.namespace = "jenkins"
    pod.metadata.name = name
    pod.metadata.labels = {"jenkins-slave": "true", "jenkins/label": name}
    pod.spec.containers = []
    for cname, mem_limit, cpu_limit in containers:
        container = MagicMock()
        container.name = cname
        container.resources.limits = {}
        if mem_limit:
            container.resources.limits["memory"] = mem_limit
        if cpu_limit:
            container.resources.limits["cpu"] = cpu_limit
        if not container.resources.limits:
            container.resources.limits = None
        pod.spec.containers.append(container)
    return pod


@pytest.mark.asyncio
async def test_agent_resource_check_skips_when_metrics_unavailable():
    check = AgentResourceCheck()
    with patch(
        "jenkins_watchdog.checks.agent_resources.list_pod_metrics",
        AsyncMock(side_effect=MetricsUnavailableError("404")),
    ):
        findings = await check.run()
    assert findings == []


@pytest.mark.asyncio
async def test_agent_resource_check_high_memory_usage():
    check = AgentResourceCheck()
    pod = _make_pod("agent-test-abc123-xk2mq", [("jnlp", "1Gi", "500m")])
    metrics = PodMetrics(
        namespace="jenkins",
        name="agent-test-abc123-xk2mq",
        containers=[ContainerUsage(name="jnlp", cpu_cores=0.1, memory_bytes=int(0.92 * 1024**3))],
    )

    with (
        patch("jenkins_watchdog.checks.agent_resources.list_pod_metrics", AsyncMock(return_value=[metrics])),
        patch("jenkins_watchdog.checks.agent_resources.list_jenkins_agent_pods", AsyncMock(return_value=[pod])),
    ):
        findings = await check.run()

    assert len(findings) == 1
    assert findings[0].severity == "critical"
    assert "Memory at 92%" in findings[0].symptom


@pytest.mark.asyncio
async def test_agent_resource_check_no_limits():
    check = AgentResourceCheck()
    pod = _make_pod("agent-test-abc123-xk2mq", [("jnlp", None, None)])
    metrics = PodMetrics(
        namespace="jenkins",
        name="agent-test-abc123-xk2mq",
        containers=[ContainerUsage(name="jnlp", cpu_cores=0.1, memory_bytes=100 * 1024**2)],
    )

    with (
        patch("jenkins_watchdog.checks.agent_resources.list_pod_metrics", AsyncMock(return_value=[metrics])),
        patch("jenkins_watchdog.checks.agent_resources.list_jenkins_agent_pods", AsyncMock(return_value=[pod])),
    ):
        findings = await check.run()

    assert len(findings) == 1
    assert "No resource limits set" in findings[0].symptom
