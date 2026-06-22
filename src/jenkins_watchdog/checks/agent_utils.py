"""Shared helpers for detecting Jenkins agent pods and grouping findings."""

import copy
import re

from jenkins_watchdog.checks.base import Finding
from jenkins_watchdog.clients.k8s import get_core_v1, run_sync
from jenkins_watchdog.config import settings

_JENKINS_NAME_KEYWORDS = (
    "jenkins-agent",
    "jnlp-agent",
    "jenkins-slave",
    "jenkins-worker",
)

_JENKINS_LABEL_MATCHES = (
    ("jenkins", "agent"),
    ("app", "jenkins-agent"),
    ("jenkins-slave", "true"),
    ("jenkins-agent", "true"),
)

_K8S_POD_SUFFIX = re.compile(r"-[a-z0-9]{5,10}-[a-z0-9]{4,5}$")
_STATEFULSET_SUFFIX = re.compile(r"-\d+$")
_DYNAMIC_VALUES = re.compile(r"\b\d+(\.\d+)?(%|s|h|ms|gb|mb|kb)?\b", re.IGNORECASE)

SEVERITY_WEIGHT = {"critical": 50, "warning": 20, "low": 5}


def is_jenkins_agent_pod(name: str, labels: dict | None = None) -> bool:
    """Detect Jenkins agent pods by name pattern or known labels."""
    name_lower = (name or "").lower()
    if any(kw in name_lower for kw in _JENKINS_NAME_KEYWORDS):
        return True

    labels = labels or {}
    if "jenkins/label" in labels:
        return True
    if labels.get("kubernetes.jenkins.io/controller"):
        return True

    for key, val in _JENKINS_LABEL_MATCHES:
        if labels.get(key) == val:
            return True

    return labels.get("app") == "jenkins" and labels.get("component") == "agent"


def extract_agent_prefix(name: str, labels: dict | None = None) -> str:
    """Extract the agent pool prefix from a pod or Jenkins node name."""
    labels = labels or {}
    if label := labels.get("jenkins/label"):
        return label.lower()

    base = (name or "").lower()
    base = _K8S_POD_SUFFIX.sub("", base)
    base = _STATEFULSET_SUFFIX.sub("", base)
    return base


def extract_prefix_from_resource(resource: str) -> str:
    """Extract agent prefix from a finding resource like 'ns/pod-name' or 'jenkins-agent/name'."""
    parts = resource.split("/", 1)
    if len(parts) < 2:
        return resource.lower()
    return extract_agent_prefix(parts[1])


def symptom_class(symptom: str) -> str:
    """Normalize symptom text so equivalent issues group together."""
    base = symptom.split("(")[0].split(",")[0].strip()
    normalized = _DYNAMIC_VALUES.sub("N", base).lower()
    if "temporarily offline" in normalized:
        return "agent_temporarily_offline"
    if "offline" in normalized:
        return "agent_offline"
    if "oomkilled" in normalized:
        return "oomkilled"
    if "crashloopbackoff" in normalized:
        return "crashloopbackoff"
    if "imagepullbackoff" in normalized:
        return "imagepullbackoff"
    if "restart" in normalized:
        return "high_restarts"
    if "exited with code" in normalized:
        return "container_exit"
    if "error" in normalized and "log" in normalized:
        return "log_errors"
    return normalized


def _group_key(finding: Finding) -> tuple[str, str, str]:
    return (
        extract_prefix_from_resource(finding.resource),
        symptom_class(finding.symptom),
        finding.severity,
    )


def _merge_group(prefix: str, group: list[Finding]) -> Finding:
    primary = max(group, key=lambda f: SEVERITY_WEIGHT.get(f.severity, 0))
    merged = copy.deepcopy(primary)
    count = len(group)
    namespace = primary.resource.split("/", 1)[0]
    merged.resource = f"{namespace}/{prefix}"
    merged.context = dict(primary.context)
    merged.context["grouped"] = True
    merged.context["group_size"] = count
    merged.context["affected_agents"] = [f.resource for f in group]
    merged.context["sample_agents"] = [f.resource for f in group[:5]]

    base_symptom = primary.symptom.split("(")[0].strip()
    if count == 1:
        merged.symptom = base_symptom
    elif "offline" in base_symptom.lower():
        reason = primary.context.get("offline_reason", "")
        reason_suffix = f": {reason}" if reason else ""
        merged.symptom = f"{count} {prefix} agents offline{reason_suffix}"
    else:
        merged.symptom = f"{count} {prefix} agents: {base_symptom}"

    return merged


def group_agent_findings(findings: list[Finding], min_group_size: int = 2) -> list[Finding]:
    """Merge jenkins_agent findings that share a prefix and the same issue type."""
    agent_findings: list[Finding] = []
    other_findings: list[Finding] = []

    for finding in findings:
        if finding.category == "jenkins_agent" and not finding.context.get("grouped"):
            agent_findings.append(finding)
        else:
            other_findings.append(finding)

    groups: dict[tuple[str, str, str], list[Finding]] = {}
    for finding in agent_findings:
        groups.setdefault(_group_key(finding), []).append(finding)

    merged_agents: list[Finding] = []
    for (_prefix, _symptom, _severity), group in groups.items():
        if len(group) < min_group_size:
            merged_agents.extend(group)
        else:
            prefix = extract_prefix_from_resource(group[0].resource)
            merged_agents.append(_merge_group(prefix, group))

    return other_findings + merged_agents


async def list_jenkins_agent_pods() -> list:
    """List Jenkins agent pods, preferring namespace + label selector."""
    v1 = get_core_v1()
    namespace = settings.jenkins_namespace

    try:
        result = await run_sync(
            v1.list_namespaced_pod,
            namespace=namespace,
            label_selector="jenkins/label",
            timeout_seconds=15,
        )
        return list(result.items)
    except Exception:
        pods = await run_sync(v1.list_pod_for_all_namespaces, timeout_seconds=30)
        return [
            pod
            for pod in pods.items
            if is_jenkins_agent_pod(pod.metadata.name, pod.metadata.labels or {})
        ]
