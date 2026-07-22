"""Jenkins agent offline checks."""

import logging

from jenkins_watchdog.checks.base import Finding
from jenkins_watchdog.clients.jenkins import get_nodes

logger = logging.getLogger(__name__)


class AgentOfflineCheck:
    name = "agent_offline"

    async def run(self) -> list[Finding]:
        findings: list[Finding] = []

        try:
            nodes = await get_nodes()
        except Exception as e:
            logger.warning("Failed to check offline agents: %s", e)
            return findings

        for node in nodes:
            if not node.get("offline", False):
                continue

            name = node.get("displayName", "unknown")
            reason = node.get("offlineCauseReason") or "no reason given"
            temporarily_offline = node.get("temporarilyOffline", False)
            severity = "warning" if temporarily_offline else "critical"

            findings.append(
                Finding(
                    severity=severity,
                    category="jenkins_agent",
                    resource=f"jenkins-agent/{name}",
                    symptom=f"Agent offline: {reason}",
                    context={
                        "offline_reason": reason,
                        "temporarily_offline": temporarily_offline,
                    },
                )
            )

        return findings
