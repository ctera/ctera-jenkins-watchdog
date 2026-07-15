"""Read-only operational tools available to the reasoning agent."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import quote

import httpx

from jenkins_watchdog.clients.jenkins import JenkinsClient, job_to_api_path
from jenkins_watchdog.clients.k8s import KubernetesClient
from jenkins_watchdog.clients.k8s_metrics import (
    KubernetesMetricsClient,
    format_bytes,
    format_cores,
    usage_pct,
)
from jenkins_watchdog.clients.log_analysis import classify_failure, extract_error_lines
from jenkins_watchdog.clients.prometheus import PrometheusClient
from jenkins_watchdog.domain.model import ScanMode

ToolHandler = Callable[[dict[str, Any], ScanMode], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class ToolExecution:
    name: str
    arguments: dict[str, Any]
    output: str
    ok: bool
    duration_ms: int


class ReadOnlyToolRegistry:
    """Whitelisted Jenkins, Kubernetes, Prometheus, and SCM read operations."""

    def __init__(
        self,
        *,
        jenkins: JenkinsClient,
        kubernetes: KubernetesClient,
        metrics: KubernetesMetricsClient,
        prometheus: PrometheusClient,
        http: httpx.AsyncClient,
        jenkins_namespace: str,
        github_api_url: str,
        github_token: str,
        gitlab_api_url: str,
        gitlab_token: str,
    ) -> None:
        self._jenkins = jenkins
        self._kubernetes = kubernetes
        self._metrics = metrics
        self._prometheus = prometheus
        self._http = http
        self._jenkins_namespace = jenkins_namespace
        self._github_api_url = github_api_url.rstrip("/")
        self._github_token = github_token
        self._gitlab_api_url = gitlab_api_url.rstrip("/")
        self._gitlab_token = gitlab_token
        self._handlers: dict[str, ToolHandler] = {
            "jenkins_list_agents": self._jenkins_list_agents,
            "jenkins_get_agent": self._jenkins_get_agent,
            "jenkins_get_queue": self._jenkins_get_queue,
            "jenkins_get_running_builds": self._jenkins_get_running_builds,
            "jenkins_get_job": self._jenkins_get_job,
            "jenkins_get_recent_failed_builds": self._jenkins_get_recent_failed_builds,
            "jenkins_get_build": self._jenkins_get_build,
            "jenkins_get_build_log": self._jenkins_get_build_log,
            "jenkins_get_job_build_history": self._jenkins_get_job_build_history,
            "jenkins_get_build_stages": self._jenkins_get_build_stages,
            "jenkins_get_test_report": self._jenkins_get_test_report,
            "jenkins_analyze_build_failure": self._jenkins_analyze_build_failure,
            "k8s_get_resource": self._k8s_get_resource,
            "k8s_list_resources": self._k8s_list_resources,
            "k8s_get_events": self._k8s_get_events,
            "k8s_get_pod_events": self._k8s_get_pod_events,
            "k8s_get_pod_logs": self._k8s_get_pod_logs,
            "k8s_top_pods": self._k8s_top_pods,
            "k8s_top_nodes": self._k8s_top_nodes,
            "prometheus_query": self._prometheus_query,
            "prometheus_query_range": self._prometheus_query_range,
            "scm_get_change": self._scm_get_change,
            "scm_get_change_diff": self._scm_get_change_diff,
        }
        self.definitions = tuple(_definitions())

    async def execute(self, name: str, arguments: dict[str, Any], *, mode: ScanMode) -> ToolExecution:
        started = time.monotonic()
        handler = self._handlers.get(name)
        if handler is None:
            output = json.dumps({"error": f"unknown read-only tool: {name}"})
            return ToolExecution(name, arguments, output, False, 0)
        try:
            result = await handler(arguments, mode)
            output = result if isinstance(result, str) else json.dumps(result, default=str, ensure_ascii=False)
            output = _truncate(_redact(output), mode=mode)
            ok = not output.startswith('{"error"')
        except Exception as exc:
            output = json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)
            ok = False
        return ToolExecution(
            name=name,
            arguments=arguments,
            output=output,
            ok=ok,
            duration_ms=round((time.monotonic() - started) * 1000),
        )

    async def _jenkins_list_agents(self, _: dict[str, Any], mode: ScanMode) -> Any:
        del mode
        return await self._jenkins.get_nodes()

    async def _jenkins_get_agent(self, args: dict[str, Any], mode: ScanMode) -> Any:
        del mode
        return await self._jenkins.get_node_info(str(args["name"]))

    async def _jenkins_get_queue(self, _: dict[str, Any], mode: ScanMode) -> Any:
        del mode
        return await self._jenkins.get_queue_info()

    async def _jenkins_get_running_builds(self, _: dict[str, Any], mode: ScanMode) -> Any:
        del mode
        return await self._jenkins.get_running_builds()

    async def _jenkins_get_job(self, args: dict[str, Any], mode: ScanMode) -> Any:
        del mode
        info = await self._jenkins.get_job_info(str(args["name"]), depth=0)
        return {
            key: info.get(key)
            for key in (
                "name",
                "fullName",
                "url",
                "color",
                "buildable",
                "inQueue",
                "lastBuild",
                "lastSuccessfulBuild",
                "lastFailedBuild",
                "healthReport",
            )
        }

    async def _jenkins_get_recent_failed_builds(self, args: dict[str, Any], mode: ScanMode) -> Any:
        del mode
        failures = await self._jenkins.get_recent_failed_builds(
            window_hours=float(args.get("window_hours") or self._jenkins.failed_build_window_hours),
            mr_only=bool(args.get("mr_only", False)),
        )
        limit = min(max(int(args.get("limit", 25)), 1), 100)
        return [item.to_dict() for item in failures[:limit]]

    async def _jenkins_get_build(self, args: dict[str, Any], mode: ScanMode) -> Any:
        del mode
        job, number = _build_args(args)
        info = await self._jenkins.get_build_info(job, number)
        return {
            "job_name": job,
            "number": info.get("number"),
            "result": info.get("result"),
            "duration": info.get("duration"),
            "estimated_duration": info.get("estimatedDuration"),
            "timestamp": info.get("timestamp"),
            "building": info.get("building"),
            "built_on": info.get("builtOn"),
            "url": info.get("url"),
            "parameters": await self._jenkins.get_build_parameters(job, number),
            "change_sets": _bounded_list(info.get("changeSets"), 20),
            "causes": _build_causes(info),
        }

    async def _jenkins_get_build_log(self, args: dict[str, Any], mode: ScanMode) -> str:
        job, number = _build_args(args)
        full = bool(args.get("full", False)) and mode is ScanMode.DEEP
        if full:
            output = await self._jenkins.get_build_console_output(job, number)
        else:
            max_bytes = 512_000 if mode is ScanMode.DEEP else 160_000
            output = await self._jenkins.get_build_console_tail(job, number, max_bytes=max_bytes)
        tail_lines = min(max(int(args.get("tail_lines", 300)), 20), 10_000)
        if not full:
            output = "\n".join(output.splitlines()[-tail_lines:])
        return output

    async def _jenkins_get_job_build_history(self, args: dict[str, Any], mode: ScanMode) -> Any:
        del mode
        limit = min(max(int(args.get("limit", 10)), 1), 50)
        return await self._jenkins.get_job_recent_builds(str(args["job_name"]), limit=limit)

    async def _jenkins_get_build_stages(self, args: dict[str, Any], mode: ScanMode) -> Any:
        del mode
        job, number = _build_args(args)
        return await self._jenkins.get_json(f"{job_to_api_path(job)}/{number}/wfapi/describe")

    async def _jenkins_get_test_report(self, args: dict[str, Any], mode: ScanMode) -> Any:
        del mode
        job, number = _build_args(args)
        report = await self._jenkins.get_json(
            f"{job_to_api_path(job)}/{number}/testReport/api/json",
            params={"tree": "failCount,skipCount,totalCount,duration,suites[cases[className,name,status,errorDetails,errorStackTrace]]"},
        )
        suites = report.get("suites") or []
        failed = []
        for suite in suites:
            for case in suite.get("cases") or []:
                if case.get("status") not in {"PASSED", "FIXED"}:
                    failed.append(case)
        return {
            "fail_count": report.get("failCount"),
            "skip_count": report.get("skipCount"),
            "total_count": report.get("totalCount"),
            "duration": report.get("duration"),
            "failed_tests": failed[:50],
            "omitted_failed_tests": max(0, len(failed) - 50),
        }

    async def _jenkins_analyze_build_failure(self, args: dict[str, Any], mode: ScanMode) -> Any:
        job, number = _build_args(args)
        info = await self._jenkins.get_build_info(job, number)
        output = await self._jenkins_get_build_log(
            {"job_name": job, "build_number": number, "tail_lines": 1500, "full": mode is ScanMode.DEEP},
            mode,
        )
        errors = extract_error_lines(output)
        result: dict[str, Any] = {
            "job_name": job,
            "build_number": number,
            "result": info.get("result"),
            "built_on": info.get("builtOn"),
            "duration_ms": info.get("duration"),
            "failure_classification": classify_failure(errors),
            "parameters": await self._jenkins.get_build_parameters(job, number),
            "error_lines": errors[:40],
            "causes": _build_causes(info),
        }
        try:
            result["stages"] = await self._jenkins_get_build_stages(args, mode)
        except Exception as exc:
            result["stages_error"] = f"{type(exc).__name__}: {exc}"
        try:
            result["test_report"] = await self._jenkins_get_test_report(args, mode)
        except Exception as exc:
            result["test_report_error"] = f"{type(exc).__name__}: {exc}"
        return result

    async def _k8s_get_resource(self, args: dict[str, Any], mode: ScanMode) -> Any:
        del mode
        kind = str(args["kind"]).lower()
        name = str(args["name"])
        namespace = str(args.get("namespace") or self._jenkins_namespace)
        core = self._kubernetes.core_v1()
        apps = self._kubernetes.apps_v1()
        batch = self._kubernetes.batch_v1()
        calls: dict[str, tuple[Callable[..., Any], tuple[Any, ...]]] = {
            "pod": (core.read_namespaced_pod, (name, namespace)),
            "service": (core.read_namespaced_service, (name, namespace)),
            "configmap": (core.read_namespaced_config_map, (name, namespace)),
            "node": (core.read_node, (name,)),
            "deployment": (apps.read_namespaced_deployment, (name, namespace)),
            "statefulset": (apps.read_namespaced_stateful_set, (name, namespace)),
            "daemonset": (apps.read_namespaced_daemon_set, (name, namespace)),
            "job": (batch.read_namespaced_job, (name, namespace)),
        }
        if kind not in calls:
            raise ValueError(f"unsupported Kubernetes resource kind: {kind}")
        function, positional = calls[kind]
        value = await self._kubernetes.run_sync(function, *positional)
        payload = core.api_client.sanitize_for_serialization(value)
        return _strip_k8s_noise(payload)

    async def _k8s_list_resources(self, args: dict[str, Any], mode: ScanMode) -> Any:
        del mode
        kind = str(args["kind"]).lower()
        namespace = args.get("namespace")
        selector = str(args.get("label_selector") or "")
        core = self._kubernetes.core_v1()
        apps = self._kubernetes.apps_v1()
        if kind == "pod":
            if namespace:
                result = await self._kubernetes.run_sync(
                    core.list_namespaced_pod, str(namespace), label_selector=selector
                )
            else:
                result = await self._kubernetes.run_sync(core.list_pod_for_all_namespaces, label_selector=selector)
            return [
                {
                    "namespace": item.metadata.namespace,
                    "name": item.metadata.name,
                    "phase": item.status.phase,
                    "node": item.spec.node_name,
                    "restarts": sum(value.restart_count for value in (item.status.container_statuses or [])),
                }
                for item in result.items
            ]
        if kind == "deployment":
            if namespace:
                result = await self._kubernetes.run_sync(
                    apps.list_namespaced_deployment, str(namespace), label_selector=selector
                )
            else:
                result = await self._kubernetes.run_sync(
                    apps.list_deployment_for_all_namespaces, label_selector=selector
                )
            return [
                {
                    "namespace": item.metadata.namespace,
                    "name": item.metadata.name,
                    "ready": item.status.ready_replicas or 0,
                    "desired": item.spec.replicas or 0,
                }
                for item in result.items
            ]
        if kind == "node":
            result = await self._kubernetes.run_sync(core.list_node, label_selector=selector)
            return [
                {
                    "name": item.metadata.name,
                    "conditions": {value.type: value.status for value in (item.status.conditions or [])},
                }
                for item in result.items
            ]
        if kind == "event":
            return await self._k8s_get_events(args, ScanMode.REGULAR)
        raise ValueError(f"unsupported Kubernetes list kind: {kind}")

    async def _k8s_get_events(self, args: dict[str, Any], mode: ScanMode) -> Any:
        del mode
        namespace = args.get("namespace")
        selectors = []
        event_type = args.get("event_type", "Warning")
        if event_type:
            selectors.append(f"type={event_type}")
        if args.get("pod_name"):
            selectors.extend([f"involvedObject.name={args['pod_name']}", "involvedObject.kind=Pod"])
        elif args.get("node_name"):
            selectors.extend([f"involvedObject.name={args['node_name']}", "involvedObject.kind=Node"])
        field_selector = ",".join(selectors)
        core = self._kubernetes.core_v1()
        if namespace:
            result = await self._kubernetes.run_sync(
                core.list_namespaced_event, str(namespace), field_selector=field_selector
            )
        else:
            result = await self._kubernetes.run_sync(
                core.list_event_for_all_namespaces, field_selector=field_selector
            )
        limit = min(max(int(args.get("limit", 50)), 1), 100)
        ordered = sorted(
            result.items,
            key=lambda item: item.last_timestamp or item.metadata.creation_timestamp or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )[:limit]
        return [
            {
                "namespace": item.involved_object.namespace,
                "kind": item.involved_object.kind,
                "name": item.involved_object.name,
                "type": item.type,
                "reason": item.reason,
                "message": item.message,
                "count": item.count,
                "last_seen": item.last_timestamp or item.metadata.creation_timestamp,
            }
            for item in ordered
        ]

    async def _k8s_get_pod_events(self, args: dict[str, Any], mode: ScanMode) -> Any:
        return await self._k8s_get_events(
            {"namespace": args["namespace"], "pod_name": args["pod_name"], "event_type": "", "limit": 100},
            mode,
        )

    async def _k8s_get_pod_logs(self, args: dict[str, Any], mode: ScanMode) -> Any:
        del mode
        kwargs: dict[str, Any] = {
            "name": str(args["pod_name"]),
            "namespace": str(args["namespace"]),
            "tail_lines": min(max(int(args.get("tail_lines", 200)), 20), 2000),
        }
        if args.get("container"):
            kwargs["container"] = str(args["container"])
        return await self._kubernetes.run_sync(self._kubernetes.core_v1().read_namespaced_pod_log, **kwargs)

    async def _k8s_top_pods(self, args: dict[str, Any], mode: ScanMode) -> Any:
        del mode
        namespace = str(args.get("namespace") or self._jenkins_namespace)
        values = await self._metrics.list_pod_metrics(namespace)
        measurements = [
            (
                item,
                sum(container.cpu_cores for container in item.containers),
                sum(container.memory_bytes for container in item.containers),
            )
            for item in values
        ]
        index = 1 if args.get("sort_by") == "cpu" else 2
        measurements.sort(key=lambda item: item[index], reverse=True)
        return [
            {
                "namespace": item.namespace,
                "name": item.name,
                "cpu": format_cores(cpu_cores),
                "memory": format_bytes(memory_bytes),
            }
            for item, cpu_cores, memory_bytes in measurements
        ]

    async def _k8s_top_nodes(self, _: dict[str, Any], mode: ScanMode) -> Any:
        del mode
        values = await self._metrics.list_node_metrics()
        allocatable = await self._metrics.get_node_allocatable()
        rows = []
        for item in values:
            limits = allocatable.get(item.name, {})
            rows.append(
                {
                    "name": item.name,
                    "cpu": format_cores(item.cpu_cores),
                    "cpu_percent": usage_pct(item.cpu_cores, limits.get("cpu_cores", 0)),
                    "memory": format_bytes(item.memory_bytes),
                    "memory_percent": usage_pct(item.memory_bytes, limits.get("memory_bytes", 0)),
                }
            )
        return rows

    async def _prometheus_query(self, args: dict[str, Any], mode: ScanMode) -> Any:
        del mode
        return await self._prometheus.query(str(args["promql"]))

    async def _prometheus_query_range(self, args: dict[str, Any], mode: ScanMode) -> Any:
        del mode
        now = datetime.now(timezone.utc)
        duration = min(max(int(args.get("duration_hours", 1)), 1), 168)
        return await self._prometheus.query_range(
            str(args["promql"]),
            start=(now - timedelta(hours=duration)).isoformat(),
            end=now.isoformat(),
            step=str(args.get("step") or "5m"),
        )

    async def _scm_get_change(self, args: dict[str, Any], mode: ScanMode) -> Any:
        del mode
        provider, repository, number = _change_args(args)
        if provider == "github":
            response = await self._http.get(
                f"{self._github_api_url}/repos/{repository}/pulls/{number}",
                headers=_github_headers(self._github_token),
            )
        elif provider == "gitlab":
            response = await self._http.get(
                f"{self._gitlab_api_url}/projects/{quote(repository, safe='')}/merge_requests/{number}",
                headers=_gitlab_headers(self._gitlab_token),
            )
        else:
            raise ValueError(f"unsupported SCM provider: {provider}")
        response.raise_for_status()
        payload = response.json()
        return {
            key: payload.get(key)
            for key in (
                "id",
                "iid",
                "number",
                "title",
                "state",
                "draft",
                "web_url",
                "html_url",
                "source_branch",
                "target_branch",
                "merge_status",
                "mergeable_state",
                "head",
                "base",
                "labels",
                "user",
                "author",
            )
        }

    async def _scm_get_change_diff(self, args: dict[str, Any], mode: ScanMode) -> Any:
        provider, repository, number = _change_args(args)
        if provider == "github":
            response = await self._http.get(
                f"{self._github_api_url}/repos/{repository}/pulls/{number}/files",
                params={"per_page": 100},
                headers=_github_headers(self._github_token),
            )
            response.raise_for_status()
            files = response.json()
            patch_limit = 12_000 if mode is ScanMode.DEEP else 4_000
            return [
                {
                    "filename": item.get("filename"),
                    "status": item.get("status"),
                    "additions": item.get("additions"),
                    "deletions": item.get("deletions"),
                    "patch": str(item.get("patch") or "")[:patch_limit],
                }
                for item in files[:100]
            ]
        if provider == "gitlab":
            response = await self._http.get(
                f"{self._gitlab_api_url}/projects/{quote(repository, safe='')}/merge_requests/{number}/changes",
                headers=_gitlab_headers(self._gitlab_token),
            )
            response.raise_for_status()
            payload = response.json()
            patch_limit = 12_000 if mode is ScanMode.DEEP else 4_000
            return [
                {
                    "old_path": item.get("old_path"),
                    "new_path": item.get("new_path"),
                    "new_file": item.get("new_file"),
                    "deleted_file": item.get("deleted_file"),
                    "renamed_file": item.get("renamed_file"),
                    "diff": str(item.get("diff") or "")[:patch_limit],
                }
                for item in (payload.get("changes") or [])[:100]
            ]
        raise ValueError(f"unsupported SCM provider: {provider}")


def _definitions() -> list[dict[str, Any]]:
    return [
        _tool("jenkins_list_agents", "List Jenkins agents with availability, executors, and monitor data."),
        _tool("jenkins_get_agent", "Get one Jenkins agent and its offline/executor details.", {"name": _string("Agent name")}, ["name"]),
        _tool("jenkins_get_queue", "Inspect the current Jenkins build queue and wait reasons."),
        _tool("jenkins_get_running_builds", "List running Jenkins builds and their assigned agents."),
        _tool("jenkins_get_job", "Read current Jenkins job status and recent build references.", {"name": _string("Full job path")}, ["name"]),
        _tool(
            "jenkins_get_recent_failed_builds",
            "Find recent failed Jenkins builds, optionally limited to MR/PR jobs.",
            {"window_hours": _integer("Lookback hours"), "mr_only": _boolean("Only MR/PR jobs"), "limit": _integer("Maximum rows")},
        ),
        _tool("jenkins_get_build", "Read build metadata, parameters, causes, node, and changes.", _build_properties(), ["job_name", "build_number"]),
        _tool(
            "jenkins_get_build_log",
            "Read a build console log. Full logs are permitted only for deep investigations.",
            _build_properties() | {"tail_lines": _integer("Tail line count"), "full": _boolean("Read the full log in deep mode")},
            ["job_name", "build_number"],
        ),
        _tool("jenkins_get_job_build_history", "Compare recent results and durations for a Jenkins job.", {"job_name": _string("Full job path"), "limit": _integer("Build count")}, ["job_name"]),
        _tool("jenkins_get_build_stages", "Read Jenkins Pipeline stage results and timing.", _build_properties(), ["job_name", "build_number"]),
        _tool("jenkins_get_test_report", "Read failed and skipped tests from a Jenkins build.", _build_properties(), ["job_name", "build_number"]),
        _tool("jenkins_analyze_build_failure", "Collect log errors, classification, parameters, stages, and tests for a failed build.", _build_properties(), ["job_name", "build_number"]),
        _tool("k8s_get_resource", "Read the spec and status of a Kubernetes resource.", {"kind": _string("Pod, Deployment, StatefulSet, DaemonSet, Service, ConfigMap, Job, or Node"), "name": _string("Resource name"), "namespace": _string("Namespace")}, ["kind", "name"]),
        _tool("k8s_list_resources", "List Kubernetes Pods, Deployments, Nodes, or Events with status.", {"kind": _string("Pod, Deployment, Node, or Event"), "namespace": _string("Optional namespace"), "label_selector": _string("Kubernetes label selector")}, ["kind"]),
        _tool("k8s_get_events", "Read Kubernetes events filtered by namespace, pod, node, and type.", {"namespace": _string("Namespace"), "pod_name": _string("Pod name"), "node_name": _string("Node name"), "event_type": _string("Warning, Normal, or empty"), "limit": _integer("Maximum events")}),
        _tool("k8s_get_pod_events", "Read all Kubernetes events for one pod.", {"namespace": _string("Namespace"), "pod_name": _string("Pod name")}, ["namespace", "pod_name"]),
        _tool("k8s_get_pod_logs", "Read recent logs from a Kubernetes pod container.", {"namespace": _string("Namespace"), "pod_name": _string("Pod name"), "container": _string("Container name"), "tail_lines": _integer("Tail line count")}, ["namespace", "pod_name"]),
        _tool("k8s_top_pods", "Read current pod CPU and memory usage from metrics-server.", {"namespace": _string("Namespace"), "sort_by": _string("memory or cpu")}),
        _tool("k8s_top_nodes", "Read current node CPU and memory usage from metrics-server."),
        _tool("prometheus_query", "Execute a read-only instant PromQL query.", {"promql": _string("PromQL expression")}, ["promql"]),
        _tool("prometheus_query_range", "Execute a read-only range PromQL query.", {"promql": _string("PromQL expression"), "duration_hours": _integer("Lookback hours"), "step": _string("Prometheus step")}, ["promql"]),
        _tool("scm_get_change", "Read GitHub pull request or GitLab merge request metadata.", _change_properties(), ["provider", "repository", "change_number"]),
        _tool("scm_get_change_diff", "Read changed files and bounded patches for a GitHub PR or GitLab MR.", _change_properties(), ["provider", "repository", "change_number"]),
    ]


def _tool(
    name: str,
    description: str,
    properties: dict[str, Any] | None = None,
    required: list[str] | None = None,
) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties or {}, "additionalProperties": False}
    if required:
        schema["required"] = required
    return {"type": "function", "function": {"name": name, "description": description, "parameters": schema}}


def _string(description: str) -> dict[str, str]:
    return {"type": "string", "description": description}


def _integer(description: str) -> dict[str, str]:
    return {"type": "integer", "description": description}


def _boolean(description: str) -> dict[str, str]:
    return {"type": "boolean", "description": description}


def _build_properties() -> dict[str, Any]:
    return {"job_name": _string("Full Jenkins job path"), "build_number": _integer("Build number")}


def _change_properties() -> dict[str, Any]:
    return {
        "provider": _string("github or gitlab"),
        "repository": _string("GitHub owner/repo or GitLab project path"),
        "change_number": _string("Pull request or merge request number"),
    }


def _build_args(args: dict[str, Any]) -> tuple[str, int]:
    return str(args["job_name"]), int(args["build_number"])


def _change_args(args: dict[str, Any]) -> tuple[str, str, str]:
    return str(args["provider"]).lower(), str(args["repository"]), str(args["change_number"])


def _build_causes(info: dict[str, Any]) -> list[dict[str, Any]]:
    causes = []
    for action in info.get("actions") or []:
        if isinstance(action, dict):
            causes.extend(item for item in action.get("causes") or [] if isinstance(item, dict))
    return causes[:20]


def _bounded_list(value: Any, limit: int) -> list[Any]:
    return list(value[:limit]) if isinstance(value, list) else []


def _strip_k8s_noise(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    metadata = value.get("metadata")
    if isinstance(metadata, dict):
        metadata.pop("managed_fields", None)
        metadata.pop("managedFields", None)
        annotations = metadata.get("annotations")
        if isinstance(annotations, dict):
            metadata["annotations"] = {
                key: nested
                for key, nested in annotations.items()
                if not key.startswith(("kubectl.kubernetes.io/", "meta.helm.sh/")) and "last-applied" not in key
            }
    return value


def _github_headers(token: str) -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _gitlab_headers(token: str) -> dict[str, str]:
    return {"PRIVATE-TOKEN": token} if token else {}


def _truncate(value: str, *, mode: ScanMode) -> str:
    limit = 24_000 if mode is ScanMode.DEEP else 12_000
    if len(value) <= limit:
        return value
    return value[:limit] + f"\n... [tool output truncated at {limit} characters]"


def _redact(value: str) -> str:
    value = re.sub(
        r"(?i)(\bauthorization\b[\"']?\s*[:=]\s*[\"']?)(?:Bearer\s+)?[^\s\",}]+",
        r"\1[REDACTED]",
        value,
    )
    value = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", value)
    return re.sub(
        r"(?i)(\b(?:password|passwd|token|secret|api[_-]?key)\b[\"']?\s*[:=]\s*[\"']?)([^\s\",}]+)",
        r"\1[REDACTED]",
        value,
    )
