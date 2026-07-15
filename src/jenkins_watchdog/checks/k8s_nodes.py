"""Kubernetes worker node health checks for k3s cluster."""

import logging

from jenkins_watchdog.checks.base import CheckReport, Finding
from jenkins_watchdog.clients.k8s import KubernetesClient
from jenkins_watchdog.clients.k8s_metrics import (
    KubernetesMetricsClient,
    format_bytes,
    format_cores,
    usage_pct,
)

logger = logging.getLogger(__name__)

_MEMORY_WARN_PCT = 85
_MEMORY_CRITICAL_PCT = 95
_CPU_WARN_PCT = 85
_CPU_CRITICAL_PCT = 95


def _severity_for_pct(pct: float, warn: float, critical: float) -> str:
    if pct > critical:
        return "critical"
    if pct > warn:
        return "warning"
    return "low"


class NodeCheck:
    name = "k8s_nodes"

    def __init__(self, kubernetes: KubernetesClient, metrics: KubernetesMetricsClient) -> None:
        self._kubernetes = kubernetes
        self._metrics = metrics

    async def run(self) -> CheckReport:
        findings: list[Finding] = []
        v1 = self._kubernetes.core_v1()
        nodes = await self._kubernetes.run_sync(v1.list_node, timeout=15)
        ready_count = 0

        for node in nodes.items:
            name = node.metadata.name
            conditions = {c.type: c for c in (node.status.conditions or [])}

            ready_cond = conditions.get("Ready")
            if ready_cond and ready_cond.status == "True":
                ready_count += 1
            if ready_cond and ready_cond.status != "True":
                findings.append(
                    Finding(
                        severity="critical",
                        category="k8s_node",
                        resource=f"node/{name}",
                        symptom=f"Node NotReady: {ready_cond.message or ready_cond.reason or 'unknown'}",
                        context={
                            "reason": ready_cond.reason or "",
                            "message": ready_cond.message or "",
                        },
                    )
                )

            for cond_type in ("MemoryPressure", "DiskPressure", "PIDPressure"):
                cond = conditions.get(cond_type)
                if cond and cond.status == "True":
                    severity = "critical" if cond_type == "MemoryPressure" else "warning"
                    findings.append(
                        Finding(
                            severity=severity,
                            category="k8s_node",
                            resource=f"node/{name}",
                            symptom=f"{cond_type}: {cond.message or cond.reason or ''}",
                            context={"reason": cond.reason or ""},
                        )
                    )

        usage_findings, metrics_count = await self._check_node_usage()
        findings.extend(usage_findings)
        return CheckReport(
            findings=findings,
            summary={
                "node_count": len(nodes.items),
                "ready_node_count": ready_count,
                "not_ready_node_count": len(nodes.items) - ready_count,
                "metrics_node_count": metrics_count,
            },
        )

    async def _check_node_usage(self) -> tuple[list[Finding], int]:
        findings: list[Finding] = []
        node_metrics = await self._metrics.list_node_metrics()
        allocatable = await self._metrics.get_node_allocatable()

        for metrics in node_metrics:
            limits = allocatable.get(metrics.name)
            if not limits:
                continue

            cpu_pct = usage_pct(metrics.cpu_cores, limits["cpu_cores"])
            if cpu_pct is not None and cpu_pct > _CPU_WARN_PCT:
                findings.append(
                    Finding(
                        severity=_severity_for_pct(cpu_pct, _CPU_WARN_PCT, _CPU_CRITICAL_PCT),
                        category="k8s_node",
                        resource=f"node/{metrics.name}",
                        symptom=(
                            f"CPU at {cpu_pct:.0f}% of allocatable "
                            f"({format_cores(metrics.cpu_cores)}/{format_cores(limits['cpu_cores'])})"
                        ),
                        context={
                            "cpu_usage_pct": round(cpu_pct, 1),
                            "cpu_used_cores": metrics.cpu_cores,
                            "cpu_allocatable_cores": limits["cpu_cores"],
                        },
                    )
                )

            mem_pct = usage_pct(metrics.memory_bytes, limits["memory_bytes"])
            if mem_pct is not None and mem_pct > _MEMORY_WARN_PCT:
                findings.append(
                    Finding(
                        severity=_severity_for_pct(mem_pct, _MEMORY_WARN_PCT, _MEMORY_CRITICAL_PCT),
                        category="k8s_node",
                        resource=f"node/{metrics.name}",
                        symptom=(
                            f"Memory at {mem_pct:.0f}% of allocatable "
                            f"({format_bytes(metrics.memory_bytes)}/{format_bytes(limits['memory_bytes'])})"
                        ),
                        context={
                            "memory_usage_pct": round(mem_pct, 1),
                            "memory_used_bytes": metrics.memory_bytes,
                            "memory_allocatable_bytes": limits["memory_bytes"],
                        },
                    )
                )

        return findings, len(node_metrics)
