"""Detect running builds that exceed their estimated duration."""

import logging
import time

from jenkins_watchdog.checks.base import Finding
from jenkins_watchdog.clients.jenkins import _job_name_from_build_url, get_jenkins_http_client

logger = logging.getLogger(__name__)

_COMPUTER_TREE = "computer[executors[currentExecutable[number,url,timestamp,estimatedDuration]]]"
THIRTY_MINUTES_MS = 30 * 60 * 1000


class BuildDurationCheck:
    name = "build_duration_anomaly"

    async def run(self) -> list[Finding]:
        findings: list[Finding] = []

        try:
            client = get_jenkins_http_client()
            resp = await client.get("/computer/api/json", params={"tree": _COMPUTER_TREE})
            resp.raise_for_status()
            computers = resp.json().get("computer", [])
        except Exception as exc:
            logger.warning("Failed to fetch running builds for duration check: %s", exc)
            return findings

        now_ms = time.time() * 1000

        for computer in computers:
            for executor in computer.get("executors", []):
                executable = executor.get("currentExecutable")
                if not executable:
                    continue

                timestamp = executable.get("timestamp")
                estimated = executable.get("estimatedDuration", 0)
                if not timestamp or estimated <= 0:
                    continue

                elapsed_ms = now_ms - timestamp
                if elapsed_ms <= 0:
                    continue

                ratio = elapsed_ms / estimated
                url = executable.get("url", "")
                number = executable.get("number", 0)
                job_name = _job_name_from_build_url(url)

                if ratio > 5:
                    severity = "critical"
                elif ratio > 3 and elapsed_ms > THIRTY_MINUTES_MS:
                    severity = "warning"
                else:
                    continue

                findings.append(
                    Finding(
                        severity=severity,
                        category="jenkins_build",
                        resource=f"jenkins-build/{job_name}#{number}",
                        symptom=f"Build running {ratio:.1f}x longer than expected",
                        context={
                            "job_name": job_name,
                            "build_number": number,
                            "elapsed_ms": int(elapsed_ms),
                            "expected_ms": estimated,
                            "elapsed_minutes": round(elapsed_ms / 60000, 1),
                            "expected_minutes": round(estimated / 60000, 1),
                            "ratio": round(ratio, 1),
                            "url": url,
                        },
                    )
                )

        return findings
