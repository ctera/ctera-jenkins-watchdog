from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from jenkins_watchdog.checks.agent_connectivity import AgentConnectivityCheck
from jenkins_watchdog.checks.agent_errors import AgentErrorCheck
from jenkins_watchdog.checks.agent_pods import AgentPodCheck
from jenkins_watchdog.checks.jenkins_jobs import JenkinsJobCheck
from jenkins_watchdog.checks.jenkins_pipeline_patterns import (
    JenkinsPipelinePatternCheck,
    _analyze_streak,
    _detect_parameter_anomalies,
)
from jenkins_watchdog.checks.k8s_events import K8sEventsCheck
from jenkins_watchdog.checks.k8s_workloads import WorkloadCheck
from jenkins_watchdog.clients.jenkins import FailedBuildSummary


def namespace(**values):
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_agent_connectivity_characterizes_controller_and_resource_findings(monkeypatch) -> None:
    async def nodes():
        return [
            {
                "displayName": "linux-1",
                "offline": False,
                "idle": False,
                "numExecutors": 0,
                "monitorData": {
                    "hudson.node_monitors.SwapSpaceMonitor": {
                        "totalPhysicalMemory": 100,
                        "availablePhysicalMemory": 4,
                    },
                    "hudson.node_monitors.DiskSpaceMonitor": {
                        "size": 512 * 1024**2,
                        "path": "/work",
                    },
                },
            }
        ]

    client = namespace(get_nodes=nodes)
    findings = await AgentConnectivityCheck(client).run()

    assert {item.severity for item in findings} == {"warning", "critical"}
    assert {item.context.get("path") for item in findings} == {None, "/work"}
    assert any("memory" in item.symptom for item in findings)

    async def unavailable():
        raise RuntimeError("offline")

    [controller] = await AgentConnectivityCheck(namespace(get_nodes=unavailable)).run()
    assert controller.category == "jenkins_controller"
    assert "Cannot reach" in controller.symptom


@pytest.mark.asyncio
async def test_agent_pods_characterizes_pending_oom_waiting_restarts_and_termination(monkeypatch) -> None:
    empty_state = namespace(terminated=None, waiting=None, running=None)
    oom = namespace(
        name="jnlp",
        restart_count=7,
        last_state=namespace(terminated=namespace(reason="OOMKilled")),
        state=empty_state,
    )
    waiting = namespace(
        name="sidecar",
        restart_count=6,
        last_state=namespace(terminated=None),
        state=namespace(
            terminated=None,
            running=None,
            waiting=namespace(reason="CrashLoopBackOff", message="backoff"),
        ),
    )
    restarted = namespace(
        name="jnlp",
        restart_count=6,
        last_state=namespace(terminated=None),
        state=empty_state,
    )
    pods = [
        namespace(
            metadata=namespace(name="jenkins-agent-pending", namespace="jenkins", deletion_timestamp=None),
            status=namespace(container_statuses=[], phase="Pending"),
        ),
        namespace(
            metadata=namespace(
                name="jenkins-agent-broken",
                namespace="jenkins",
                deletion_timestamp=datetime.now(timezone.utc),
            ),
            status=namespace(container_statuses=[oom, waiting], phase="Running"),
        ),
        namespace(
            metadata=namespace(name="jenkins-agent-restarts", namespace="jenkins", deletion_timestamp=None),
            status=namespace(container_statuses=[restarted], phase="Running"),
        ),
    ]

    async def list_pods(*args):
        del args
        return pods

    monkeypatch.setattr("jenkins_watchdog.checks.agent_pods.list_jenkins_agent_pods", list_pods)
    findings = await AgentPodCheck(namespace(), namespace="jenkins").run()

    symptoms = [item.symptom for item in findings]
    assert "Agent pod stuck in Pending phase" in symptoms
    assert any("OOMKilled" in item for item in symptoms)
    assert any("CrashLoopBackOff" in item for item in symptoms)
    assert any("restarts" in item for item in symptoms)
    assert "Stuck terminating (deletion_timestamp set)" in symptoms


@pytest.mark.asyncio
async def test_agent_error_check_characterizes_exit_and_log_patterns(monkeypatch) -> None:
    terminated = namespace(
        name="jnlp",
        restart_count=1,
        state=namespace(
            terminated=namespace(exit_code=137, reason="OOMKilled"),
            running=None,
            waiting=None,
        ),
        last_state=namespace(terminated=None),
    )
    running = namespace(
        name="builder",
        restart_count=0,
        state=namespace(terminated=None, running=namespace(), waiting=None),
        last_state=namespace(terminated=None),
    )
    pod = namespace(
        metadata=namespace(name="jenkins-agent-1", namespace="jenkins"),
        status=namespace(container_statuses=[terminated, running]),
    )

    async def list_pods(*args):
        del args
        return [pod]

    async def run_sync(func, **kwargs):
        del func, kwargs
        return "INFO\njava.lang.OutOfMemoryError: heap\nFATAL: disconnected"

    monkeypatch.setattr("jenkins_watchdog.checks.agent_errors.list_jenkins_agent_pods", list_pods)
    kubernetes = namespace(
        core_v1=lambda: namespace(read_namespaced_pod_log=lambda: None),
        run_sync=run_sync,
    )
    findings = await AgentErrorCheck(
        kubernetes,
        namespace="jenkins",
        request_timeout_seconds=1,
    ).run()

    assert any(item.context.get("exit_code") == 137 for item in findings)
    assert any(item.context.get("error_count") == 2 for item in findings)


