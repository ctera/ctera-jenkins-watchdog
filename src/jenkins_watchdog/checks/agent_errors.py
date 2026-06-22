"""Jenkins agent container error checks — log error patterns and container failures."""

import asyncio
import logging

from jenkins_watchdog.checks.agent_utils import list_jenkins_agent_pods
from jenkins_watchdog.checks.base import Finding
from jenkins_watchdog.clients.k8s import get_core_v1, run_sync
from jenkins_watchdog.config import settings

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

_LOG_READ_CONCURRENCY = 10


class AgentErrorCheck:
    name = "jenkins_agent_errors"

    async def run(self) -> list[Finding]:
        findings: list[Finding] = []
        v1 = get_core_v1()
        pods = await list_jenkins_agent_pods()
        semaphore = asyncio.Semaphore(_LOG_READ_CONCURRENCY)

        async def check_pod(pod) -> list[Finding]:
            pod_findings: list[Finding] = []
            name = pod.metadata.name
            ns = pod.metadata.namespace
            resource = f"{ns}/{name}"

            if not pod.status or not pod.status.container_statuses:
                return pod_findings

            for cs in pod.status.container_statuses:
                if cs.state and cs.state.terminated:
                    exit_code = cs.state.terminated.exit_code
                    reason = cs.state.terminated.reason or ""
                    if exit_code not in (0, None):
                        pod_findings.append(
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

            running_containers = [
                cs for cs in pod.status.container_statuses if cs.state and cs.state.running
            ]
            if not running_containers:
                return pod_findings

            async with semaphore:
                for cs in running_containers:
                    try:
                        logs = await asyncio.wait_for(
                            run_sync(
                                v1.read_namespaced_pod_log,
                                name=name,
                                namespace=ns,
                                container=cs.name,
                                tail_lines=100,
                            ),
                            timeout=settings.request_timeout_s,
                        )
                    except Exception as e:
                        logger.debug("Failed to read logs for %s/%s: %s", ns, name, e)
                        continue

                    if not logs:
                        continue

                    errors_found = []
                    for line in logs.split("\n"):
                        for pattern in _ERROR_PATTERNS:
                            if pattern in line:
                                errors_found.append(line.strip()[:200])
                                break
                    if errors_found:
                        pod_findings.append(
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

            return pod_findings

        results = await asyncio.gather(
            *[check_pod(pod) for pod in pods],
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Exception):
                logger.warning("Agent log check failed: %s", result)
            elif isinstance(result, list):
                findings.extend(result)

        return findings
