from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from jenkins_watchdog.clients.k8s_metrics import ContainerUsage, NodeMetrics, PodMetrics
from jenkins_watchdog.domain.model import ScanMode
from jenkins_watchdog.infrastructure import tools as tools_module
from jenkins_watchdog.infrastructure.tools import ReadOnlyToolRegistry

NOW = datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)


class FailedBuild:
    def to_dict(self) -> dict[str, Any]:
        return {"job_name": "portal/MR-42", "build_number": 12, "result": "FAILURE"}


class FakeJenkins:
    failed_build_window_hours = 4

    def __init__(self) -> None:
        self.tail_calls: list[tuple[str, int, int]] = []
        self.full_calls: list[tuple[str, int]] = []

    async def get_nodes(self):
        return [{"displayName": "agent-a", "offline": False}]

    async def get_node_info(self, name: str):
        return {"displayName": name, "authorization": "Bearer node-secret", "token": "node-token"}

    async def get_queue_info(self):
        return [{"id": 1, "why": "Waiting for next available executor"}]

    async def get_running_builds(self):
        return [{"name": "portal", "number": 12, "node": "agent-a"}]

    async def get_job_info(self, name: str, depth: int = 0):
        assert depth == 0
        return {
            "name": name,
            "fullName": name,
            "url": "https://jenkins/job/portal/",
            "color": "red",
            "buildable": True,
            "inQueue": False,
            "lastBuild": {"number": 12},
            "lastSuccessfulBuild": {"number": 11},
            "lastFailedBuild": {"number": 12},
            "healthReport": [],
            "ignored": "not returned",
        }

    async def get_recent_failed_builds(self, *, window_hours: float, mr_only: bool):
        assert window_hours > 0
        assert mr_only is True
        return [FailedBuild(), FailedBuild()]

    async def get_build_info(self, job: str, number: int):
        return {
            "number": number,
            "result": "FAILURE",
            "duration": 120_000,
            "estimatedDuration": 100_000,
            "timestamp": int(NOW.timestamp() * 1000),
            "building": False,
            "builtOn": "agent-a",
            "url": f"https://jenkins/job/{job}/{number}/",
            "changeSets": [{"kind": "git", "items": [1]}] * 25,
            "actions": [None, {"causes": [{"_class": "UserIdCause"}, "bad"]}],
        }

    async def get_build_parameters(self, job: str, number: int):
        del job, number
        return {"MR_ID": "42"}

    async def get_build_console_output(self, job: str, number: int):
        self.full_calls.append((job, number))
        return "full line\npassword=full-secret\nerror: compilation failed"

    async def get_build_console_tail(self, job: str, number: int, *, max_bytes: int = 160_000):
        self.tail_calls.append((job, number, max_bytes))
        return "old line\nnew line\napi_key=tail-secret\nerror: compilation failed"

    async def get_job_recent_builds(self, job: str, *, limit: int):
        return [{"job": job, "number": index} for index in range(limit)]

    async def get_json(self, path: str, *, params=None):
        del params
        if path.endswith("/wfapi/describe"):
            return {"stages": [{"name": "Compile", "status": "FAILED"}]}
        if "/testReport/api/json" in path:
            return {
                "failCount": 1,
                "skipCount": 1,
                "totalCount": 3,
                "duration": 2.5,
                "suites": [
                    {
                        "cases": [
                            {"name": "passes", "status": "PASSED"},
                            {"name": "fixed", "status": "FIXED"},
                            {"name": "fails", "status": "FAILED", "errorDetails": "assertion"},
                        ]
                    }
                ],
            }
        raise AssertionError(path)


def _pod(name: str = "pod-a") -> SimpleNamespace:
    return SimpleNamespace(
        metadata=SimpleNamespace(namespace="jenkins", name=name),
        status=SimpleNamespace(
            phase="Running",
            container_statuses=[SimpleNamespace(restart_count=2), SimpleNamespace(restart_count=1)],
        ),
        spec=SimpleNamespace(node_name="worker-a"),
    )


def _deployment(name: str = "controller") -> SimpleNamespace:
    return SimpleNamespace(
        metadata=SimpleNamespace(namespace="jenkins", name=name),
        status=SimpleNamespace(ready_replicas=1),
        spec=SimpleNamespace(replicas=2),
    )


def _node(name: str = "worker-a") -> SimpleNamespace:
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name),
        status=SimpleNamespace(conditions=[SimpleNamespace(type="Ready", status="True")]),
    )


def _event(name: str, hour: int) -> SimpleNamespace:
    timestamp = NOW.replace(hour=hour)
    return SimpleNamespace(
        involved_object=SimpleNamespace(namespace="jenkins", kind="Pod", name=name),
        type="Warning",
        reason="BackOff",
        message="container restarting",
        count=3,
        last_timestamp=timestamp,
        metadata=SimpleNamespace(creation_timestamp=timestamp),
    )


