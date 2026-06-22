"""Kubernetes tools for Claude to investigate cluster state."""

import json
import logging
from datetime import datetime, timezone

from jenkins_watchdog.clients.k8s import get_apps_v1, get_batch_v1, get_core_v1, run_sync

logger = logging.getLogger(__name__)

MAX_OUTPUT_BYTES = 4096

_NOISY_ANNOTATION_PREFIXES = (
    "kubectl.kubernetes.io/",
    "meta.helm.sh/",
    "field.cattle.io/",
)


def _truncate(text: str) -> str:
    if len(text) > MAX_OUTPUT_BYTES:
        return text[:MAX_OUTPUT_BYTES] + "\n... [truncated]"
    return text


def _strip_noise(data: dict) -> dict:
    """Remove verbose K8s metadata that pollutes Claude's context window."""
    if "metadata" in data:
        data["metadata"].pop("managedFields", None)
        annotations = data["metadata"].get("annotations", {})
        for key in list(annotations.keys()):
            if any(key.startswith(p) for p in _NOISY_ANNOTATION_PREFIXES):
                del annotations[key]
            elif "last-applied" in key:
                del annotations[key]

    if "status" in data and isinstance(data["status"], dict):
        data["status"].pop("conditions", None)

    return data


async def get_resource(kind: str, name: str, namespace: str = "default") -> str:
    """Get full spec of a K8s resource."""
    try:
        kind_lower = kind.lower()
        if kind_lower == "pod":
            obj = await run_sync(get_core_v1().read_namespaced_pod, name, namespace)
        elif kind_lower == "deployment":
            obj = await run_sync(get_apps_v1().read_namespaced_deployment, name, namespace)
        elif kind_lower == "statefulset":
            obj = await run_sync(get_apps_v1().read_namespaced_stateful_set, name, namespace)
        elif kind_lower == "daemonset":
            obj = await run_sync(get_apps_v1().read_namespaced_daemon_set, name, namespace)
        elif kind_lower == "service":
            obj = await run_sync(get_core_v1().read_namespaced_service, name, namespace)
        elif kind_lower == "configmap":
            obj = await run_sync(get_core_v1().read_namespaced_config_map, name, namespace)
        elif kind_lower == "job":
            obj = await run_sync(get_batch_v1().read_namespaced_job, name, namespace)
        elif kind_lower == "node":
            obj = await run_sync(get_core_v1().read_node, name)
        else:
            return f"Unsupported kind: {kind}. Supported: Pod, Deployment, StatefulSet, DaemonSet, Service, ConfigMap, Job, Node"

        from kubernetes.client import ApiClient

        data = ApiClient().sanitize_for_serialization(obj)
        data = _strip_noise(data)

        return _truncate(json.dumps(data, indent=2, default=str))
    except Exception as e:
        return f"Error getting {kind}/{name} in {namespace}: {e}"


