"""Jenkins agent pod health checks on k3s worker nodes."""

import logging

from jenkins_watchdog.checks.agent_utils import list_jenkins_agent_pods
from jenkins_watchdog.checks.base import Finding

logger = logging.getLogger(__name__)


class AgentPodCheck:
    name = "jenkins_agent_pods"

    async def run(self) -> list[Finding]:
        findings: list[Finding] = []
        pods = await list_jenkins_agent_pods()

        for pod in pods:
            ns = pod.metadata.namespace
            name = pod.metadata.name
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
