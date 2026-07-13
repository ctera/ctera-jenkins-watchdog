"""Adapter from legacy detectors to durable v2 ``CheckResult`` values."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from jenkins_watchdog.checks.agent_connectivity import AgentConnectivityCheck
from jenkins_watchdog.checks.agent_errors import AgentErrorCheck
from jenkins_watchdog.checks.agent_pods import AgentPodCheck
from jenkins_watchdog.checks.agent_resources import AgentResourceCheck
from jenkins_watchdog.checks.agent_utils import extract_prefix_from_resource, symptom_class
from jenkins_watchdog.checks.base import BaseCheck, Finding
from jenkins_watchdog.checks.jenkins_failed_builds import JenkinsFailedBuildCheck
from jenkins_watchdog.checks.jenkins_jobs import JenkinsJobCheck
from jenkins_watchdog.checks.jenkins_pipeline_patterns import JenkinsPipelinePatternCheck
from jenkins_watchdog.checks.k8s_events import K8sEventsCheck
from jenkins_watchdog.checks.k8s_nodes import NodeCheck
from jenkins_watchdog.checks.k8s_workloads import WorkloadCheck
from jenkins_watchdog.clients.jenkins import JenkinsClient
from jenkins_watchdog.clients.k8s import KubernetesClient
from jenkins_watchdog.clients.k8s_metrics import KubernetesMetricsClient
from jenkins_watchdog.domain.model import CheckResult, CheckStatus, FindingObservation, ScanMode, Severity
from jenkins_watchdog.scan_options import ScanOptions, activate_scan_options, reset_scan_options

CHECK_CATEGORIES: dict[str, frozenset[str]] = {
    "jenkins_agent_pods": frozenset({"jenkins_agent"}),
    "jenkins_agent_resources": frozenset({"jenkins_agent"}),
    "jenkins_agent_errors": frozenset({"jenkins_agent"}),
    "jenkins_agent_connectivity": frozenset({"jenkins_agent", "jenkins_controller"}),
    "jenkins_jobs": frozenset({"jenkins_queue", "jenkins_build"}),
    "jenkins_failed_builds": frozenset({"jenkins_failed_build"}),
    "jenkins_pipeline_patterns": frozenset({"jenkins_pipeline_pattern"}),
    "k8s_nodes": frozenset({"k8s_node"}),
    "k8s_workloads": frozenset({"k8s_workload"}),
    "k8s_events": frozenset({"k8s_event"}),
}


def default_checks(
    *,
    jenkins: JenkinsClient,
    kubernetes: KubernetesClient,
    metrics: KubernetesMetricsClient,
    jenkins_namespace: str,
    request_timeout_seconds: float,
    k8s_events_window_minutes: int,
) -> tuple[BaseCheck, ...]:
    return (
        AgentPodCheck(kubernetes, namespace=jenkins_namespace),
        AgentResourceCheck(kubernetes, metrics, namespace=jenkins_namespace),
        AgentErrorCheck(
            kubernetes,
            namespace=jenkins_namespace,
            request_timeout_seconds=request_timeout_seconds,
        ),
        AgentConnectivityCheck(jenkins),
        JenkinsJobCheck(jenkins),
        JenkinsFailedBuildCheck(jenkins),
        JenkinsPipelinePatternCheck(jenkins),
        NodeCheck(kubernetes, metrics),
        WorkloadCheck(kubernetes),
        K8sEventsCheck(
            kubernetes,
            jenkins_namespace=jenkins_namespace,
            window_minutes=k8s_events_window_minutes,
        ),
    )


class LegacyCheckRunner:
    def __init__(
        self,
        checks: tuple[BaseCheck, ...],
        *,
        timeout_seconds: float,
        regular_options: ScanOptions | None = None,
    ) -> None:
        self._checks = {check.name: check for check in checks}
        self._timeout_seconds = timeout_seconds
        self._regular_options = regular_options or ScanOptions()
        unknown = set(self._checks) - set(CHECK_CATEGORIES)
        if unknown:
            raise ValueError(f"missing category ownership for checks: {sorted(unknown)}")

    @property
    def check_names(self) -> tuple[str, ...]:
        return tuple(self._checks)

    def checks_for_categories(self, categories: tuple[str, ...]) -> tuple[str, ...]:
        if not categories:
            return self.check_names
        selected = set(categories)
        return tuple(name for name in self.check_names if CHECK_CATEGORIES[name] & selected)

    async def run(self, scan_id: str, check_name: str, mode: ScanMode) -> CheckResult:
        check = self._checks[check_name]
        started_at = datetime.now(timezone.utc)
        options = ScanOptions.deep_scan() if mode is ScanMode.DEEP else self._regular_options
        token = activate_scan_options(options)
        try:
            findings = await asyncio.wait_for(check.run(), timeout=self._timeout_seconds)
        except TimeoutError:
            completed_at = datetime.now(timezone.utc)
            return CheckResult(
                scan_id=scan_id,
                check_name=check_name,
                status=CheckStatus.TIMED_OUT,
                failure_summary=f"timed out after {self._timeout_seconds:.0f}s",
                categories=CHECK_CATEGORIES[check_name],
                started_at=started_at,
                completed_at=completed_at,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            completed_at = datetime.now(timezone.utc)
            return CheckResult(
                scan_id=scan_id,
                check_name=check_name,
                status=CheckStatus.FAILED,
                failure_summary=_failure_summary(exc),
                categories=CHECK_CATEGORIES[check_name],
                started_at=started_at,
                completed_at=completed_at,
            )
        finally:
            reset_scan_options(token)

        completed_at = datetime.now(timezone.utc)
        observations = tuple(
            _to_observation(scan_id=scan_id, check_name=check_name, finding=finding, observed_at=completed_at)
            for finding in findings
        )
        return CheckResult(
            scan_id=scan_id,
            check_name=check_name,
            status=CheckStatus.SUCCEEDED,
            findings=observations,
            categories=CHECK_CATEGORIES[check_name],
            started_at=started_at,
            completed_at=completed_at,
        )


def _to_observation(*, scan_id: str, check_name: str, finding: Finding, observed_at: datetime) -> FindingObservation:
    dimensions: dict[str, Any] = {"symptom_family": symptom_class(finding.symptom)}
    for name in ("error_signature", "pattern", "reason", "failure_class"):
        value = finding.context.get(name)
        if value not in (None, "", "unknown"):
            dimensions[name] = value
    scm = finding.context.get("scm")
    scm_metadata = scm if isinstance(scm, dict) else finding.context
    for target, candidates in {
        "scm_provider": ("scm_provider", "provider"),
        "scm_repository": ("scm_repository", "repository"),
        "change_number": ("change_number", "mr_number", "pr_number"),
    }.items():
        value = next((scm_metadata.get(name) for name in candidates if scm_metadata.get(name) not in (None, "")), None)
        if value is not None:
            dimensions[target] = value
    node = finding.context.get("built_on") or finding.context.get("node")
    source = finding.context.get("source")
    if not node and isinstance(source, dict):
        node = source.get("host")
    if not node and finding.category == "k8s_node":
        node = finding.resource.rsplit("/", 1)[-1]
    if node:
        dimensions["node"] = node
    if finding.category == "jenkins_agent":
        dimensions["agent_pool"] = extract_prefix_from_resource(finding.resource)
    return FindingObservation(
        scan_id=scan_id,
        check_name=check_name,
        rule_id=f"{check_name}.{finding.category}.v1",
        resource_id=finding.resource,
        severity=Severity(finding.severity),
        category=finding.category,
        summary=finding.symptom,
        observed_at=observed_at,
        identity_dimensions=dimensions,
        evidence=finding.context,
    )


def _failure_summary(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:500]
