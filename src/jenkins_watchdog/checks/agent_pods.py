"""Jenkins agent pod health checks on k3s worker nodes."""

import logging

from jenkins_watchdog.checks.agent_utils import extract_agent_prefix, list_jenkins_agent_pods
from jenkins_watchdog.checks.base import CheckReport, Finding
from jenkins_watchdog.clients.k8s import KubernetesClient

logger = logging.getLogger(__name__)


class AgentPodCheck:
    name = "jenkins_agent_pods"

    def __init__(self, kubernetes: KubernetesClient, *, namespace: str) -> None:
        self._kubernetes = kubernetes
        self._namespace = namespace

    async def run(self) -> CheckReport:
        findings: list[Finding] = []
        pods = await list_jenkins_agent_pods(self._kubernetes, self._namespace)
        phases: dict[str, int] = {}
        total_restarts = 0

        for pod in pods:
            ns = pod.metadata.namespace
            name = pod.metadata.name
            resource = f"{ns}/{name}"
            finding_start = len(findings)
            agent_pool = extract_agent_prefix(name, getattr(pod.metadata, "labels", None) or {})
            node_name = getattr(getattr(pod, "spec", None), "node_name", "") or ""
            phase = pod.status.phase if pod.status and pod.status.phase else "Unknown"
            phases[phase] = phases.get(phase, 0) + 1

            if not pod.status or not pod.status.container_statuses:
                if pod.status and pod.status.phase == "Pending":
                    findings.append(
                        Finding(
                            severity="warning",
                            category="jenkins_agent",
                            resource=resource,
                            symptom="Agent pod stuck in Pending phase",
                            context={
                                "phase": pod.status.phase,
                                "agent_pool": agent_pool,
                                "pod_name": name,
                                "namespace": ns,
                                "node": node_name,
                            },
                        )
                    )
                continue

            for cs in pod.status.container_statuses:
                total_restarts += cs.restart_count
                if cs.last_state and cs.last_state.terminated:
                    if cs.last_state.terminated.reason == "OOMKilled":
                        findings.append(
                            Finding(
                                severity="critical",
                                category="jenkins_agent",
                                resource=resource,
                                symptom=f"OOMKilled (container: {cs.name})",
                                context={
                                    "restart_count": cs.restart_count,
                                    "container": cs.name,
                                },
                            )
                        )

                if cs.state and cs.state.waiting:
                    reason = cs.state.waiting.reason
                    if reason in ("CrashLoopBackOff", "ImagePullBackOff", "CreateContainerConfigError"):
                        findings.append(
                            Finding(
                                severity="critical" if reason == "CrashLoopBackOff" else "warning",
                                category="jenkins_agent",
                                resource=resource,
                                symptom=f"{reason} (container: {cs.name})",
                                context={
                                    "restart_count": cs.restart_count,
                                    "message": cs.state.waiting.message or "",
                                },
                            )
                        )

                if cs.restart_count >= 5:
                    if not any(f.resource == resource and "OOMKilled" in f.symptom for f in findings):
                        findings.append(
                            Finding(
                                severity="warning",
                                category="jenkins_agent",
                                resource=resource,
                                symptom=f"{cs.restart_count} restarts (container: {cs.name})",
                                context={"restart_count": cs.restart_count},
                            )
                        )

            if pod.metadata.deletion_timestamp and pod.status.phase != "Succeeded":
                findings.append(
                    Finding(
                        severity="warning",
                        category="jenkins_agent",
                        resource=resource,
                        symptom="Stuck terminating (deletion_timestamp set)",
                        context={"phase": pod.status.phase},
                    )
                )

            for finding in findings[finding_start:]:
                finding.context.setdefault("agent_pool", agent_pool)
                finding.context.setdefault("pod_name", name)
                finding.context.setdefault("namespace", ns)
                finding.context.setdefault("node", node_name)

        return CheckReport(
            findings=findings,
            summary={
                "agent_pod_count": len(pods),
                "pod_phases": phases,
                "total_container_restarts": total_restarts,
            },
        )
