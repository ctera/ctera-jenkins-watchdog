"""Kubernetes workload health checks — deployments and statefulsets across all namespaces."""

import logging

from jenkins_watchdog.checks.base import Finding
from jenkins_watchdog.clients.k8s import get_apps_v1, run_sync

logger = logging.getLogger(__name__)


class WorkloadCheck:
    name = "k8s_workloads"

    async def run(self) -> list[Finding]:
        findings: list[Finding] = []
        apps = get_apps_v1()

        deployments = await run_sync(apps.list_deployment_for_all_namespaces, timeout_seconds=20)
        for dep in deployments.items:
            ns = dep.metadata.namespace
            name = dep.metadata.name

            desired = dep.spec.replicas or 0
            available = dep.status.available_replicas or 0
            unavailable = dep.status.unavailable_replicas or 0

            if unavailable > 0:
                findings.append(
                    Finding(
                        severity="critical" if available == 0 else "warning",
                        category="k8s_workload",
                        resource=f"{ns}/{name}",
                        symptom=f"Deployment has {unavailable} unavailable replica(s) ({available}/{desired} ready)",
                        context={
                            "desired": desired,
                            "available": available,
                            "unavailable": unavailable,
                        },
                    )
                )

            if dep.status.conditions:
                for cond in dep.status.conditions:
                    if cond.type == "Progressing" and cond.status == "False":
                        findings.append(
                            Finding(
                                severity="warning",
                                category="k8s_workload",
                                resource=f"{ns}/{name}",
                                symptom=f"Deployment rollout stuck: {cond.message or cond.reason or ''}",
                                context={"reason": cond.reason or ""},
                            )
                        )

        statefulsets = await run_sync(apps.list_stateful_set_for_all_namespaces, timeout_seconds=20)
        for sts in statefulsets.items:
            ns = sts.metadata.namespace
            name = sts.metadata.name

            desired = sts.spec.replicas or 0
            ready = sts.status.ready_replicas or 0

            if ready < desired:
                findings.append(
                    Finding(
                        severity="critical" if ready == 0 else "warning",
                        category="k8s_workload",
                        resource=f"{ns}/{name}",
                        symptom=f"StatefulSet has {ready}/{desired} ready replicas",
                        context={"desired": desired, "ready": ready},
                    )
                )

        return findings
