"""Cluster and Jenkins context gathering — injected into investigation system prompt."""

import logging

from jenkins_watchdog.checks.agent_utils import list_jenkins_agent_pods
from jenkins_watchdog.clients.jenkins import get_queue_info, get_recent_failed_builds, get_running_builds
from jenkins_watchdog.clients.k8s import get_core_v1, run_sync
from jenkins_watchdog.config import settings

logger = logging.getLogger(__name__)


async def gather_cluster_context() -> str:
    """Gather a lightweight cluster + Jenkins snapshot for the investigation system prompt."""
    sections: list[str] = []

    try:
        v1 = get_core_v1()

        nodes = await run_sync(v1.list_node, timeout_seconds=10)
        node_info = []
        for node in nodes.items:
            conditions = {c.type: c.status for c in (node.status.conditions or [])}
            alloc = node.status.allocatable or {}
            node_info.append(
                f"  - {node.metadata.name}: Ready={conditions.get('Ready', '?')} "
                f"cpu={alloc.get('cpu', '?')} mem={alloc.get('memory', '?')}"
            )

        agent_pods = await list_jenkins_agent_pods()
        agent_count = len(agent_pods)
        agent_by_phase: dict[str, int] = {}
        for pod in agent_pods:
            phase = pod.status.phase or "Unknown"
            agent_by_phase[phase] = agent_by_phase.get(phase, 0) + 1

        sections.append(
            f"## k3s Cluster Snapshot\n"
            f"- Nodes: {len(nodes.items)}\n"
            + "\n".join(node_info) + "\n"
            f"- Jenkins agent pods: {agent_count} ({', '.join(f'{k}={v}' for k, v in sorted(agent_by_phase.items()))})\n"
        )
    except Exception as e:
        logger.warning("Failed to gather cluster context: %s", e)

    try:
        queue = await get_queue_info()
        running = await get_running_builds()
        failed = await get_recent_failed_builds(window_hours=settings.jenkins_failed_build_window_hours)

        mr_failures = sum(1 for b in failed if b.is_mr)
        sections.append(
            f"## Jenkins Snapshot\n"
            f"- Build queue: {len(queue)} items\n"
            f"- Running builds: {len(running)}\n"
            f"- Failed builds (last {settings.jenkins_failed_build_window_hours}h): {len(failed)} "
            f"({mr_failures} MR/PR)\n"
        )
        if failed:
            top_jobs = {}
            for b in failed[:10]:
                top_jobs[b.job_name] = top_jobs.get(b.job_name, 0) + 1
            job_lines = [f"  - {name}: {count} failure(s)" for name, count in sorted(top_jobs.items(), key=lambda x: -x[1])[:5]]
            sections.append("Top failing jobs:\n" + "\n".join(job_lines) + "\n")
    except Exception as e:
        logger.warning("Failed to gather Jenkins context: %s", e)

    if sections:
        sections.append("Use this context to avoid redundant queries and to correlate infrastructure vs pipeline issues.\n")
    return "\n".join(sections)
