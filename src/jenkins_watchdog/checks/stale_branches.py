"""Detect stale branches in multibranch Jenkins pipeline jobs."""

import asyncio
import logging
import time

from jenkins_watchdog.checks.base import Finding
from jenkins_watchdog.clients.jenkins import get_jenkins_http_client, job_to_api_path

logger = logging.getLogger(__name__)

MAX_PARENT_JOBS = 20
CONCURRENCY = 10
STALE_DAYS = 30
STALE_BRANCH_THRESHOLD = 5

_JOB_TREE = "jobs[name,color,url,jobs[name,color,url,jobs[name,color,url]]]"


def _flatten_jobs(jobs: list[dict], prefix: str = "") -> list[dict]:
    flat: list[dict] = []
    for job in jobs:
        name = job.get("name", "")
        if not name:
            continue
        fullname = f"{prefix}/{name}" if prefix else name
        flat.append({"name": name, "fullname": fullname})
        nested = job.get("jobs")
        if nested:
            flat.extend(_flatten_jobs(nested, fullname))
    return flat


class StaleBranchCheck:
    name = "stale_branches"

    async def run(self) -> list[Finding]:
        findings: list[Finding] = []
        client = get_jenkins_http_client()
        now_ms = time.time() * 1000

        try:
            resp = await client.get("/api/json", params={"tree": _JOB_TREE})
            resp.raise_for_status()
            root_jobs = resp.json().get("jobs", [])
        except Exception as exc:
            logger.warning("Stale branch check: failed to list jobs: %s", exc)
            return findings

        parent_candidates = _flatten_jobs(root_jobs)[:MAX_PARENT_JOBS]
        semaphore = asyncio.Semaphore(CONCURRENCY)

        async def _check_parent(job: dict) -> list[Finding]:
            job_name = job.get("fullname") or job.get("name", "")
            if not job_name:
                return []

            async with semaphore:
                try:
                    resp = await client.get(
                        f"{job_to_api_path(job_name)}/api/json",
                        params={"tree": "jobs[name,color,url,lastBuild[timestamp]]"},
                    )
                    resp.raise_for_status()
                    sub_jobs = resp.json().get("jobs") or []
                except Exception as exc:
                    logger.debug("Failed to fetch sub-jobs for %s: %s", job_name, exc)
                    return []

            if not sub_jobs:
                return []

            stale_branches: list[tuple[str, int]] = []
            for branch in sub_jobs:
                branch_name = branch.get("name", "")
                color = branch.get("color", "")
                if color == "disabled":
                    continue
                last_build = branch.get("lastBuild") or {}
                timestamp = last_build.get("timestamp")
                if not timestamp:
                    continue
                age_days = int((now_ms - timestamp) / (24 * 3600 * 1000))
                if age_days > STALE_DAYS:
                    stale_branches.append((branch_name, age_days))

            if len(stale_branches) <= STALE_BRANCH_THRESHOLD:
                return []

            return [
                Finding(
                    severity="low",
                    category="jenkins_stale_branch",
                    resource=f"jenkins-job/{job_name}/{branch_name}",
                    symptom=f"Stale branch: {branch_name} not built in {age_days} days",
                    context={
                        "parent_job": job_name,
                        "branch_name": branch_name,
                        "days_since_build": age_days,
                        "stale_branch_count": len(stale_branches),
                    },
                )
                for branch_name, age_days in stale_branches
            ]

        results = await asyncio.gather(
            *[_check_parent(job) for job in parent_candidates],
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, list):
                findings.extend(result)
            elif isinstance(result, Exception):
                logger.warning("Stale branch check error: %s", result)

        return findings
