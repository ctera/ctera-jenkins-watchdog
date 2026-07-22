"""Detect disabled Jenkins jobs that were built recently."""

import asyncio
import logging
import time

from jenkins_watchdog.checks.base import Finding
from jenkins_watchdog.clients.jenkins import get_all_jobs, get_jenkins_http_client, job_to_api_path

logger = logging.getLogger(__name__)

SEVEN_DAYS_MS = 7 * 24 * 3600 * 1000
MAX_DISABLED_JOBS = 20


class DisabledJobCheck:
    name = "disabled_jobs"

    async def run(self) -> list[Finding]:
        findings: list[Finding] = []

        try:
            jobs = await get_all_jobs(folder_depth=2)
        except Exception as exc:
            logger.warning("Failed to fetch Jenkins jobs: %s", exc)
            return findings

        disabled = [
            job.get("fullname") or job.get("name", "")
            for job in jobs
            if job.get("color") == "disabled" and (job.get("fullname") or job.get("name"))
        ][:MAX_DISABLED_JOBS]

        if not disabled:
            return findings

        semaphore = asyncio.Semaphore(10)
        cutoff_ms = time.time() * 1000 - SEVEN_DAYS_MS

        async def _check_job(job_name: str) -> Finding | None:
            async with semaphore:
                try:
                    client = get_jenkins_http_client()
                    tree = "name,fullName,color,lastBuild[number,timestamp],description"
                    resp = await client.get(
                        f"{job_to_api_path(job_name)}/api/json",
                        params={"tree": tree},
                    )
                    resp.raise_for_status()
                    data = resp.json()
                except Exception as exc:
                    logger.debug("Failed to fetch disabled job %s: %s", job_name, exc)
                    return None

            last_build = data.get("lastBuild")
            if not last_build:
                return None

            timestamp = last_build.get("timestamp")
            if not timestamp or timestamp < cutoff_ms:
                return None

            return Finding(
                severity="warning",
                category="jenkins_job",
                resource=f"jenkins-job/{job_name}",
                symptom="Job disabled but was built recently",
                context={
                    "job_name": job_name,
                    "full_name": data.get("fullName") or job_name,
                    "last_build_number": last_build.get("number"),
                    "last_build_timestamp_ms": timestamp,
                    "description": (data.get("description") or "")[:200],
                },
            )

        results = await asyncio.gather(*[_check_job(name) for name in disabled], return_exceptions=True)
        for result in results:
            if isinstance(result, Finding):
                findings.append(result)
            elif isinstance(result, Exception):
                logger.warning("Disabled job check error: %s", result)

        return findings
