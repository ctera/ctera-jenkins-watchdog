"""Jenkins agent resource consumption checks — CPU/memory pressure on agent pods."""

import logging

from jenkins_watchdog.checks.base import Finding
from jenkins_watchdog.clients.prometheus import query_instant

logger = logging.getLogger(__name__)


class AgentResourceCheck:
    name = "jenkins_agent_resources"

    async def run(self) -> list[Finding]:
        findings: list[Finding] = []

        try:
            mem_results = await query_instant(
                'container_memory_working_set_bytes{container!="POD",container!=""}'
                ' / on(namespace,pod,container) '
                'kube_pod_container_resource_limits{resource="memory"} > 0.85'
            )
            for r in mem_results:
                pod = r["metric"].get("pod", "")
                ns = r["metric"].get("namespace", "")
                container = r["metric"].get("container", "")
                if not self._is_jenkins_agent_pod(pod):
                    continue
                usage_pct = float(r["value"][1]) * 100
                findings.append(
                    Finding(
                        severity="critical" if usage_pct > 95 else "warning",
                        category="jenkins_agent",
                        resource=f"{ns}/{pod}",
                        symptom=f"Memory at {usage_pct:.0f}% of limit (container: {container})",
                        context={
                            "container": container,
                            "memory_usage_pct": round(usage_pct, 1),
                        },
                    )
                )
        except Exception as e:
            logger.warning("Prometheus memory query failed: %s", e)

        try:
            cpu_results = await query_instant(
                'rate(container_cpu_usage_seconds_total{container!="POD",container!=""}[5m])'
                ' / on(namespace,pod,container) '
                'kube_pod_container_resource_limits{resource="cpu"} > 0.90'
            )
            for r in cpu_results:
                pod = r["metric"].get("pod", "")
                ns = r["metric"].get("namespace", "")
                container = r["metric"].get("container", "")
                if not self._is_jenkins_agent_pod(pod):
                    continue
                usage_pct = float(r["value"][1]) * 100
                findings.append(
                    Finding(
                        severity="warning",
                        category="jenkins_agent",
                        resource=f"{ns}/{pod}",
                        symptom=f"CPU at {usage_pct:.0f}% of limit (container: {container})",
                        context={
                            "container": container,
                            "cpu_usage_pct": round(usage_pct, 1),
                        },
                    )
                )
        except Exception as e:
            logger.warning("Prometheus CPU query failed: %s", e)

        try:
            restart_results = await query_instant(
                'increase(kube_pod_container_status_restarts_total[1h]) > 3'
            )
            for r in restart_results:
                pod = r["metric"].get("pod", "")
                ns = r["metric"].get("namespace", "")
                if not self._is_jenkins_agent_pod(pod):
                    continue
                restarts = int(float(r["value"][1]))
                findings.append(
                    Finding(
                        severity="warning",
                        category="jenkins_agent",
                        resource=f"{ns}/{pod}",
                        symptom=f"{restarts} restarts in last hour",
                        context={"restarts_1h": restarts},
                    )
                )
        except Exception as e:
            logger.warning("Prometheus restart query failed: %s", e)

        return findings

    def _is_jenkins_agent_pod(self, name: str) -> bool:
        name_lower = name.lower()
        return any(kw in name_lower for kw in ("jenkins-agent", "jnlp-agent", "jenkins-slave", "jenkins-worker"))
