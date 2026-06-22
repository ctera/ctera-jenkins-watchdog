"""Shared helpers for detecting Jenkins agent pods."""

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
