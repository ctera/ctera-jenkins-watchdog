"""Jenkins controller (master) health checks."""

import logging

from jenkins_watchdog.checks.base import Finding
from jenkins_watchdog.clients.jenkins import get_jenkins_http_client

logger = logging.getLogger(__name__)

_GB = 1024**3
_DISK_WARN_BYTES = 10 * _GB
_DISK_CRITICAL_BYTES = 2 * _GB
_TEMP_WARN_BYTES = 2 * _GB
_MEM_WARN_PCT = 90
_MEM_CRITICAL_PCT = 95
_RESPONSE_WARN_MS = 5000

_CONTROLLER_TREE = (
    "displayName,monitorData[hudson.node_monitors.DiskSpaceMonitor[size,path],"
    "hudson.node_monitors.TemporarySpaceMonitor[size],"
    "hudson.node_monitors.SwapSpaceMonitor[totalPhysicalMemory,availablePhysicalMemory,"
    "totalSwapSpace,availableSwapSpace],"
    "hudson.node_monitors.ResponseTimeMonitor[average]]"
)


class ControllerHealthCheck:
    name = "controller_health"

    async def run(self) -> list[Finding]:
        findings: list[Finding] = []
        client = get_jenkins_http_client()

        try:
            resp = await client.get(
                "/computer/(master)/api/json",
                params={"tree": _CONTROLLER_TREE},
            )
            if resp.status_code == 404:
                resp = await client.get("/computer/api/json", params={"tree": f"computer[{_CONTROLLER_TREE}]"})
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("Failed to check controller health: %s", e)
            return findings

        if "computer" in data:
            controller = _find_builtin_node(data.get("computer", []))
        else:
            controller = data

        if not controller:
            logger.warning("Built-In Node not found in Jenkins computer list")
            return findings

        monitor_data = controller.get("monitorData") or {}
        context: dict = {"node": controller.get("displayName", "Built-In Node")}

        try:
            info_resp = await client.get("/api/json", params={"tree": "quietingDown,mode,jenkinsVersion"})
            info_resp.raise_for_status()
            info = info_resp.json()
            context["jenkins_version"] = info.get("jenkinsVersion") or info.get("version")
            context["mode"] = info.get("mode")

            if info.get("quietingDown"):
                findings.append(
                    Finding(
                        severity="warning",
                        category="jenkins_controller",
                        resource="jenkins-controller",
                        symptom="Jenkins is in quiet-down mode (preparing for restart)",
                        context={**context, "quieting_down": True},
                    )
                )
        except Exception as e:
            logger.debug("Failed to fetch Jenkins controller info: %s", e)

        disk_monitor = monitor_data.get("hudson.node_monitors.DiskSpaceMonitor") or {}
        free_disk = disk_monitor.get("size")
        if free_disk is not None:
            context["disk_free_bytes"] = free_disk
            context["disk_path"] = disk_monitor.get("path")
            if free_disk < _DISK_CRITICAL_BYTES:
                findings.append(
                    Finding(
                        severity="critical",
                        category="jenkins_controller",
                        resource="jenkins-controller",
                        symptom="Jenkins controller disk critically low",
                        context=context,
                    )
                )
            elif free_disk < _DISK_WARN_BYTES:
                free_gb = free_disk / _GB
                findings.append(
                    Finding(
                        severity="warning",
                        category="jenkins_controller",
                        resource="jenkins-controller",
                        symptom=f"Jenkins controller disk space low ({free_gb:.1f}GB free)",
                        context=context,
                    )
                )

        temp_monitor = monitor_data.get("hudson.node_monitors.TemporarySpaceMonitor") or {}
        free_temp = temp_monitor.get("size")
        if free_temp is not None and free_temp < _TEMP_WARN_BYTES:
            free_gb = free_temp / _GB
            context["temp_free_bytes"] = free_temp
            findings.append(
                Finding(
                    severity="warning",
                    category="jenkins_controller",
                    resource="jenkins-controller",
                    symptom=f"Jenkins controller temp space low ({free_gb:.1f}GB free)",
                    context=context,
                )
            )

        swap_monitor = monitor_data.get("hudson.node_monitors.SwapSpaceMonitor") or {}
        total_mem = swap_monitor.get("totalPhysicalMemory")
        available_mem = swap_monitor.get("availablePhysicalMemory")
        if total_mem and available_mem is not None and total_mem > 0:
            used_pct = ((total_mem - available_mem) / total_mem) * 100
            context["memory_used_pct"] = round(used_pct, 1)
            context["total_physical_memory"] = total_mem
            context["available_physical_memory"] = available_mem

            if used_pct > _MEM_CRITICAL_PCT:
                findings.append(
                    Finding(
                        severity="critical",
                        category="jenkins_controller",
                        resource="jenkins-controller",
                        symptom=f"Jenkins controller memory critically high ({used_pct:.0f}% used)",
                        context=context,
                    )
                )
            elif used_pct > _MEM_WARN_PCT:
                findings.append(
                    Finding(
                        severity="warning",
                        category="jenkins_controller",
                        resource="jenkins-controller",
                        symptom=f"Jenkins controller memory high ({used_pct:.0f}% used)",
                        context=context,
                    )
                )

        response_monitor = monitor_data.get("hudson.node_monitors.ResponseTimeMonitor") or {}
        avg_response = response_monitor.get("average")
        if avg_response is not None and avg_response > _RESPONSE_WARN_MS:
            context["avg_response_ms"] = avg_response
            findings.append(
                Finding(
                    severity="warning",
                    category="jenkins_controller",
                    resource="jenkins-controller",
                    symptom=f"Jenkins controller slow (avg response {avg_response:.0f}ms)",
                    context=context,
                )
            )

        return findings


def _find_builtin_node(computers: list) -> dict | None:
    for node in computers:
        name = node.get("displayName", "")
        if name in ("Built-In Node", "master"):
            return node
    return None