async def list_resources(kind: str, namespace: str | None = None, label_selector: str = "") -> str:
    """List K8s resources. Returns name + status summary for each."""
    try:
        kind_lower = kind.lower()
        items = []

        if kind_lower == "pod":
            if namespace:
                result = await run_sync(get_core_v1().list_namespaced_pod, namespace, label_selector=label_selector)
            else:
                result = await run_sync(get_core_v1().list_pod_for_all_namespaces, label_selector=label_selector)
            for pod in result.items:
                phase = pod.status.phase if pod.status else "Unknown"
                restarts = 0
                if pod.status and pod.status.container_statuses:
                    restarts = sum(cs.restart_count for cs in pod.status.container_statuses)
                items.append(f"{pod.metadata.namespace}/{pod.metadata.name} phase={phase} restarts={restarts}")

        elif kind_lower == "deployment":
            if namespace:
                result = await run_sync(get_apps_v1().list_namespaced_deployment, namespace, label_selector=label_selector)
            else:
                result = await run_sync(get_apps_v1().list_deployment_for_all_namespaces, label_selector=label_selector)
            for dep in result.items:
                ready = dep.status.ready_replicas or 0
                desired = dep.spec.replicas or 0
                items.append(f"{dep.metadata.namespace}/{dep.metadata.name} ready={ready}/{desired}")

        elif kind_lower == "node":
            result = await run_sync(get_core_v1().list_node, label_selector=label_selector)
            for node in result.items:
                conditions = {c.type: c.status for c in (node.status.conditions or [])}
                items.append(f"{node.metadata.name} Ready={conditions.get('Ready', '?')}")

        elif kind_lower == "event":
            if namespace:
                result = await run_sync(get_core_v1().list_namespaced_event, namespace)
            else:
                result = await run_sync(get_core_v1().list_event_for_all_namespaces)
            for ev in sorted(
                result.items,
                key=lambda e: (
                    e.last_timestamp or e.metadata.creation_timestamp or datetime.min.replace(tzinfo=timezone.utc)
                ),
                reverse=True,
            )[:30]:
                ts = ev.last_timestamp or ev.metadata.creation_timestamp or ""
                items.append(f"[{ev.type}] {ev.involved_object.kind}/{ev.involved_object.name}: {ev.message} ({ts})")

        else:
            return f"Unsupported kind for list: {kind}. Supported: Pod, Deployment, Node, Event"

        return _truncate("\n".join(items) if items else f"No {kind} resources found")
    except Exception as e:
        return f"Error listing {kind}: {e}"


async def get_events(
    namespace: str | None = None,
    pod_name: str | None = None,
    node_name: str | None = None,
    event_type: str = "Warning",
    limit: int = 50,
) -> str:
    """Query Kubernetes events scoped by namespace, pod, or node."""
    try:
        field_parts: list[str] = []
        if event_type:
            field_parts.append(f"type={event_type}")
        if pod_name:
            field_parts.append(f"involvedObject.name={pod_name}")
            field_parts.append("involvedObject.kind=Pod")
        elif node_name:
            field_parts.append(f"involvedObject.name={node_name}")
            field_parts.append("involvedObject.kind=Node")

        field_selector = ",".join(field_parts) if field_parts else None
        limit = min(limit, 100)

        if namespace:
            result = await run_sync(
                get_core_v1().list_namespaced_event,
                namespace,
                field_selector=field_selector,
            )
        else:
            result = await run_sync(
                get_core_v1().list_event_for_all_namespaces,
                field_selector=field_selector,
            )

        if not result.items:
            scope = namespace or "all namespaces"
            if pod_name:
                scope = f"{namespace or '?'}/{pod_name}"
            elif node_name:
                scope = f"node/{node_name}"
            return f"No events found for {scope}"

        sorted_events = sorted(
            result.items,
            key=lambda e: (
                e.last_timestamp or e.metadata.creation_timestamp or datetime.min.replace(tzinfo=timezone.utc)
            ),
            reverse=True,
        )[:limit]

        lines = []
        for ev in sorted_events:
            ts = ev.last_timestamp or ev.metadata.creation_timestamp or ""
            obj = ev.involved_object
            obj_ref = f"{obj.namespace or ''}/{obj.kind}/{obj.name}".strip("/")
            count = ev.count or 1
            source = ""
            if ev.source and ev.source.host:
                source = f", host={ev.source.host}"
            lines.append(
                f"[{ev.type}] {obj_ref} {ev.reason}: {ev.message} (count={count}, last={ts}{source})"
            )

        return _truncate("\n".join(lines))
    except Exception as e:
        return f"Error getting events: {e}"


async def get_pod_events(namespace: str, pod_name: str) -> str:
    """Get events for a specific pod."""
    try:
        field_selector = f"involvedObject.name={pod_name},involvedObject.kind=Pod"
        result = await run_sync(get_core_v1().list_namespaced_event, namespace, field_selector=field_selector)
        if not result.items:
            return f"No events found for pod {namespace}/{pod_name}"

        lines = []
        for ev in sorted(
            result.items,
            key=lambda e: (
                e.last_timestamp or e.metadata.creation_timestamp or datetime.min.replace(tzinfo=timezone.utc)
            ),
            reverse=True,
        ):
            ts = ev.last_timestamp or ev.metadata.creation_timestamp or ""
            count = ev.count or 1
            lines.append(f"[{ev.type}] {ev.reason}: {ev.message} (count={count}, last={ts})")

        return _truncate("\n".join(lines))
    except Exception as e:
        return f"Error getting events for {namespace}/{pod_name}: {e}"


