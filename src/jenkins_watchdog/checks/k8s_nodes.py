"""Kubernetes worker node health checks for k3s cluster."""

import logging

from jenkins_watchdog.checks.base import Finding
from jenkins_watchdog.clients.k8s import get_core_v1, run_sync

logger = logging.getLogger(__name__)


class NodeCheck:
    name = "k8s_nodes"

    async def run(self) -> list[Finding]:
        findings: list[Finding] = []
        v1 = get_core_v1()
        nodes = await run_sync(v1.list_node, timeout_seconds=15)

        for node in nodes.items:
            name = node.metadata.name
            conditions = {c.type: c for c in (node.status.conditions or [])}

            ready_cond = conditions.get("Ready")
            if ready_cond and ready_cond.status != "True":
                findings.append(
                    Finding(
                        severity="critical",
                        category="k8s_node",
                        resource=f"node/{name}",
                        symptom=f"Node NotReady: {ready_cond.message or ready_cond.reason or 'unknown'}",
                        context={
                            "reason": ready_cond.reason or "",
                            "message": ready_cond.message or "",
                        },
                    )
                )

            for cond_type in ("MemoryPressure", "DiskPressure", "PIDPressure"):
                cond = conditions.get(cond_type)
                if cond and cond.status == "True":
                    findings.append(
                        Finding(
                            severity="critical" if cond_type == "MemoryPressure" else "warning",
                            category="k8s_node",
                            resource=f"node/{name}",
                            symptom=f"{cond_type}: {cond.message or cond.reason or ''}",
                            context={"reason": cond.reason or ""},
                        )
                    )

        return findings
