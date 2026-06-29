"""Detect recent failed Jenkins builds with log analysis context."""

import asyncio
import logging
from collections import defaultdict
from datetime import UTC, datetime

from jenkins_watchdog.checks.base import Finding
from jenkins_watchdog.clients.jenkins import FailedBuildSummary, get_build_console_output, get_recent_failed_builds
from jenkins_watchdog.clients.log_analysis import classify_failure, error_signature, extract_error_lines
from jenkins_watchdog.scan_options import get_scan_options

logger = logging.getLogger(__name__)

LOG_FETCH_CONCURRENCY = 10


def _worst_severity(results: list[str]) -> str:
    return "critical"


def _format_timestamp(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC).isoformat()


def _build_symptom(job_name: str, builds: list[FailedBuildSummary], failure_class: str = "") -> str:
    latest = builds[0]
    prefix = "MR build" if latest.is_mr else "Build"
    class_hint = f" [{failure_class}]" if failure_class and failure_class != "unknown" else ""
    if len(builds) == 1:
        return f"{prefix} {latest.result}{class_hint}: {job_name} #{latest.build_number}"
    counts = f"FAILURE={len(builds)}"
    return f"{len(builds)} failed {prefix.lower()}s on {job_name} ({counts}){class_hint}"


def _group_by_job(failed_builds: list[FailedBuildSummary]) -> dict[str, list[FailedBuildSummary]]:
    grouped: dict[str, list[FailedBuildSummary]] = defaultdict(list)
    for build in failed_builds:
        grouped[build.job_name].append(build)
    for builds in grouped.values():
        builds.sort(key=lambda item: item.timestamp_ms, reverse=True)
    return grouped


async def _enrich_with_log(job_name: str, build_number: int) -> dict:
    """Fetch console output and extract error context for the latest failed build."""
    try:
        console = await get_build_console_output(job_name, build_number)
        error_lines = extract_error_lines(console)
        return {
            "error_lines": error_lines[:15],
            "failure_class": classify_failure(error_lines),
            "error_signature": error_signature(error_lines),
            "log_tail_preview": error_lines[-3:] if error_lines else [],
        }
    except Exception as exc:
        logger.debug("Failed to fetch log for %s#%s: %s", job_name, build_number, exc)
        return {}


class JenkinsFailedBuildCheck:
    name = "jenkins_failed_builds"

    async def run(self) -> list[Finding]:
        findings: list[Finding] = []
        opts = get_scan_options()

        try:
            failed_builds = await get_recent_failed_builds(
                window_hours=opts.jenkins_failed_build_window_hours,
                build_limit=opts.jenkins_build_depth,
            )
        except Exception as exc:
            logger.warning("Failed to check recent Jenkins builds: %s", exc)
            return findings

        if not failed_builds:
            return findings

        grouped = _group_by_job(failed_builds)
        semaphore = asyncio.Semaphore(LOG_FETCH_CONCURRENCY)

        async def _process_job(job_name: str, builds: list[FailedBuildSummary]) -> Finding:
            log_context: dict = {}
            async with semaphore:
                log_context = await _enrich_with_log(job_name, builds[0].build_number)

            failure_class = log_context.get("failure_class", "unknown")
            return Finding(
                severity=_worst_severity([build.result for build in builds]),
                category="jenkins_failed_build",
                resource=f"jenkins-job/{job_name}",
                symptom=_build_symptom(job_name, builds, failure_class),
                context={
                    "job_name": job_name,
                    "is_mr": builds[0].is_mr,
                    "window_hours": opts.jenkins_failed_build_window_hours,
                    "build_depth": opts.jenkins_build_depth,
                    "deep_scan": opts.deep,
                    "failure_class": failure_class,
                    "error_signature": log_context.get("error_signature", ""),
                    "error_lines": log_context.get("error_lines", []),
                    "log_tail_preview": log_context.get("log_tail_preview", []),
                    "failed_builds": [
                        {
                            **build.to_dict(),
                            "timestamp": _format_timestamp(build.timestamp_ms),
                        }
                        for build in builds
                    ],
                    "investigation_hint": (
                        "Use jenkins_get_build_log to read full console output. "
                        "Compare with previous builds via jenkins_get_job_build_history. "
                        "Check if failure_class matches infrastructure vs test vs config."
                    ),
                },
            )

        results = await asyncio.gather(
            *[_process_job(name, builds) for name, builds in grouped.items()],
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Finding):
                findings.append(result)
            elif isinstance(result, Exception):
                logger.warning("Failed build enrichment error: %s", result)

        return findings
