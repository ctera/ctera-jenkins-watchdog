"""Detect SCM polling errors on Jenkins jobs."""

import asyncio
import logging

from jenkins_watchdog.checks.base import Finding
from jenkins_watchdog.clients.jenkins import get_all_jobs, get_jenkins_http_client, job_to_api_path

logger = logging.getLogger(__name__)

MAX_JOBS = 30
CONCURRENCY = 10

_ERROR_INDICATORS = ("ERROR", "FATAL", "failed", "exception", "Could not")


def _has_polling_errors(log_text: str) -> bool:
    return any(indicator in log_text for indicator in _ERROR_INDICATORS)


class SCMPollingCheck:
    name = "scm_polling"

    async def run(self) -> list[Finding]:
        findings: list[Finding] = []

        try:
            jobs = await get_all_jobs(folder_depth=2)
        except Exception as exc:
            logger.warning("SCM polling check: failed to list jobs: %s", exc)
            return findings

        candidates = [
            job.get("fullname") or job.get("name", "")
            for job in jobs
            if job.get("fullname") or job.get("name")
        ][:MAX_JOBS]

        client = get_jenkins_http_client()
        semaphore = asyncio.Semaphore(CONCURRENCY)

        async def _check_job(job_name: str) -> Finding | None:
            async with semaphore:
                try:
                    resp = await client.get(f"{job_to_api_path(job_name)}/scmPollLog")
                except Exception as exc:
                    logger.debug("Failed to fetch SCM poll log for %s: %s", job_name, exc)
                    return None

                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
                log_text = resp.text

            if not _has_polling_errors(log_text):
                return None

            return Finding(
                severity="warning",
                category="jenkins_scm_polling",
                resource=f"jenkins-job/{job_name}",
                symptom=f"SCM polling errors on {job_name}",
                context={"job_name": job_name},
            )

        results = await asyncio.gather(
            *[_check_job(name) for name in candidates],
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Finding):
                findings.append(result)
            elif isinstance(result, Exception):
                logger.warning("SCM polling check error: %s", result)

        return findings
