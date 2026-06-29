"""Detect pipeline failure patterns — streaks, regressions, shared errors, parameter anomalies."""

import asyncio
import logging
from collections import defaultdict
from datetime import UTC, datetime

from jenkins_watchdog.checks.base import Finding
from jenkins_watchdog.clients.jenkins import (
    FailedBuildSummary,
    get_build_console_output,
    get_build_parameters,
    get_job_recent_builds,
    get_recent_failed_builds,
    is_mr_job,
)
from jenkins_watchdog.clients.log_analysis import (
    classify_failure,
    error_signature,
    extract_error_lines,
)
from jenkins_watchdog.scan_options import get_scan_options

logger = logging.getLogger(__name__)

CONSECUTIVE_FAILURE_THRESHOLD = 3
HISTORY_LIMIT = 15
LOG_FETCH_CONCURRENCY = 8


def _thresholds() -> tuple[int, int]:
    opts = get_scan_options()
    return opts.consecutive_failure_threshold, opts.pipeline_history_limit


def _format_ts(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC).isoformat()


def _analyze_streak(builds: list[dict]) -> dict:
    """Analyze recent build results for streaks and regressions."""
    sorted_builds = sorted(builds, key=lambda b: b.get("number", 0), reverse=True)
    results = [b.get("result") for b in sorted_builds]

    consecutive_failures = 0
    for result in results:
        if result == "FAILURE":
            consecutive_failures += 1
        else:
            break

    had_success = any(r == "SUCCESS" for r in results)
    recent_all_failed = len(results) >= 3 and all(r == "FAILURE" for r in results[:3])
    regression = had_success and results and results[0] == "FAILURE" and consecutive_failures >= 2

    last_success = next((b for b in sorted_builds if b.get("result") == "SUCCESS"), None)

    return {
        "consecutive_failures": consecutive_failures,
        "recent_all_failed": recent_all_failed,
        "regression": regression,
        "had_success_in_window": had_success,
        "last_success_build": last_success.get("number") if last_success else None,
        "last_success_at": _format_ts(last_success["timestamp"]) if last_success and last_success.get("timestamp") else None,
        "recent_results": results[:8],
    }


def _detect_parameter_anomalies(params: dict[str, str], job_name: str) -> list[str]:
    """Flag suspicious or empty build parameters."""
    anomalies: list[str] = []
    for name, value in params.items():
        if value in ("", "null", "None", "undefined"):
            anomalies.append(f"{name} is empty")
        elif name.lower() in ("branch", "git_branch", "BRANCH") and value in ("master", "main", "HEAD"):
            if is_mr_job(job_name):
                anomalies.append(f"{name}={value} on MR job (expected feature branch)")
        elif len(value) > 500:
            anomalies.append(f"{name} has unusually long value ({len(value)} chars)")
    return anomalies


async def _fetch_log_signature(job_name: str, build_number: int) -> tuple[list[str], str, str]:
    """Fetch build log and return error lines, signature, and failure class."""
    try:
        console = await get_build_console_output(job_name, build_number)
        error_lines = extract_error_lines(console)
        return error_lines, error_signature(error_lines), classify_failure(error_lines)
    except Exception as exc:
        logger.debug("Log fetch failed for %s#%s: %s", job_name, build_number, exc)
        return [], "", "unknown"


