"""Detect recent failed Jenkins builds, with emphasis on MR/PR pipelines."""

import logging
from collections import defaultdict
from datetime import UTC, datetime

from jenkins_watchdog.checks.base import Finding
from jenkins_watchdog.clients.jenkins import FailedBuildSummary, get_recent_failed_builds
from jenkins_watchdog.config import settings

logger = logging.getLogger(__name__)


def _worst_severity(results: list[str]) -> str:
    return "critical"


def _format_timestamp(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC).isoformat()


def _build_symptom(job_name: str, builds: list[FailedBuildSummary]) -> str:
    latest = builds[0]
    prefix = "MR build" if latest.is_mr else "Build"
    if len(builds) == 1:
        return f"{prefix} {latest.result}: {job_name} #{latest.build_number}"
    counts = f"FAILURE={len(builds)}"
    return f"{len(builds)} failed {prefix.lower()}s on {job_name} ({counts})"


def _group_by_job(failed_builds: list[FailedBuildSummary]) -> dict[str, list[FailedBuildSummary]]:
    grouped: dict[str, list[FailedBuildSummary]] = defaultdict(list)
    for build in failed_builds:
        grouped[build.job_name].append(build)
    for builds in grouped.values():
        builds.sort(key=lambda item: item.timestamp_ms, reverse=True)
    return grouped


class JenkinsFailedBuildCheck:
    name = "jenkins_failed_builds"

    async def run(self) -> list[Finding]:
        findings: list[Finding] = []

        try:
            failed_builds = await get_recent_failed_builds(
                window_hours=settings.jenkins_failed_build_window_hours,
            )
        except Exception as exc:
            logger.warning("Failed to check recent Jenkins builds: %s", exc)
            return findings

        if not failed_builds:
            return findings

        grouped = _group_by_job(failed_builds)
        for job_name, builds in grouped.items():
            results = [build.result for build in builds]
            findings.append(
                Finding(
                    severity=_worst_severity(results),
                    category="jenkins_failed_build",
                    resource=f"jenkins-job/{job_name}",
                    symptom=_build_symptom(job_name, builds),
                    context={
                        "job_name": job_name,
                        "is_mr": builds[0].is_mr,
                        "window_hours": settings.jenkins_failed_build_window_hours,
                        "failed_builds": [
                            {
                                **build.to_dict(),
                                "timestamp": _format_timestamp(build.timestamp_ms),
                            }
                            for build in builds
                        ],
                    },
                )
            )

        return findings
