"""Jenkins job and build queue checks — stuck builds, queue congestion, executor starvation."""

import time

from jenkins_watchdog.checks.base import CheckReport, Finding
from jenkins_watchdog.clients.jenkins import JenkinsClient

STUCK_QUEUE_THRESHOLD_S = 600  # 10 minutes
LONG_BUILD_THRESHOLD_S = 7200  # 2 hours
CRITICAL_BUILD_THRESHOLD_S = 21600  # 6 hours


class JenkinsJobCheck:
    name = "jenkins_jobs"

    def __init__(self, jenkins: JenkinsClient) -> None:
        self._jenkins = jenkins

    async def run(self) -> CheckReport:
        findings: list[Finding] = []
        queue = await self._jenkins.get_queue_info()
        stuck_items = []
        for item in queue:
            in_queue_since = item.get("inQueueSince", 0)
            if not in_queue_since:
                continue
            wait_s = (time.time() * 1000 - in_queue_since) / 1000
            if wait_s <= STUCK_QUEUE_THRESHOLD_S:
                continue
            why = item.get("why", "Unknown reason")
            task_name = item.get("task", {}).get("name", "unknown")
            stuck_items.append(
                {
                    "queue_item_id": item.get("id"),
                    "task": task_name,
                    "wait_minutes": round(wait_s / 60, 1),
                    "reason": why[:200],
                }
            )

        queue_severity = "critical" if len(stuck_items) > 3 else "warning"
        for item in stuck_items:
            identifier = item["queue_item_id"] or item["task"]
            findings.append(
                Finding(
                    severity=queue_severity,
                    category="jenkins_queue",
                    resource=f"jenkins-queue/{identifier}",
                    symptom="Jenkins job stuck in queue",
                    context={
                        "queue_task": item["task"],
                        "queue_item_id": item["queue_item_id"],
                        "wait_minutes": item["wait_minutes"],
                        "reason": item["reason"],
                        "total_queue_size": len(queue),
                        "total_stuck_items": len(stuck_items),
                    },
                )
            )

        if len(queue) > 20:
            findings.append(
                Finding(
                    severity="warning",
                    category="jenkins_queue",
                    resource="jenkins-queue",
                    symptom=f"Build queue has {len(queue)} items (congestion)",
                    context={"queue_size": len(queue)},
                )
            )

        builds = await self._jenkins.get_running_builds()
        long_running = []
        for build in builds:
            job_name = build.get("name", "unknown")
            number = build.get("number", 0)
            url = build.get("url", "")
            timestamp = build.get("timestamp")
            elapsed_s = max(0, (time.time() * 1000 - timestamp) / 1000) if timestamp else 0
            if elapsed_s <= LONG_BUILD_THRESHOLD_S:
                continue
            elapsed_hours = round(elapsed_s / 3600, 1)
            item = {
                "job_name": job_name,
                "build_number": number,
                "elapsed_hours": elapsed_hours,
                "url": url,
                "node": build.get("node") or "unknown",
                "started_at_ms": timestamp,
            }
            long_running.append(item)
            findings.append(
                Finding(
                    severity="critical" if elapsed_s > CRITICAL_BUILD_THRESHOLD_S else "warning",
                    category="jenkins_build",
                    resource=f"jenkins-build/{job_name}#{number}",
                    symptom=f"Build running for {elapsed_hours:.1f}h",
                    context=item,
                )
            )

        long_running.sort(key=lambda item: item["elapsed_hours"], reverse=True)
        return CheckReport(
            findings=findings,
            summary={
                "queue_size": len(queue),
                "stuck_queue_count": len(stuck_items),
                "oldest_queue_wait_minutes": max((item["wait_minutes"] for item in stuck_items), default=0),
                "stuck_queue_items": stuck_items[:10],
                "running_build_count": len(builds),
                "long_running_build_count": len(long_running),
                "oldest_running_build_hours": max((item["elapsed_hours"] for item in long_running), default=0),
                "long_running_builds": long_running[:25],
            },
        )
