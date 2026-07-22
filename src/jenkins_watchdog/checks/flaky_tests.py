"""Detect Jenkins jobs with flaky pass/fail patterns across recent builds."""

import asyncio
import logging

from jenkins_watchdog.checks.base import Finding
from jenkins_watchdog.clients.jenkins import (
    FAILED_JOB_COLORS,
    get_all_jobs,
    get_job_recent_builds,
)

logger = logging.getLogger(__name__)

MAX_JOBS = 50
CONCURRENCY = 15
BUILD_LIMIT = 10


def _is_flaky(results: list[str | None]) -> tuple[bool, int, int]:
    outcomes = [r for r in results if r in ("SUCCESS", "FAILURE")]
    successes = outcomes.count("SUCCESS")
    failures = outcomes.count("FAILURE")
    if successes < 2 or failures < 2:
        return False, successes, failures
    if len(outcomes) < 2:
        return False, successes, failures
    all_consecutive_same = all(outcomes[i] == outcomes[i + 1] for i in range(len(outcomes) - 1))
    if all_consecutive_same:
        return False, successes, failures
    return True, successes, failures


class FlakyTestCheck:
    name = "flaky_tests"

    async def run(self) -> list[Finding]:
        findings: list[Finding] = []

        try:
            jobs = await get_all_jobs(folder_depth=2)
        except Exception as exc:
            logger.warning("Flaky test check: failed to list jobs: %s", exc)
            return findings

        candidates = [
            job.get("fullname") or job.get("name", "")
            for job in jobs
            if job.get("color") in FAILED_JOB_COLORS and (job.get("fullname") or job.get("name"))
        ][:MAX_JOBS]

        semaphore = asyncio.Semaphore(CONCURRENCY)

        async def _check_job(job_name: str) -> Finding | None:
            async with semaphore:
                try:
                    builds = await get_job_recent_builds(job_name, limit=BUILD_LIMIT)
                except Exception as exc:
                    logger.debug("Failed to fetch builds for %s: %s", job_name, exc)
                    return None

            sorted_builds = sorted(builds, key=lambda b: b.get("number", 0), reverse=True)
            results = [b.get("result") for b in sorted_builds[:BUILD_LIMIT]]
            flaky, successes, failures = _is_flaky(results)
            if not flaky:
                return None

            return Finding(
                severity="warning",
                category="jenkins_flaky_test",
                resource=f"jenkins-job/{job_name}",
                symptom=f"Flaky job: {successes} successes and {failures} failures in last 10 builds",
                context={
                    "job_name": job_name,
                    "successes": successes,
                    "failures": failures,
                    "recent_results": results,
                },
            )

        results = await asyncio.gather(
            *[_check_job(name) for name in candidates],
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Finding):
                findings.append(result)
            elif isinstance(result, Exception):
                logger.warning("Flaky test check error: %s", result)

        return findings
