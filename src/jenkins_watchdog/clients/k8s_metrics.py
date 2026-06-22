"""Kubernetes metrics API client (metrics-server)."""

import logging
from dataclasses import dataclass

from kubernetes.client.exceptions import ApiException

from jenkins_watchdog.clients.k8s import get_core_v1, get_custom, run_sync

logger = logging.getLogger(__name__)

_METRICS_GROUP = "metrics.k8s.io"
_METRICS_VERSION = "v1beta1"


@dataclass
class ContainerUsage:
    name: str
    cpu_cores: float
    memory_bytes: int


@dataclass
class PodMetrics:
    namespace: str
    name: str
    containers: list[ContainerUsage]


@dataclass
class NodeMetrics:
    name: str
    cpu_cores: float
    memory_bytes: int


class MetricsUnavailableError(Exception):
    """Raised when metrics-server is not installed or reachable."""


def parse_cpu_quantity(value: str | None) -> float:
    """Parse a Kubernetes CPU quantity to cores."""
    if not value:
        return 0.0
    if value.endswith("n"):
        return int(value[:-1]) / 1e9
    if value.endswith("u"):
        return int(value[:-1]) / 1e6
    if value.endswith("m"):
        return int(value[:-1]) / 1000
    return float(value)


def parse_memory_quantity(value: str | None) -> int:
    """Parse a Kubernetes memory quantity to bytes."""
    if not value:
        return 0
    binary_suffixes = {
        "Ki": 1024,
        "Mi": 1024**2,
        "Gi": 1024**3,
        "Ti": 1024**4,
        "Pi": 1024**5,
        "Ei": 1024**6,
    }
    for suffix, multiplier in binary_suffixes.items():
        if value.endswith(suffix):
            return int(float(value[: -len(suffix)]) * multiplier)
    decimal_suffixes = {
        "K": 1000,
        "M": 1000**2,
        "G": 1000**3,
        "T": 1000**4,
        "P": 1000**5,
        "E": 1000**6,
    }
    for suffix, multiplier in decimal_suffixes.items():
        if value.endswith(suffix):
            return int(float(value[: -len(suffix)]) * multiplier)
    return int(value)


def usage_pct(used: float | int, limit: float | int) -> float | None:
    """Return usage percentage, or None when limit is zero/unset."""
    if not limit:
        return None
    return (used / limit) * 100


def format_bytes(num_bytes: int) -> str:
    """Format bytes as a human-readable string."""
    if num_bytes >= 1024**3:
        return f"{num_bytes / 1024**3:.1f}Gi"
    if num_bytes >= 1024**2:
        return f"{num_bytes / 1024**2:.0f}Mi"
    if num_bytes >= 1024:
        return f"{num_bytes / 1024:.0f}Ki"
    return f"{num_bytes}B"


def format_cores(cores: float) -> str:
    """Format CPU cores for display."""
    if cores >= 1:
        return f"{cores:.2f}"
    return f"{cores * 1000:.0f}m"


def _raise_if_unavailable(exc: ApiException) -> None:
    if exc.status in (404, 503):
        raise MetricsUnavailableError(str(exc)) from exc
    raise exc


def _parse_container_usage(item: dict) -> ContainerUsage:
    usage = item.get("usage", {})
    return ContainerUsage(
        name=item.get("name", ""),
        cpu_cores=parse_cpu_quantity(usage.get("cpu")),
        memory_bytes=parse_memory_quantity(usage.get("memory")),
    )


async def metrics_api_available() -> bool:
    """Return True when metrics-server responds to the metrics API."""
    try:
        await list_node_metrics()
        return True
    except MetricsUnavailableError:
        return False
    except Exception as exc:
        logger.warning("Metrics API probe failed: %s", exc)
        return False


async def list_pod_metrics(namespace: str) -> list[PodMetrics]:
    """List pod resource usage in a namespace via metrics-server."""
    custom = get_custom()
    try:
        result = await run_sync(
            custom.list_namespaced_custom_object,
            _METRICS_GROUP,
            _METRICS_VERSION,
            namespace,
            "pods",
            timeout_seconds=15,
        )
    except ApiException as exc:
        _raise_if_unavailable(exc)

    pods: list[PodMetrics] = []
    for item in result.get("items", []):
        metadata = item.get("metadata", {})
        pods.append(
            PodMetrics(
                namespace=metadata.get("namespace", namespace),
                name=metadata.get("name", ""),
                containers=[_parse_container_usage(c) for c in item.get("containers", [])],
            )
        )
    return pods


async def list_node_metrics() -> list[NodeMetrics]:
    """List node resource usage via metrics-server."""
    custom = get_custom()
    try:
        result = await run_sync(
            custom.list_cluster_custom_object,
            _METRICS_GROUP,
            _METRICS_VERSION,
            "nodes",
            timeout_seconds=15,
        )
    except ApiException as exc:
        _raise_if_unavailable(exc)

    nodes: list[NodeMetrics] = []
    for item in result.get("items", []):
        metadata = item.get("metadata", {})
        usage = item.get("usage", {})
        nodes.append(
            NodeMetrics(
                name=metadata.get("name", ""),
                cpu_cores=parse_cpu_quantity(usage.get("cpu")),
                memory_bytes=parse_memory_quantity(usage.get("memory")),
            )
        )
    return nodes


async def get_node_allocatable() -> dict[str, dict[str, float | int]]:
    """Return allocatable CPU (cores) and memory (bytes) per node."""
    v1 = get_core_v1()
    nodes = await run_sync(v1.list_node, timeout_seconds=15)
    allocatable: dict[str, dict[str, float | int]] = {}
    for node in nodes.items:
        raw = node.status.allocatable or {}
        allocatable[node.metadata.name] = {
            "cpu_cores": parse_cpu_quantity(raw.get("cpu")),
            "memory_bytes": parse_memory_quantity(raw.get("memory")),
        }
    return allocatable