@pytest.mark.asyncio
async def test_jenkins_jobs_characterizes_queue_congestion_and_long_build(monkeypatch) -> None:
    monkeypatch.setattr("jenkins_watchdog.checks.jenkins_jobs.time.time", lambda: 10_000)

    async def queue():
        return [
            {
                "inQueueSince": 1,
                "why": "waiting for executor",
                "task": {"name": f"job-{index}"},
            }
            for index in range(21)
        ]

    async def builds():
        return [
            {
                "name": "app/main",
                "number": 9,
                "url": "https://jenkins/job/app/9",
                "timestamp": 1_000 * 1_000,
            }
        ]

    findings = await JenkinsJobCheck(namespace(get_queue_info=queue, get_running_builds=builds)).run()

    assert any("stuck" in item.symptom for item in findings)
    assert any("congestion" in item.symptom for item in findings)
    assert any("running for" in item.symptom for item in findings)


@pytest.mark.asyncio
async def test_pipeline_patterns_characterizes_streak_parameters_and_shared_signature(monkeypatch) -> None:
    summaries = [
        FailedBuildSummary(name, 12, "FAILURE", 1000, 10_000, "url", True) for name in ("app/MR-1", "app/MR-2")
    ]

    async def failed_builds(**kwargs):
        del kwargs
        return summaries

    async def history(job_name, *, limit):
        del job_name, limit
        return [
            {"number": 12, "result": "FAILURE", "timestamp": 10_000},
            {"number": 11, "result": "FAILURE", "timestamp": 9_000},
            {"number": 10, "result": "FAILURE", "timestamp": 8_000},
            {"number": 9, "result": "SUCCESS", "timestamp": 7_000},
        ]

    async def signature(client, job_name, build_number):
        del client, job_name, build_number
        return ["Compilation failure"], "shared-signature", "compilation_error"

    async def parameters(job_name, build_number):
        del job_name, build_number
        return {"BRANCH": "main", "EMPTY": ""}

    monkeypatch.setattr(
        "jenkins_watchdog.checks.jenkins_pipeline_patterns._fetch_log_signature",
        signature,
    )
    client = namespace(
        get_recent_failed_builds=failed_builds,
        get_job_recent_builds=history,
        get_build_parameters=parameters,
    )
    findings = await JenkinsPipelinePatternCheck(client).run()

    assert sum("consecutive failures" in item.symptom for item in findings) == 2
    assert any(item.context.get("pattern") == "parameter_anomaly" for item in findings)
    assert any(item.context.get("pattern") == "shared_failure_signature" for item in findings)
    assert _analyze_streak([])["consecutive_failures"] == 0
    assert _detect_parameter_anomalies({"VALUE": "x" * 501}, "job") == ["VALUE has unusually long value (501 chars)"]


@pytest.mark.asyncio
async def test_k8s_events_characterizes_grouping_and_severity(monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    meaningful = namespace(
        reason="Evicted",
        last_timestamp=now,
        event_time=None,
        count=2,
        message="node pressure",
        involved_object=namespace(namespace="jenkins", kind="Pod", name="agent-1"),
        metadata=namespace(namespace="jenkins", creation_timestamp=now),
        source=namespace(component="kubelet", host="worker-1"),
    )
    ignored = namespace(
        reason="Pulled",
        last_timestamp=now,
        event_time=None,
        count=1,
        message="image",
        involved_object=namespace(namespace="jenkins", kind="Pod", name="agent-1"),
        metadata=namespace(namespace="jenkins", creation_timestamp=now),
        source=None,
    )

    async def run_sync(func, *args, **kwargs):
        del func, args, kwargs
        return namespace(items=[meaningful, ignored])

    kubernetes = namespace(
        core_v1=lambda: namespace(list_namespaced_event=lambda: None),
        run_sync=run_sync,
    )
    [finding] = await K8sEventsCheck(
        kubernetes,
        jenkins_namespace="jenkins",
        window_minutes=30,
    ).run()

    assert finding.severity == "critical"
    assert finding.context["count"] == 4
    assert finding.context["source"]["host"] == "worker-1"


@pytest.mark.asyncio
async def test_workload_check_characterizes_deployments_and_statefulsets(monkeypatch) -> None:
    deployment = namespace(
        metadata=namespace(namespace="jenkins", name="jenkins-controller", labels={}),
        spec=namespace(replicas=2),
        status=namespace(
            available_replicas=0,
            unavailable_replicas=2,
            conditions=[namespace(type="Progressing", status="False", message="deadline", reason="timeout")],
        ),
    )
    statefulset = namespace(
        metadata=namespace(namespace="jenkins", name="jenkins-cache", labels={}),
        spec=namespace(replicas=2),
        status=namespace(ready_replicas=1),
    )
    apps = namespace(
        list_deployment_for_all_namespaces=lambda: namespace(items=[deployment]),
        list_stateful_set_for_all_namespaces=lambda: namespace(items=[statefulset]),
    )

    async def run_sync(func, **kwargs):
        del kwargs
        return func()

    kubernetes = namespace(apps_v1=lambda: apps, run_sync=run_sync)
    findings = await WorkloadCheck(kubernetes).run()

    assert len(findings) == 3
    assert findings[0].severity == "critical"
    assert any("rollout stuck" in item.symptom for item in findings)
    assert any("StatefulSet" in item.symptom for item in findings)
