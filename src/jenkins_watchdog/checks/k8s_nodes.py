"""Kubernetes worker node health checks for k3s cluster."""

import logging

from jenkins_watchdog.checks.base import Finding
from jenkins_watchdog.clients.k8s import get_core_v1, run_sync
from jenkins_watchdog.clients.k8s_metrics import (
    MetricsUnavailableError,
    format_bytes,
    format_cores,
    get_node_allocatable,
    list_node_metrics,
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

    async def run(self) -> list[Finding]:
        findings: list[Finding] = []
        v1 = get_core_v1()
        nodes = await run_sync(v1.list_node, timeout_seconds=15)

        for node in nodes.items:
            name = node.metadata.name
            conditions = {c.type: c for c in (node.status.conditions or [])}

            ready_cond = conditions.get("Ready")
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

        findings.extend(await self._check_node_usage())
        return findings

    async def _check_node_usage(self) -> list[Finding]:
        findings: list[Finding] = []

        try:
            node_metrics = await list_node_metrics()
            allocatable = await get_node_allocatable()
        except MetricsUnavailableError:
            logger.warning("Metrics-server unavailable — skipping node resource usage checks")
            return []
        except Exception as exc:
            logger.warning("Failed to fetch node metrics: %s", exc)
            return []

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

        return findings