class JenkinsPipelinePatternCheck:
    name = "jenkins_pipeline_patterns"

    async def run(self) -> list[Finding]:
        findings: list[Finding] = []
        opts = get_scan_options()
        consecutive_threshold, history_limit = _thresholds()

        try:
            failed_builds = await get_recent_failed_builds(
                window_hours=opts.jenkins_failed_build_window_hours,
                build_limit=opts.jenkins_build_depth,
            )
        except Exception as exc:
            logger.warning("Pipeline pattern check: failed to get recent builds: %s", exc)
            return findings

        if not failed_builds:
            return findings

        grouped: dict[str, list[FailedBuildSummary]] = defaultdict(list)
        for build in failed_builds:
            grouped[build.job_name].append(build)

        semaphore = asyncio.Semaphore(LOG_FETCH_CONCURRENCY)
        signature_jobs: dict[str, list[str]] = defaultdict(list)

        async def _analyze_job(job_name: str, builds: list[FailedBuildSummary]) -> list[Finding]:
            job_findings: list[Finding] = []
            try:
                history = await get_job_recent_builds(job_name, limit=history_limit)
            except Exception as exc:
                logger.debug("Failed to fetch history for %s: %s", job_name, exc)
                return job_findings

            streak = _analyze_streak(history)
            latest = builds[0]

            error_lines: list[str] = []
            sig = ""
            failure_class = "unknown"
            params: dict[str, str] = {}
            param_anomalies: list[str] = []

            async with semaphore:
                error_lines, sig, failure_class = await _fetch_log_signature(
                    job_name, latest.build_number
                )
                try:
                    params = await get_build_parameters(job_name, latest.build_number)
                    param_anomalies = _detect_parameter_anomalies(params, job_name)
                except Exception:
                    pass

            if sig:
                signature_jobs[sig].append(job_name)

            base_context = {
                "job_name": job_name,
                "latest_build": latest.build_number,
                "is_mr": latest.is_mr,
                "failure_class": failure_class,
                "error_signature": sig,
                "error_lines": error_lines[:10],
                "build_parameters": params,
                "streak_analysis": streak,
            }

            if streak["consecutive_failures"] >= consecutive_threshold:
                job_findings.append(
                    Finding(
                        severity="critical",
                        category="jenkins_pipeline_pattern",
                        resource=f"jenkins-job/{job_name}",
                        symptom=(
                            f"{streak['consecutive_failures']} consecutive failures on {job_name} "
                            f"(#{latest.build_number})"
                        ),
                        context={
                            **base_context,
                            "pattern": "consecutive_failures",
                            "consecutive_count": streak["consecutive_failures"],
                        },
                    )
                )

            if streak["regression"] and streak["consecutive_failures"] < consecutive_threshold:
                job_findings.append(
                    Finding(
                        severity="warning" if streak["consecutive_failures"] == 2 else "critical",
                        category="jenkins_pipeline_pattern",
                        resource=f"jenkins-job/{job_name}",
                        symptom=(
                            f"Regression: {job_name} was passing, now {streak['consecutive_failures']} failure(s) "
                            f"(last success #{streak.get('last_success_build', '?')})"
                        ),
                        context={
                            **base_context,
                            "pattern": "regression",
                        },
                    )
                )

            if param_anomalies:
                job_findings.append(
                    Finding(
                        severity="warning",
                        category="jenkins_pipeline_pattern",
                        resource=f"jenkins-job/{job_name}",
                        symptom=f"Suspicious build parameters on {job_name} #{latest.build_number}",
                        context={
                            **base_context,
                            "pattern": "parameter_anomaly",
                            "parameter_anomalies": param_anomalies,
                        },
                    )
                )

            if error_lines and failure_class != "unknown" and not job_findings:
                job_findings.append(
                    Finding(
                        severity="warning",
                        category="jenkins_pipeline_pattern",
                        resource=f"jenkins-job/{job_name}",
                        symptom=(
                            f"{failure_class.replace('_', ' ')} in {job_name} #{latest.build_number}: "
                            f"{error_lines[-1][:120]}"
                        ),
                        context={
                            **base_context,
                            "pattern": "classified_failure",
                        },
                    )
                )

            return job_findings

        results = await asyncio.gather(
            *[_analyze_job(name, builds) for name, builds in grouped.items()],
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, list):
                findings.extend(result)
            elif isinstance(result, Exception):
                logger.warning("Job pattern analysis failed: %s", result)

        # Cross-job correlation: same error signature = shared root cause
        for sig, jobs in signature_jobs.items():
            if len(jobs) < 2 or not sig:
                continue
            unique_jobs = sorted(set(jobs))
            findings.append(
                Finding(
                    severity="critical",
                    category="jenkins_pipeline_pattern",
                    resource="jenkins-pipelines/shared-failure",
                    symptom=f"Same failure pattern across {len(unique_jobs)} jobs (sig={sig})",
                    context={
                        "pattern": "shared_failure_signature",
                        "error_signature": sig,
                        "affected_jobs": unique_jobs,
                        "job_count": len(unique_jobs),
                    },
                )
            )

        return findings
