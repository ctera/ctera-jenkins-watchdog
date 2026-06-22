"""Jenkins job and build queue checks — stuck builds, queue congestion, executor starvation."""

import logging
import time

from jenkins_watchdog.checks.base import Finding
from jenkins_watchdog.clients.jenkins import get_queue_info, get_running_builds

logger = logging.getLogger(__name__)

STUCK_QUEUE_THRESHOLD_S = 600  # 10 minutes
LONG_BUILD_THRESHOLD_S = 7200  # 2 hours


class JenkinsJobCheck:
    name = "jenkins_jobs"

    async def run(self) -> list[Finding]:
        findings: list[Finding] = []

        try:
            queue = await get_queue_info()
            if queue:
                stuck_items = []
                for item in queue:
                    in_queue_since = item.get("inQueueSince", 0)
                    if in_queue_since:
                        wait_s = (time.time() * 1000 - in_queue_since) / 1000
                        if wait_s > STUCK_QUEUE_THRESHOLD_S:
                            why = item.get("why", "Unknown reason")
                            task_name = item.get("task", {}).get("name", "unknown")
                            stuck_items.append({
                                "task": task_name,
                                "wait_minutes": round(wait_s / 60, 1),
                                "reason": why[:200],
                            })

                if stuck_items:
                    findings.append(
                        Finding(
                            severity="critical" if len(stuck_items) > 3 else "warning",
                            category="jenkins_queue",
                            resource="jenkins-queue",
                            symptom=f"{len(stuck_items)} build(s) stuck in queue",
                            context={
                                "stuck_builds": stuck_items[:5],
                                "total_queue_size": len(queue),
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
        except Exception as e:
            logger.warning("Failed to check Jenkins queue: %s", e)

        try:
            builds = await get_running_builds()
            for build in builds:
                job_name = build.get("name", "unknown")
                number = build.get("number", 0)
                url = build.get("url", "")

                timestamp = build.get("timestamp")
                if timestamp:
                    elapsed_s = max(0, (time.time() * 1000 - timestamp) / 1000)
                else:
                    elapsed_s = 0

                if elapsed_s > LONG_BUILD_THRESHOLD_S:
                    findings.append(
                        Finding(
                            severity="warning",
                            category="jenkins_build",
                            resource=f"jenkins-build/{job_name}#{number}",
                            symptom=f"Build running for {elapsed_s / 3600:.1f}h",
                            context={
                                "job_name": job_name,
                                "build_number": number,
                                "elapsed_hours": round(elapsed_s / 3600, 1),
                                "url": url,
                            },
                        )
                    )
        except Exception as e:
            logger.warning("Failed to check running builds: %s", e)

        return findings
