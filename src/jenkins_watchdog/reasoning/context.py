"""Cluster context gathering — lightweight snapshot injected into system prompt."""

import logging

from jenkins_watchdog.checks.agent_utils import list_jenkins_agent_pods
from jenkins_watchdog.clients.k8s import get_core_v1, run_sync

logger = logging.getLogger(__name__)


async def gather_cluster_context() -> str:
    """Gather a lightweight cluster snapshot for the investigation system prompt.

    Called once per scan and shared across all investigations.
    """
    try:
        v1 = get_core_v1()

        nodes = await run_sync(v1.list_node, timeout_seconds=10)
        namespaces = await run_sync(v1.list_namespace, timeout_seconds=10)
        ns_names = [ns.metadata.name for ns in namespaces.items]

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

        return (
            f"## k3s Cluster Snapshot (pre-gathered)\n"
            f"- Nodes: {len(nodes.items)}\n"
            + "\n".join(node_info) + "\n"
            f"- Namespaces: {len(ns_names)} total ({', '.join(ns_names[:10])}{'...' if len(ns_names) > 10 else ''})\n"
            f"- Jenkins agent pods running: {agent_count}\n"
            f"\nUse this context to avoid querying for basic cluster info.\n"
        )
    except Exception as e:
        logger.warning("Failed to gather cluster context: %s", e)
        return ""
