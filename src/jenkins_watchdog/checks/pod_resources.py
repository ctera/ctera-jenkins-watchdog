"""Cluster-wide pod resource checks — CPU/memory pressure on all pods."""

import logging

from jenkins_watchdog.checks.base import Finding
from jenkins_watchdog.clients.k8s import get_core_v1, run_sync
from jenkins_watchdog.clients.k8s_metrics import (
    MetricsUnavailableError,
    format_bytes,
    format_cores,
    list_all_pod_metrics,
    parse_cpu_quantity,
    parse_memory_quantity,
    usage_pct,
)

logger = logging.getLogger(__name__)

_MEMORY_WARN_PCT = 85
_MEMORY_CRITICAL_PCT = 95
_CPU_WARN_PCT = 85
_CPU_CRITICAL_PCT = 95

_SKIP_NAMESPACES = frozenset({"kube-system"})


def _severity_for_pct(pct: float, warn: float, critical: float) -> str:
    if pct > critical:
        return "critical"
    if pct > warn:
        return "warning"
    return "low"


class PodResourceCheck:
    name = "pod_resources"

    async def run(self) -> list[Finding]:
        findings: list[Finding] = []

        try:
            all_metrics = await list_all_pod_metrics()
        except MetricsUnavailableError:
            logger.warning("Metrics-server unavailable — skipping pod resource checks")
            return []
        except Exception as exc:
            logger.warning("Failed to fetch cluster pod metrics: %s", exc)
            return []

        metrics_index: dict[tuple[str, str], list] = {}
        for m in all_metrics:
            if m.namespace in _SKIP_NAMESPACES:
                continue
            metrics_index[(m.namespace, m.name)] = m.containers

        v1 = get_core_v1()
        pods = await run_sync(v1.list_pod_for_all_namespaces, timeout_seconds=20)

        for pod in pods.items:
            ns = pod.metadata.namespace
            name = pod.metadata.name
            if ns in _SKIP_NAMESPACES:
                continue
            if not pod.status or pod.status.phase != "Running":
                continue

            container_metrics = metrics_index.get((ns, name))
            if not container_metrics:
                continue
            usage_by_name = {c.name: c for c in container_metrics}

            for container in pod.spec.containers:
                limits = container.resources.limits if container.resources else None
                if not limits:
                    continue

                mem_limit_raw = limits.get("memory")
                cpu_limit_raw = limits.get("cpu")
                usage = usage_by_name.get(container.name)
                if not usage:
                    continue

                resource = f"{ns}/{name}"

                if mem_limit_raw:
                    mem_limit = parse_memory_quantity(mem_limit_raw)
                    mem_pct = usage_pct(usage.memory_bytes, mem_limit)
                    if mem_pct is not None and mem_pct > _MEMORY_WARN_PCT:
                        findings.append(
                            Finding(
                                severity=_severity_for_pct(mem_pct, _MEMORY_WARN_PCT, _MEMORY_CRITICAL_PCT),
                                category="pod_resource",
                                resource=resource,
                                symptom=(
                                    f"Memory at {mem_pct:.0f}% of limit "
                                    f"({format_bytes(usage.memory_bytes)}/{mem_limit_raw}, "
                                    f"container: {container.name})"
                                ),
                                context={
                                    "container": container.name,
                                    "memory_usage_pct": round(mem_pct, 1),
                                    "memory_used": usage.memory_bytes,
                                    "memory_limit": mem_limit_raw,
                                },
                            )
                        )

                if cpu_limit_raw:
                    cpu_limit = parse_cpu_quantity(cpu_limit_raw)
                    cpu_pct = usage_pct(usage.cpu_cores, cpu_limit)
                    if cpu_pct is not None and cpu_pct > _CPU_WARN_PCT:
                        findings.append(
                            Finding(
                                severity=_severity_for_pct(cpu_pct, _CPU_WARN_PCT, _CPU_CRITICAL_PCT),
                                category="pod_resource",
                                resource=resource,
                                symptom=(
                                    f"CPU at {cpu_pct:.0f}% of limit "
                                    f"({format_cores(usage.cpu_cores)}/{cpu_limit_raw}, "
                                    f"container: {container.name})"
                                ),
                                context={
                                    "container": container.name,
                                    "cpu_usage_pct": round(cpu_pct, 1),
                                    "cpu_used_cores": usage.cpu_cores,
                                    "cpu_limit": cpu_limit_raw,
                                },
                            )
                        )

        return findings
