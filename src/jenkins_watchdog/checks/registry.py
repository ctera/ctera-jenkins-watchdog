"""Check registry — auto-discover and run all checks in parallel."""

import asyncio
import logging
import time

from jenkins_watchdog.checks.base import Finding

logger = logging.getLogger(__name__)

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
    from jenkins_watchdog.checks.jenkins_jobs import JenkinsJobCheck
    from jenkins_watchdog.checks.k8s_nodes import NodeCheck
    from jenkins_watchdog.checks.k8s_workloads import WorkloadCheck

    _checks = [
        AgentPodCheck(),
        AgentResourceCheck(),
        AgentErrorCheck(),
        AgentConnectivityCheck(),
        JenkinsJobCheck(),
        NodeCheck(),
        WorkloadCheck(),
    ]


async def run_all_checks() -> list[Finding]:
    """Run all registered checks in parallel, collect findings."""
    register_checks()

    start = time.monotonic()
    results = await asyncio.gather(
        *[_run_single(check) for check in _checks],
        return_exceptions=True,
    )

    findings: list[Finding] = []
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            logger.error("Check '%s' failed: %s", _checks[i].name, result)
        elif isinstance(result, list):
            findings.extend(result)

    elapsed = time.monotonic() - start
    logger.info("All checks completed in %.2fs, %d findings", elapsed, len(findings))
    return findings


async def _run_single(check) -> list[Finding]:
    """Run a single check with timeout."""
    try:
        return await asyncio.wait_for(check.run(), timeout=60)
    except asyncio.TimeoutError:
        logger.error("Check '%s' timed out after 60s", check.name)
        return []