async def get_pod_logs(namespace: str, pod_name: str, container: str | None = None, tail_lines: int = 100) -> str:
    """Get recent logs from a pod container."""
    try:
        kwargs = {"name": pod_name, "namespace": namespace, "tail_lines": min(tail_lines, 200)}
        if container:
            kwargs["container"] = container

        logs = await run_sync(get_core_v1().read_namespaced_pod_log, **kwargs)
        return _truncate(logs if logs else "(empty logs)")
    except Exception as e:
        return f"Error getting logs for {namespace}/{pod_name}: {e}"


TOOL_DEFINITIONS = [
    {
        "name": "k8s_get_resource",
        "description": "Get the full spec/status of a Kubernetes resource (Pod, Deployment, StatefulSet, DaemonSet, Service, ConfigMap, Job, Node).",
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "description": "Resource kind (Pod, Deployment, StatefulSet, DaemonSet, Service, ConfigMap, Job, Node)"},
                "name": {"type": "string", "description": "Resource name"},
                "namespace": {"type": "string", "description": "Namespace (omit for cluster-scoped like Node)", "default": "default"},
            },
            "required": ["kind", "name"],
        },
    },
    {
        "name": "k8s_list_resources",
        "description": "List Kubernetes resources with status summaries. Use for discovering pods in a namespace, checking deployment health, etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "description": "Resource kind (Pod, Deployment, Node, Event)"},
                "namespace": {"type": "string", "description": "Namespace to scope to (omit for all namespaces)"},
                "label_selector": {"type": "string", "description": "Label selector (e.g. 'app=jenkins')"},
            },
            "required": ["kind"],
        },
    },
    {
        "name": "k8s_get_events",
        "description": "Query Kubernetes cluster events by namespace, pod, or node. Returns Warning events by default (Unhealthy, BackOff, FailedScheduling, etc.).",
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "description": "Namespace to scope events (omit for all namespaces)"},
                "pod_name": {"type": "string", "description": "Filter to events for a specific pod"},
                "node_name": {"type": "string", "description": "Filter to events for a specific node"},
                "event_type": {"type": "string", "description": "Event type filter (Warning or Normal)", "default": "Warning"},
                "limit": {"type": "integer", "description": "Max events to return (max 100)", "default": 50},
            },
        },
    },
    {
        "name": "k8s_get_pod_events",
        "description": "Get Kubernetes events for a specific pod. Useful for OOMKill, scheduling failures, image pull errors, etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "description": "Pod namespace"},
                "pod_name": {"type": "string", "description": "Pod name"},
            },
            "required": ["namespace", "pod_name"],
        },
    },
    {
        "name": "k8s_get_pod_logs",
        "description": "Get recent logs from a pod container. Use to find error messages, stack traces, or startup failures.",
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "description": "Pod namespace"},
                "pod_name": {"type": "string", "description": "Pod name"},
                "container": {"type": "string", "description": "Container name (omit if pod has one container)"},
                "tail_lines": {"type": "integer", "description": "Number of recent lines to return (max 200)", "default": 100},
            },
            "required": ["namespace", "pod_name"],
        },
    },
]

TOOL_HANDLERS = {
    "k8s_get_resource": lambda args: get_resource(args["kind"], args["name"], args.get("namespace", "default")),
    "k8s_list_resources": lambda args: list_resources(args["kind"], args.get("namespace"), args.get("label_selector", "")),
    "k8s_get_events": lambda args: get_events(
        args.get("namespace"),
        args.get("pod_name"),
        args.get("node_name"),
        args.get("event_type", "Warning"),
        args.get("limit", 50),
    ),
    "k8s_get_pod_events": lambda args: get_pod_events(args["namespace"], args["pod_name"]),
    "k8s_get_pod_logs": lambda args: get_pod_logs(args["namespace"], args["pod_name"], args.get("container"), args.get("tail_lines", 100)),
}
