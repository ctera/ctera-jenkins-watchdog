"""Jenkins executor pool utilization checks."""

import logging

from jenkins_watchdog.checks.base import Finding
from jenkins_watchdog.clients.jenkins import _get_computers, get_queue_info

logger = logging.getLogger(__name__)


class ExecutorUtilizationCheck:
    name = "executor_utilization"

    async def run(self) -> list[Finding]:
        findings: list[Finding] = []

        try:
            computers = await _get_computers()
        except Exception as e:
            logger.warning("Failed to check executor utilization: %s", e)
            return findings

        total_executors = 0
        busy_executors = 0

        for computer in computers:
            name = computer.get("displayName", "unknown")
            if name in ("Built-In Node", "master"):
                continue
            if computer.get("offline", False):
                continue

            total_executors += computer.get("numExecutors", 0)
            for executor in computer.get("executors", []):
                executable = executor.get("currentExecutable")
                if executable and "number" in executable:
                    busy_executors += 1

        if total_executors == 0:
            findings.append(
                Finding(
                    severity="critical",
                    category="jenkins_executor",
                    resource="jenkins-executors",
                    symptom="No executors available",
                    context={"total_executors": 0, "busy_executors": 0},
                )
            )
            return findings

        utilization_pct = (busy_executors / total_executors) * 100

        if utilization_pct == 100:
            try:
                queue = await get_queue_info()
            except Exception as e:
                logger.warning("Failed to check Jenkins queue: %s", e)
                queue = []

            if queue:
                findings.append(
                    Finding(
                        severity="critical",
                        category="jenkins_executor",
                        resource="jenkins-executors",
                        symptom="All executors busy, builds queuing",
                        context={
                            "total_executors": total_executors,
                            "busy_executors": busy_executors,
                            "queue_size": len(queue),
                        },
                    )
                )

        if utilization_pct > 90:
            findings.append(
                Finding(
                    severity="warning",
                    category="jenkins_executor",
                    resource="jenkins-executors",
                    symptom=f"Executor pool at {utilization_pct:.0f}% utilization ({busy_executors}/{total_executors})",
                    context={
                        "total_executors": total_executors,
                        "busy_executors": busy_executors,
                        "utilization_pct": round(utilization_pct, 1),
                    },
                )
            )

        return findings
