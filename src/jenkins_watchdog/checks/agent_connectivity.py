"""Jenkins agent health checks — executors, memory, and disk monitors."""

from jenkins_watchdog.checks.base import CheckReport, Finding
from jenkins_watchdog.clients.jenkins import JenkinsClient


class AgentConnectivityCheck:
    name = "jenkins_agent_connectivity"

    def __init__(self, jenkins: JenkinsClient) -> None:
        self._jenkins = jenkins

    async def run(self) -> CheckReport:
        findings: list[Finding] = []
        nodes = await self._jenkins.get_nodes()

        for node in nodes:
            name = node.get("displayName", "unknown")
            if name == "Built-In Node" or name == "master":
                continue

            resource = f"jenkins-agent/{name}"
            offline = node.get("offline", False)
            idle = node.get("idle", True)
            num_executors = node.get("numExecutors", 0)

            if not offline and not idle and num_executors == 0:
                findings.append(
                    Finding(
                        severity="warning",
                        category="jenkins_agent",
                        resource=resource,
                        symptom="Agent online but has 0 executors configured",
                        context={"num_executors": num_executors},
                    )
                )

            monitor_data = node.get("monitorData", {})
            if monitor_data and not offline:
                swap_monitor = monitor_data.get("hudson.node_monitors.SwapSpaceMonitor")
                if swap_monitor:
                    total_physical = swap_monitor.get("totalPhysicalMemory", 0)
                    available_physical = swap_monitor.get("availablePhysicalMemory", 0)
                    if total_physical and available_physical:
                        used_pct = (1 - available_physical / total_physical) * 100
                        if used_pct > 90:
                            findings.append(
                                Finding(
                                    severity="critical" if used_pct > 95 else "warning",
                                    category="jenkins_agent",
                                    resource=resource,
                                    symptom=f"Agent memory at {used_pct:.0f}% used",
                                    context={
                                        "total_memory_gb": round(total_physical / (1024**3), 1),
                                        "available_memory_gb": round(available_physical / (1024**3), 1),
                                        "used_pct": round(used_pct, 1),
                                    },
                                )
                            )

                disk_monitor = monitor_data.get("hudson.node_monitors.DiskSpaceMonitor")
                if disk_monitor:
                    free_bytes = disk_monitor.get("size", 0)
                    if free_bytes and free_bytes < 5 * 1024**3:
                        findings.append(
                            Finding(
                                severity="critical" if free_bytes < 1 * 1024**3 else "warning",
                                category="jenkins_agent",
                                resource=resource,
                                symptom=f"Agent disk space low: {free_bytes / (1024**3):.1f}GB free",
                                context={
                                    "free_disk_gb": round(free_bytes / (1024**3), 1),
                                    "path": disk_monitor.get("path", ""),
                                },
                            )
                        )

        offline = [node for node in nodes if node.get("offline")]
        temporarily_offline = [node for node in nodes if node.get("temporarilyOffline")]
        return CheckReport(
            findings=findings,
            summary={
                "agent_count": len(nodes),
                "online_agent_count": len(nodes) - len(offline),
                "offline_agent_count": len(offline),
                "temporarily_offline_count": len(temporarily_offline),
                "executor_count": sum(int(node.get("numExecutors") or 0) for node in nodes),
            },
        )
