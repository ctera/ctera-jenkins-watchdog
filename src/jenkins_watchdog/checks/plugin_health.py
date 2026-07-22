"""Jenkins plugin health checks."""

import logging

from jenkins_watchdog.checks.base import Finding
from jenkins_watchdog.clients.jenkins import get_jenkins_http_client

logger = logging.getLogger(__name__)

_PLUGIN_TREE = "plugins[shortName,longName,active,enabled,hasUpdate,version]"
_UPDATE_THRESHOLD = 20


class PluginHealthCheck:
    name = "plugin_health"

    async def run(self) -> list[Finding]:
        findings: list[Finding] = []

        try:
            client = get_jenkins_http_client()
            resp = await client.get(
                "/pluginManager/api/json",
                params={"tree": _PLUGIN_TREE, "depth": 1},
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("Failed to check plugin health: %s", e)
            return findings

        plugins = data.get("plugins", [])
        update_count = 0

        for plugin in plugins:
            name = plugin.get("longName") or plugin.get("shortName") or "unknown"
            short_name = plugin.get("shortName") or name
            active = plugin.get("active", True)
            enabled = plugin.get("enabled", True)

            if not active and enabled:
                findings.append(
                    Finding(
                        severity="warning",
                        category="jenkins_plugin",
                        resource=f"jenkins-plugin/{short_name}",
                        symptom=f"Plugin {name} enabled but not active (failed to load)",
                        context={
                            "short_name": short_name,
                            "version": plugin.get("version"),
                        },
                    )
                )

            if plugin.get("hasUpdate", False):
                update_count += 1

        if update_count > _UPDATE_THRESHOLD:
            findings.append(
                Finding(
                    severity="low",
                    category="jenkins_plugin",
                    resource="jenkins-plugins",
                    symptom="20+ plugins have pending updates",
                    context={"pending_updates": update_count},
                )
            )

        return findings