class CoreApi:
    def __init__(self) -> None:
        self.api_client = self

    def sanitize_for_serialization(self, value):
        del value
        return {
            "metadata": {
                "managedFields": ["noise"],
                "annotations": {
                    "kubectl.kubernetes.io/last-applied-configuration": "secret",
                    "meta.helm.sh/release-name": "jenkins",
                    "owner": "platform",
                },
            },
            "status": {"phase": "Running"},
        }

    def read_namespaced_pod(self, name, namespace):
        return name, namespace

    def read_namespaced_service(self, name, namespace):
        return name, namespace

    def read_namespaced_config_map(self, name, namespace):
        return name, namespace

    def read_node(self, name):
        return name

    def list_namespaced_pod(self, namespace, *, label_selector):
        del namespace, label_selector
        return SimpleNamespace(items=[_pod()])

    def list_pod_for_all_namespaces(self, *, label_selector):
        del label_selector
        return SimpleNamespace(items=[_pod("pod-all")])

    def list_node(self, *, label_selector=""):
        del label_selector
        return SimpleNamespace(items=[_node()])

    def list_namespaced_event(self, namespace, *, field_selector):
        del namespace, field_selector
        return SimpleNamespace(items=[_event("older", 8), _event("newer", 9)])

    def list_event_for_all_namespaces(self, *, field_selector):
        del field_selector
        return SimpleNamespace(items=[_event("cluster-event", 7)])

    def read_namespaced_pod_log(self, **kwargs):
        assert kwargs["tail_lines"] <= 2000
        return "pod log token=pod-secret"


class AppsApi:
    def read_namespaced_deployment(self, name, namespace):
        return name, namespace

    def read_namespaced_stateful_set(self, name, namespace):
        return name, namespace

    def read_namespaced_daemon_set(self, name, namespace):
        return name, namespace

    def list_namespaced_deployment(self, namespace, *, label_selector):
        del namespace, label_selector
        return SimpleNamespace(items=[_deployment()])

    def list_deployment_for_all_namespaces(self, *, label_selector):
        del label_selector
        return SimpleNamespace(items=[_deployment("all-controller")])


class BatchApi:
    def read_namespaced_job(self, name, namespace):
        return name, namespace


class FakeKubernetes:
    def __init__(self) -> None:
        self.core = CoreApi()
        self.apps = AppsApi()
        self.batch = BatchApi()

    def core_v1(self):
        return self.core

    def apps_v1(self):
        return self.apps

    def batch_v1(self):
        return self.batch

    async def run_sync(self, function, *args, **kwargs):
        return function(*args, **kwargs)


class FakeMetrics:
    async def list_pod_metrics(self, namespace: str):
        return [
            PodMetrics(namespace, "small", [ContainerUsage("c", 0.9, 900 * 1024**2)]),
            PodMetrics(namespace, "large", [ContainerUsage("c", 2.0, 2 * 1024**3)]),
        ]

    async def list_node_metrics(self):
        return [NodeMetrics("worker-a", 2.0, 4 * 1024**3), NodeMetrics("unknown", 0.5, 512 * 1024**2)]

    async def get_node_allocatable(self):
        return {"worker-a": {"cpu_cores": 4.0, "memory_bytes": 8 * 1024**3}}


class FakePrometheus:
    def __init__(self) -> None:
        self.range_call: dict[str, str] | None = None

    async def query(self, promql: str):
        return [{"metric": {"query": promql}, "value": [1, "2"]}]

    async def query_range(self, promql: str, *, start: str, end: str, step: str):
        self.range_call = {"promql": promql, "start": start, "end": end, "step": step}
        return [{"metric": {}, "values": []}]


def _http_client() -> httpx.AsyncClient:
    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/pulls/42/files"):
            return httpx.Response(
                200,
                json=[{"filename": "app.py", "status": "modified", "additions": 4, "deletions": 2, "patch": "x" * 20_000}],
            )
        if path.endswith("/pulls/42"):
            return httpx.Response(200, json={"number": 42, "title": "Fix", "state": "open", "html_url": "https://github/pr/42"})
        if path.endswith("/merge_requests/7/changes"):
            return httpx.Response(
                200,
                json={"changes": [{"old_path": "a", "new_path": "b", "new_file": False, "deleted_file": False, "renamed_file": True, "diff": "y" * 20_000}]},
            )
        if path.endswith("/merge_requests/7"):
            return httpx.Response(200, json={"iid": 7, "title": "MR", "state": "opened", "web_url": "https://gitlab/mr/7"})
        return httpx.Response(404, json={"error": path})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _registry(http: httpx.AsyncClient | None = None) -> tuple[ReadOnlyToolRegistry, FakeJenkins, FakePrometheus]:
    jenkins = FakeJenkins()
    prometheus = FakePrometheus()
    registry = ReadOnlyToolRegistry(
        jenkins=jenkins,  # type: ignore[arg-type]
        kubernetes=FakeKubernetes(),  # type: ignore[arg-type]
        metrics=FakeMetrics(),  # type: ignore[arg-type]
        prometheus=prometheus,  # type: ignore[arg-type]
        http=http or _http_client(),
        jenkins_namespace="jenkins",
        github_api_url="https://api.github.test/",
        github_token="github-secret",
        gitlab_api_url="https://gitlab.test/api/v4/",
        gitlab_token="gitlab-secret",
    )
    return registry, jenkins, prometheus


