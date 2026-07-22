"""Detect running builds with abnormally large console output."""

import asyncio
import logging

from jenkins_watchdog.checks.base import Finding
from jenkins_watchdog.clients.jenkins import get_jenkins_http_client, get_running_builds, job_to_api_path

logger = logging.getLogger(__name__)

MB_50 = 50 * 1024 * 1024
MB_100 = 100 * 1024 * 1024


class BuildLogSizeCheck:
    name = "build_log_size"

    async def run(self) -> list[Finding]:
        findings: list[Finding] = []

        try:
            builds = await get_running_builds()
        except Exception as exc:
            logger.warning("Failed to fetch running builds: %s", exc)
            return findings

        if not builds:
            return findings

        semaphore = asyncio.Semaphore(5)

        async def _check_build(build: dict) -> Finding | None:
            job_name = build.get("name", "unknown")
            number = build.get("number", 0)
            url = build.get("url", "")

            async with semaphore:
                try:
                    client = get_jenkins_http_client()
                    resp = await client.head(f"{job_to_api_path(job_name)}/{number}/consoleText")
                    resp.raise_for_status()
                    content_length = resp.headers.get("content-length")
                    if content_length is None:
                        return None
                    size = int(content_length)
                except Exception as exc:
                    logger.debug("Failed to check log size for %s#%s: %s", job_name, number, exc)
                    return None

            if size > MB_100:
                severity = "critical"
                symptom = "Build log exceeding 100MB — possible log spam"
            elif size > MB_50:
                severity = "warning"
                symptom = "Build log exceeding 50MB"
            else:
                return None

            return Finding(
                severity=severity,
                category="jenkins_build",
                resource=f"jenkins-build/{job_name}#{number}",
                symptom=symptom,
                context={
                    "job_name": job_name,
                    "build_number": number,
                    "log_size_bytes": size,
                    "log_size_mb": round(size / (1024 * 1024), 1),
                    "url": url,
                },
            )

        results = await asyncio.gather(
            *[_check_build(build) for build in builds],
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Finding):
                findings.append(result)
            elif isinstance(result, Exception):
                logger.warning("Build log size check error: %s", result)

        return findings
