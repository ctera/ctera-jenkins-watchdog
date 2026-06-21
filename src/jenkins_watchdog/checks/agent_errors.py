"""Jenkins agent container error checks — log error patterns and container failures."""

import logging

from jenkins_watchdog.checks.base import Finding
from jenkins_watchdog.clients.k8s import get_core_v1, run_sync

logger = logging.getLogger(__name__)

_ERROR_PATTERNS = [
    "java.lang.OutOfMemoryError",
    "java.net.ConnectException",
    "hudson.remoting.ChannelClosedException",
    "FATAL:",
    "Connection was broken",
    "ERROR: Connection terminated",
    "agent.jar: Connection refused",
    "JNLP agent failed to connect",
    "Slave JVM has terminated",
]


class AgentErrorCheck:
    name = "jenkins_agent_errors"

    async def run(self) -> list[Finding]:
        findings: list[Finding] = []
        v1 = get_core_v1()
        pods = await run_sync(v1.list_pod_for_all_namespaces, timeout_seconds=30)

        for pod in pods.items:
            name = pod.metadata.name
            ns = pod.metadata.namespace
            if not self._is_jenkins_agent(name):
                continue

            resource = f"{ns}/{name}"

            if not pod.status or not pod.status.container_statuses:
                continue

            for cs in pod.status.container_statuses:
                if cs.state and cs.state.terminated:
                    exit_code = cs.state.terminated.exit_code
                    reason = cs.state.terminated.reason or ""
                    if exit_code not in (0, None):
                        findings.append(
                            Finding(
                                severity="critical" if exit_code == 137 else "warning",
                                category="jenkins_agent",
                                resource=resource,
                                symptom=f"Container {cs.name} exited with code {exit_code} ({reason})",
                                context={
                                    "container": cs.name,
                                    "exit_code": exit_code,
                                    "reason": reason,
                                },
                            )
                        )

            try:
                for cs in pod.status.container_statuses:
                    if cs.state and cs.state.running:
                        logs = await run_sync(
                            v1.read_namespaced_pod_log,
                            name=name,
                            namespace=ns,
                            container=cs.name,
                            tail_lines=100,
                        )
                        if logs:
                            errors_found = []
                            for line in logs.split("\n"):
                                for pattern in _ERROR_PATTERNS:
                                    if pattern in line:
                                        errors_found.append(line.strip()[:200])
                                        break
                            if errors_found:
                                findings.append(
                                    Finding(
                                        severity="warning",
                                        category="jenkins_agent",
                                        resource=resource,
                                        symptom=f"{len(errors_found)} error(s) in {cs.name} logs",
                                        context={
                                            "container": cs.name,
                                            "error_count": len(errors_found),
                                            "sample_errors": errors_found[:3],
                                        },
                                    )
                                )
            except Exception as e:
                logger.debug("Failed to read logs for %s: %s", resource, e)

        return findings

    def _is_jenkins_agent(self, name: str) -> bool:
        name_lower = name.lower()
        return any(kw in name_lower for kw in ("jenkins-agent", "jnlp-agent", "jenkins-slave", "jenkins-worker"))