@pytest.mark.asyncio
async def test_jenkins_tools_return_bounded_redacted_evidence_in_regular_and_deep_modes() -> None:
    registry, jenkins, _ = _registry()
    assert len(registry.definitions) == 23
    calls = [
        ("jenkins_list_agents", {}),
        ("jenkins_get_agent", {"name": "agent-a"}),
        ("jenkins_get_queue", {}),
        ("jenkins_get_running_builds", {}),
        ("jenkins_get_job", {"name": "portal"}),
        ("jenkins_get_recent_failed_builds", {"window_hours": 24, "mr_only": True, "limit": 1}),
        ("jenkins_get_build", {"job_name": "portal", "build_number": 12}),
        ("jenkins_get_build_log", {"job_name": "portal", "build_number": 12, "tail_lines": 2, "full": True}),
        ("jenkins_get_job_build_history", {"job_name": "portal", "limit": 3}),
        ("jenkins_get_build_stages", {"job_name": "portal", "build_number": 12}),
        ("jenkins_get_test_report", {"job_name": "portal", "build_number": 12}),
        ("jenkins_analyze_build_failure", {"job_name": "portal", "build_number": 12}),
    ]
    results = {}
    for name, args in calls:
        execution = await registry.execute(name, args, mode=ScanMode.REGULAR)
        assert execution.ok, (name, execution.output)
        results[name] = execution.output if name == "jenkins_get_build_log" else json.loads(execution.output)

    assert results["jenkins_get_agent"]["authorization"] == "[REDACTED]"
    assert results["jenkins_get_agent"]["token"] == "[REDACTED]"
    assert len(results["jenkins_get_recent_failed_builds"]) == 1
    assert len(results["jenkins_get_build"]["change_sets"]) == 20
    assert results["jenkins_get_test_report"]["failed_tests"][0]["name"] == "fails"
    assert results["jenkins_analyze_build_failure"]["error_lines"]
    assert "tail-secret" not in results["jenkins_get_build_log"]
    assert jenkins.full_calls == []
    assert jenkins.tail_calls[-1][2] == 160_000

    deep = await registry.execute(
        "jenkins_get_build_log",
        {"job_name": "portal", "build_number": 12, "full": True},
        mode=ScanMode.DEEP,
    )
    assert deep.ok and "full-secret" not in deep.output and "[REDACTED]" in deep.output
    assert jenkins.full_calls == [("portal", 12)]


@pytest.mark.asyncio
async def test_kubernetes_tools_cover_resources_events_logs_and_numeric_metrics_ordering() -> None:
    registry, _, _ = _registry()
    for kind in ("pod", "service", "configmap", "node", "deployment", "statefulset", "daemonset", "job"):
        execution = await registry.execute(
            "k8s_get_resource",
            {"kind": kind, "name": "resource-a", "namespace": "jenkins"},
            mode=ScanMode.REGULAR,
        )
        assert execution.ok
        payload = json.loads(execution.output)
        assert payload["metadata"]["annotations"] == {"owner": "platform"}
        assert "managedFields" not in payload["metadata"]

    list_calls = [
        {"kind": "pod", "namespace": "jenkins"},
        {"kind": "pod"},
        {"kind": "deployment", "namespace": "jenkins"},
        {"kind": "deployment"},
        {"kind": "node"},
        {"kind": "event", "namespace": "jenkins", "pod_name": "pod-a"},
    ]
    for args in list_calls:
        assert (await registry.execute("k8s_list_resources", args, mode=ScanMode.REGULAR)).ok

    events = await registry.execute(
        "k8s_get_events",
        {"node_name": "worker-a", "event_type": "Warning", "limit": 1},
        mode=ScanMode.REGULAR,
    )
    assert events.ok and json.loads(events.output)[0]["name"] == "cluster-event"
    pod_events = await registry.execute(
        "k8s_get_pod_events",
        {"namespace": "jenkins", "pod_name": "pod-a"},
        mode=ScanMode.REGULAR,
    )
    assert pod_events.ok and json.loads(pod_events.output)[0]["name"] == "newer"
    logs = await registry.execute(
        "k8s_get_pod_logs",
        {"namespace": "jenkins", "pod_name": "pod-a", "container": "jnlp", "tail_lines": 10_000},
        mode=ScanMode.REGULAR,
    )
    assert logs.ok and "pod-secret" not in logs.output

    pods_memory = await registry.execute("k8s_top_pods", {"sort_by": "memory"}, mode=ScanMode.REGULAR)
    pods_cpu = await registry.execute("k8s_top_pods", {"sort_by": "cpu"}, mode=ScanMode.REGULAR)
    assert json.loads(pods_memory.output)[0]["name"] == "large"
    assert json.loads(pods_cpu.output)[0]["name"] == "large"
    nodes = await registry.execute("k8s_top_nodes", {}, mode=ScanMode.REGULAR)
    node_rows = json.loads(nodes.output)
    assert node_rows[0]["cpu_percent"] == 50.0
    assert node_rows[1]["cpu_percent"] is None


