"""Detect Jenkins builds that appear stuck or running far longer than expected."""

import logging
import time

from jenkins_watchdog.checks.base import Finding
from jenkins_watchdog.clients.jenkins import get_running_builds

logger = logging.getLogger(__name__)

_EIGHT_HOURS_S = 8 * 3600
_TWENTY_FOUR_HOURS_S = 24 * 3600


class ZombieBuildCheck:
    name = "zombie_builds"

    async def run(self) -> list[Finding]:
        findings: list[Finding] = []

        try:
            builds = await get_running_builds()
        except Exception as e:
            logger.warning("Failed to check for zombie builds: %s", e)
            return findings

        now_ms = time.time() * 1000

        for build in builds:
            job_name = build.get("name", "unknown")
            number = build.get("number", 0)
            url = build.get("url", "")
            timestamp = build.get("timestamp")
            estimated_duration = build.get("estimatedDuration") or 0

            if not timestamp:
                continue

            elapsed_s = max(0, (now_ms - timestamp) / 1000)
            elapsed_h = elapsed_s / 3600
            over_estimate = estimated_duration > 0 and elapsed_s * 1000 > 4 * estimated_duration
            over_eight_hours = elapsed_s > _EIGHT_HOURS_S
            over_twenty_four_hours = elapsed_s > _TWENTY_FOUR_HOURS_S

            if not (over_estimate or over_eight_hours or over_twenty_four_hours):
                continue

            if over_twenty_four_hours:
                severity = "critical"
                symptom = f"Likely zombie build running for {elapsed_h:.1f}h"
            elif over_estimate:
                severity = "warning"
                symptom = "Zombie build"
            else:
                severity = "warning"
                symptom = f"Build running for {elapsed_h:.1f}h"

            findings.append(
                Finding(
                    severity=severity,
                    category="jenkins_build",
                    resource=f"jenkins-build/{job_name}#{number}",
                    symptom=symptom,
                    context={
                        "job_name": job_name,
                        "build_number": number,
                        "node": build.get("node", ""),
                        "elapsed_hours": round(elapsed_h, 1),
                        "estimated_duration_ms": estimated_duration,
                        "url": url,
                    },
                )
            )

        return findings
