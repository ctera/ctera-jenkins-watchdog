"""Jenkins credential health checks."""

import logging

from jenkins_watchdog.checks.base import Finding
from jenkins_watchdog.clients.jenkins import get_jenkins_http_client

logger = logging.getLogger(__name__)

_CREDENTIAL_TREE = "stores[*[domains[*[credentials[id,typeName,displayName,description]]]]]"
_EXPIRY_HINTS = ("expire", "expir", "renew", "temp", "temporary")


class CredentialHealthCheck:
    name = "credential_health"

    async def run(self) -> list[Finding]:
        findings: list[Finding] = []

        try:
            client = get_jenkins_http_client()
            resp = await client.get(
                "/manage/credentials/api/json",
                params={"tree": _CREDENTIAL_TREE, "depth": 5},
            )
            if resp.status_code in (403, 404):
                logger.debug("Credentials API not accessible: HTTP %s", resp.status_code)
                return findings
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("Failed to check credential health: %s", e)
            return findings

        credentials = _iter_credentials(data.get("stores", {}))
        total_count = len(credentials)

        for cred in credentials:
            cred_id = cred.get("id") or "unknown"
            display_name = cred.get("displayName") or ""
            description = cred.get("description") or ""
            combined = f"{display_name} {description}".lower()
            resource = f"jenkins-credential/{cred_id}"
            context = {
                "display_name": display_name,
                "type": cred.get("typeName"),
                "total_credentials": total_count,
            }

            if "expired" in description.lower():
                findings.append(
                    Finding(
                        severity="warning",
                        category="jenkins_credential",
                        resource=resource,
                        symptom=f"Credential may be expired: {display_name or cred_id}",
                        context=context,
                    )
                )
                continue

            if any(hint in combined for hint in _EXPIRY_HINTS):
                findings.append(
                    Finding(
                        severity="low",
                        category="jenkins_credential",
                        resource=resource,
                        symptom=f"Credential may need renewal: {display_name or cred_id}",
                        context=context,
                    )
                )

        return findings


def _iter_credentials(stores) -> list[dict]:
    result: list[dict] = []
    if isinstance(stores, dict):
        store_items = stores.values()
    elif isinstance(stores, list):
        store_items = stores
    else:
        return result

    for store in store_items:
        if not isinstance(store, dict):
            continue
        domains = store.get("domains", {})
        if isinstance(domains, dict):
            domain_items = domains.values()
        elif isinstance(domains, list):
            domain_items = domains
        else:
            continue

        for domain in domain_items:
            if not isinstance(domain, dict):
                continue
            creds = domain.get("credentials", [])
            if isinstance(creds, dict):
                result.extend(creds.values())
            elif isinstance(creds, list):
                result.extend(c for c in creds if isinstance(c, dict))

    return result