@pytest.mark.asyncio
async def test_prometheus_and_scm_tools_support_both_providers_and_bounded_diffs() -> None:
    http = _http_client()
    registry, _, prometheus = _registry(http)
    try:
        instant = await registry.execute("prometheus_query", {"promql": "up"}, mode=ScanMode.REGULAR)
        ranged = await registry.execute(
            "prometheus_query_range",
            {"promql": "rate(builds[5m])", "duration_hours": 999, "step": "1m"},
            mode=ScanMode.REGULAR,
        )
        assert instant.ok and ranged.ok
        assert prometheus.range_call is not None and prometheus.range_call["step"] == "1m"

        github = {"provider": "github", "repository": "ctera/portal", "change_number": "42"}
        gitlab = {"provider": "gitlab", "repository": "group/subgroup/repo", "change_number": "7"}
        assert (await registry.execute("scm_get_change", github, mode=ScanMode.REGULAR)).ok
        assert (await registry.execute("scm_get_change", gitlab, mode=ScanMode.REGULAR)).ok
        github_diff = await registry.execute("scm_get_change_diff", github, mode=ScanMode.REGULAR)
        gitlab_diff = await registry.execute("scm_get_change_diff", gitlab, mode=ScanMode.DEEP)
        assert len(json.loads(github_diff.output)[0]["patch"]) == 4_000
        assert len(json.loads(gitlab_diff.output)[0]["diff"]) == 12_000
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_tool_errors_are_data_and_helpers_enforce_read_only_output_bounds() -> None:
    registry, _, _ = _registry()
    unknown = await registry.execute("delete_everything", {}, mode=ScanMode.REGULAR)
    invalid_kind = await registry.execute(
        "k8s_get_resource",
        {"kind": "secret", "name": "credentials"},
        mode=ScanMode.REGULAR,
    )
    invalid_list = await registry.execute("k8s_list_resources", {"kind": "secret"}, mode=ScanMode.REGULAR)
    invalid_scm = await registry.execute(
        "scm_get_change",
        {"provider": "bitbucket", "repository": "a/b", "change_number": "1"},
        mode=ScanMode.REGULAR,
    )
    invalid_diff = await registry.execute(
        "scm_get_change_diff",
        {"provider": "bitbucket", "repository": "a/b", "change_number": "1"},
        mode=ScanMode.REGULAR,
    )
    assert not unknown.ok
    assert not invalid_kind.ok and "unsupported Kubernetes resource" in invalid_kind.output
    assert not invalid_list.ok and not invalid_scm.ok and not invalid_diff.ok

    assert tools_module._bounded_list("not-a-list", 3) == []
    assert tools_module._strip_k8s_noise("plain") == "plain"
    assert "Authorization" not in tools_module._github_headers("")
    assert "Authorization" in tools_module._github_headers("secret")
    assert tools_module._gitlab_headers("") == {}
    assert tools_module._gitlab_headers("secret") == {"PRIVATE-TOKEN": "secret"}
    truncated = tools_module._truncate("x" * 100_000, mode=ScanMode.REGULAR)
    assert len(truncated) < 100_000 and "truncated" in truncated
    deep_truncated = tools_module._truncate("x" * 100_000, mode=ScanMode.DEEP)
    assert len(deep_truncated) < 100_000 and "24000" in deep_truncated
    assert tools_module._truncate("short", mode=ScanMode.DEEP) == "short"
    redacted = tools_module._redact('password="secret" Bearer abc.def api-key: value')
    assert "secret" not in redacted and "abc.def" not in redacted and "value" not in redacted
