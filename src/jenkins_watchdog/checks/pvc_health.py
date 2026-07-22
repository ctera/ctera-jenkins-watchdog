"""PersistentVolumeClaim health checks across all namespaces."""

import logging

from jenkins_watchdog.checks.base import Finding
from jenkins_watchdog.clients.k8s import get_core_v1, run_sync

logger = logging.getLogger(__name__)


class PVCHealthCheck:
    name = "pvc_health"

    async def run(self) -> list[Finding]:
        findings: list[Finding] = []
        v1 = get_core_v1()

        pvcs = await run_sync(v1.list_persistent_volume_claim_for_all_namespaces, timeout_seconds=20)
        pv_cache: dict[str, object | None] = {}

        for pvc in pvcs.items:
            ns = pvc.metadata.namespace
            name = pvc.metadata.name
            resource = f"{ns}/{name}"
            phase = pvc.status.phase

            if phase == "Pending":
                findings.append(
                    Finding(
                        severity="warning",
                        category="pvc",
                        resource=resource,
                        symptom="PVC stuck in Pending state",
                        context={"phase": phase},
                    )
                )
            elif phase == "Lost":
                findings.append(
                    Finding(
                        severity="critical",
                        category="pvc",
                        resource=resource,
                        symptom="PVC in Lost state — data may be unavailable",
                        context={"phase": phase},
                    )
                )
            elif phase == "Bound":
                volume_name = pvc.spec.volume_name
                if not volume_name:
                    continue
                if volume_name not in pv_cache:
                    try:
                        pv = await run_sync(v1.read_persistent_volume, volume_name, timeout_seconds=10)
                        pv_cache[volume_name] = pv
                    except Exception as e:
                        logger.debug("Failed to read PV %s: %s", volume_name, e)
                        pv_cache[volume_name] = None

                pv = pv_cache.get(volume_name)
                if pv and pv.status and pv.status.phase != "Bound":
                    findings.append(
                        Finding(
                            severity="warning",
                            category="pvc",
                            resource=resource,
                            symptom="PVC bound to unhealthy PV",
                            context={
                                "pvc_phase": phase,
                                "pv_name": volume_name,
                                "pv_phase": pv.status.phase,
                            },
                        )
                    )

        return findings
