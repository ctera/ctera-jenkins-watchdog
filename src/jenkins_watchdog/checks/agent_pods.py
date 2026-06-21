"""Jenkins agent pod health checks on k3s worker nodes."""

import logging

from jenkins_watchdog.checks.base import Finding
from jenkins_watchdog.clients.k8s import get_core_v1, run_sync

logger = logging.getLogger(__name__)

_JENKINS_LABELS = [
    "jenkins=agent",
    "jenkins/label",
    "app=jenkins-agent",
    "jenkins-agent=true",
]


class AgentPodCheck:
    name = "jenkins_agent_pods"

    async def run(self) -> list[Finding]:
        findings: list[Finding] = []
        v1 = get_core_v1()
        pods = await run_sync(v1.list_pod_for_all_namespaces, timeout_seconds=30)

        for pod in pods.items:
            ns = pod.metadata.namespace
            name = pod.metadata.name
            labels = pod.metadata.labels or {}

            if not self._is_jenkins_agent(name, labels):
                continue

            resource = f"{ns}/{name}"

            if not pod.status or not pod.status.container_statuses:
                if pod.status and pod.status.phase == "Pending":
                    findings.append(
                        Finding(
                            severity="warning",
                            category="jenkins_agent",
                            resource=resource,
                            symptom="Agent pod stuck in Pending phase",
                            context={"phase": pod.status.phase},
                        )
                    )
                continue

            for cs in pod.status.container_statuses:
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

        return findings

    def _is_jenkins_agent(self, name: str, labels: dict) -> bool:
        """Detect Jenkins agent pods by name pattern or labels."""
        name_lower = name.lower()
        if any(kw in name_lower for kw in ("jenkins-agent", "jnlp-agent", "jenkins-slave", "jenkins-worker")):
            return True
        for label_key in _JENKINS_LABELS:
            if "=" in label_key:
                key, val = label_key.split("=", 1)
                if labels.get(key) == val:
                    return True
            elif label_key in labels:
                return True
        if labels.get("app") == "jenkins" and labels.get("component") == "agent":
            return True
        return False
