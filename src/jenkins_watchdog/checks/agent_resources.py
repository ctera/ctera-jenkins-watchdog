"""Jenkins agent resource consumption checks — CPU/memory pressure on agent pods."""

import logging

from jenkins_watchdog.checks.agent_utils import is_jenkins_agent_pod, list_jenkins_agent_pods
from jenkins_watchdog.checks.base import Finding
from jenkins_watchdog.clients.k8s_metrics import (
    MetricsUnavailableError,
    format_bytes,
    format_cores,
    list_pod_metrics,
    parse_cpu_quantity,
    parse_memory_quantity,
    usage_pct,
)
from jenkins_watchdog.config import settings

logger = logging.getLogger(__name__)

_MEMORY_WARN_PCT = 80
_MEMORY_CRITICAL_PCT = 90
_CPU_WARN_PCT = 80
_CPU_CRITICAL_PCT = 90


def _severity_for_pct(pct: float, warn: float, critical: float) -> str:
    if pct > critical:
        return "critical"
    if pct > warn:
        return "warning"
    return "low"


class AgentResourceCheck:
    name = "jenkins_agent_resources"

    async def run(self) -> list[Finding]:
        findings: list[Finding] = []

        try:
            pod_metrics = await list_pod_metrics(settings.jenkins_namespace)
        except MetricsUnavailableError:
            logger.warning("Metrics-server unavailable — skipping agent resource checks")
            return []
        except Exception as exc:
            logger.warning("Failed to fetch pod metrics: %s", exc)
            return []

        metrics_by_pod = {m.name: m for m in pod_metrics}
        pods = await list_jenkins_agent_pods()

        for pod in pods:
            ns = pod.metadata.namespace
            name = pod.metadata.name
            if not is_jenkins_agent_pod(name, pod.metadata.labels or {}):
                continue

            resource = f"{ns}/{name}"
            pod_usage = metrics_by_pod.get(name)
            usage_by_container = {c.name: c for c in pod_usage.containers} if pod_usage else {}

            for container in pod.spec.containers:
                limits = container.resources.limits if container.resources else None
                mem_limit_raw = limits.get("memory") if limits else None
                cpu_limit_raw = limits.get("cpu") if limits else None

                if not mem_limit_raw and not cpu_limit_raw:
                    findings.append(
                        Finding(
                            severity="warning",
                            category="jenkins_agent",
                            resource=resource,
                            symptom=f"No resource limits set (container: {container.name})",
                            context={"container": container.name},
                        )
                    )
                    continue

                usage = usage_by_container.get(container.name)
                if not usage:
                    continue

                if mem_limit_raw:
                    mem_limit = parse_memory_quantity(mem_limit_raw)
                    mem_pct = usage_pct(usage.memory_bytes, mem_limit)
                    if mem_pct is not None and mem_pct > _MEMORY_WARN_PCT:
                        findings.append(
                            Finding(
                                severity=_severity_for_pct(mem_pct, _MEMORY_WARN_PCT, _MEMORY_CRITICAL_PCT),
                                category="jenkins_agent",
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
                                category="jenkins_agent",
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
