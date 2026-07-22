"""Certificate health checks via cert-manager CRDs."""

import logging
from datetime import datetime, timedelta, timezone

from jenkins_watchdog.checks.base import Finding
from jenkins_watchdog.clients.k8s import get_custom, run_sync

logger = logging.getLogger(__name__)

EXPIRY_WARNING_DAYS = 14


class CertCheck:
    name = "certs"

    async def run(self) -> list[Finding]:
        findings: list[Finding] = []
        custom = get_custom()

        try:
            certs = await run_sync(
                custom.list_cluster_custom_object,
                group="cert-manager.io",
                version="v1",
                plural="certificates",
            )
        except Exception as e:
            logger.debug("cert-manager CRD not available: %s", e)
            return findings

        now = datetime.now(timezone.utc)
        warn_threshold = now + timedelta(days=EXPIRY_WARNING_DAYS)

        for cert in certs.get("items", []):
            ns = cert["metadata"]["namespace"]
            name = cert["metadata"]["name"]
            resource = f"{ns}/cert/{name}"
            status = cert.get("status", {})

            conditions = status.get("conditions", [])
            for cond in conditions:
                if cond.get("type") == "Ready" and cond.get("status") != "True":
                    findings.append(
                        Finding(
                            severity="warning",
                            category="cert",
                            resource=resource,
                            symptom=f"Certificate not ready: {cond.get('reason', 'Unknown')}",
                            context={"message": cond.get("message", "")},
                        )
                    )

            not_after = status.get("notAfter")
            if not_after:
                try:
                    expiry = datetime.fromisoformat(not_after.replace("Z", "+00:00"))
                    if expiry < now:
                        days_ago = (now - expiry).days
                        findings.append(
                            Finding(
                                severity="critical",
                                category="cert",
                                resource=resource,
                                symptom=f"Certificate EXPIRED {days_ago} day(s) ago",
                                context={"not_after": not_after, "days_expired": days_ago},
                            )
                        )
                    elif expiry < warn_threshold:
                        days_left = (expiry - now).days
                        findings.append(
                            Finding(
                                severity="critical" if days_left < 3 else "warning",
                                category="cert",
                                resource=resource,
                                symptom=f"Certificate expires in {days_left} day(s)",
                                context={"not_after": not_after, "days_left": days_left},
                            )
                        )
                except ValueError:
                    pass

        return findings
