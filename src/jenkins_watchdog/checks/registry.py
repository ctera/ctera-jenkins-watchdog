"""Check registry — auto-discover and run all checks in parallel."""

import asyncio
import logging
import time

from jenkins_watchdog.checks.agent_utils import group_agent_findings
from jenkins_watchdog.checks.base import Finding
from jenkins_watchdog.config import settings

logger = logging.getLogger(__name__)

CHECK_TIMEOUT_S = max(settings.request_timeout_s, 20.0)

_checks: list = []


def register_checks() -> None:
    """Import and register all check modules."""
    global _checks
    if _checks:
        return

    from jenkins_watchdog.checks.agent_connectivity import AgentConnectivityCheck
    from jenkins_watchdog.checks.agent_errors import AgentErrorCheck
    from jenkins_watchdog.checks.agent_pods import AgentPodCheck
    from jenkins_watchdog.checks.agent_resources import AgentResourceCheck
    from jenkins_watchdog.checks.jenkins_failed_builds import JenkinsFailedBuildCheck
    from jenkins_watchdog.checks.jenkins_jobs import JenkinsJobCheck
    from jenkins_watchdog.checks.jenkins_pipeline_patterns import JenkinsPipelinePatternCheck
    from jenkins_watchdog.checks.k8s_events import K8sEventsCheck
    from jenkins_watchdog.checks.k8s_nodes import NodeCheck
    from jenkins_watchdog.checks.k8s_workloads import WorkloadCheck

    _checks = [
        AgentPodCheck(),
        AgentResourceCheck(),
        AgentErrorCheck(),
        AgentConnectivityCheck(),
        JenkinsJobCheck(),
        JenkinsFailedBuildCheck(),
        JenkinsPipelinePatternCheck(),
        NodeCheck(),
        WorkloadCheck(),
        K8sEventsCheck(),
    ]


async def run_all_checks(cancel_event: asyncio.Event | None = None) -> list[Finding]:
    """Run all registered checks in parallel, collect findings."""
    register_checks()

    start = time.monotonic()
    tasks = [asyncio.create_task(_run_single(check)) for check in _checks]
    try:
        while True:
            if cancel_event and cancel_event.is_set():
                raise asyncio.CancelledError()
            if all(t.done() for t in tasks):
                break
            await asyncio.wait(tasks, timeout=0.5, return_when=asyncio.FIRST_COMPLETED)
        results = []
        for t in tasks:
            if t.cancelled():
                raise asyncio.CancelledError()
            try:
                results.append(t.result())
            except Exception as exc:
                results.append(exc)
    except asyncio.CancelledError:
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    findings: list[Finding] = []
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            logger.error("Check '%s' failed: %s", _checks[i].name, result)
        elif isinstance(result, list):
            findings.extend(result)

    raw_count = len(findings)
    findings = group_agent_findings(findings)

    elapsed = time.monotonic() - start
    logger.info(
        "All checks completed in %.2fs, %d findings (%d raw, %d grouped)",
        elapsed,
        len(findings),
        raw_count,
        raw_count - len(findings),
    )
    return findings


async def _run_single(check) -> list[Finding]:
    """Run a single check with timeout."""
    try:
        return await asyncio.wait_for(check.run(), timeout=CHECK_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.error("Check '%s' timed out after %.0fs", check.name, CHECK_TIMEOUT_S)
        return []
