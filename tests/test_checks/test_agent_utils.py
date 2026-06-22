"""Tests for Jenkins agent pod detection helpers."""

from jenkins_watchdog.checks.agent_utils import (
    extract_agent_prefix,
    extract_prefix_from_resource,
    group_agent_findings,
    is_jenkins_agent_pod,
    symptom_class,
)
from jenkins_watchdog.checks.base import Finding
from jenkins_watchdog.clients.jenkins import _job_name_from_build_url


def test_is_jenkins_agent_pod_by_label():
    labels = {
        "jenkins-slave": "true",
        "jenkins/label": "AgentCloudSyncRegression_3052-z86w3",
        "kubernetes.jenkins.io/controller": "https___jenkins_ctera_devx",
    }
    assert is_jenkins_agent_pod("agentcloudsyncregression-3052-z86w3-22bqb-msxrh", labels)


def test_is_jenkins_agent_pod_by_name():
    assert is_jenkins_agent_pod("jenkins-agent-abc123", {})
    assert not is_jenkins_agent_pod("nginx-ingress-controller", {})


def test_extract_agent_prefix_from_k8s_pod_suffix():
    assert extract_agent_prefix("esxdeploy-7d4f6b8c-xk2mq") == "esxdeploy"
    assert (
        extract_agent_prefix("agentcloudsyncregression-3052-z86w3-22bqb-msxrh")
        == "agentcloudsyncregression-3052-z86w3"
    )


def test_extract_agent_prefix_from_jenkins_label():
    labels = {"jenkins/label": "ESXDeploy"}
    assert extract_agent_prefix("some-random-pod-abc123", labels) == "esxdeploy"


def test_extract_prefix_from_resource():
    assert extract_prefix_from_resource("jenkins/esxdeploy-7d4f6b8c-xk2mq") == "esxdeploy"
    assert extract_prefix_from_resource("jenkins-agent/esxdeploy-7d4f6b8c-xk2mq") == "esxdeploy"


def test_symptom_class_normalizes_dynamic_values():
    assert symptom_class("Memory at 85% of limit (container: jnlp)") == symptom_class(
        "Memory at 92% of limit (container: jnlp)"
    )
    assert symptom_class("Agent offline: no reason given") == "agent_offline"
    assert symptom_class("Agent temporarily offline: maintenance") == "agent_temporarily_offline"


def test_group_agent_findings_merges_same_prefix_and_issue():
    findings = [
        Finding(
            severity="critical",
            category="jenkins_agent",
            resource="jenkins/esxdeploy-aaa111-xk2mq",
            symptom="Agent offline: connection lost",
            context={"offline_reason": "connection lost"},
        ),
        Finding(
            severity="critical",
            category="jenkins_agent",
            resource="jenkins/esxdeploy-bbb222-yl3nr",
            symptom="Agent offline: connection lost",
            context={"offline_reason": "connection lost"},
        ),
        Finding(
            severity="critical",
            category="jenkins_agent",
            resource="jenkins/otheragent-ccc333-zm4ps",
            symptom="Agent offline: connection lost",
            context={"offline_reason": "connection lost"},
        ),
    ]

    grouped = group_agent_findings(findings)
    assert len(grouped) == 2

    esxdeploy = next(f for f in grouped if f.resource == "jenkins/esxdeploy")
    assert esxdeploy.context["group_size"] == 2
    assert "2 esxdeploy agents offline" in esxdeploy.symptom
    assert len(esxdeploy.context["affected_agents"]) == 2


def test_group_agent_findings_keeps_single_pod_findings():
    findings = [
        Finding(
            severity="warning",
            category="jenkins_agent",
            resource="jenkins/esxdeploy-aaa111-xk2mq",
            symptom="5 restarts (container: jnlp)",
            context={"restart_count": 5},
        ),
    ]
    grouped = group_agent_findings(findings)
    assert len(grouped) == 1
    assert grouped[0].context.get("grouped") is None


def test_group_agent_findings_does_not_merge_different_symptoms():
    findings = [
        Finding(
            severity="critical",
            category="jenkins_agent",
            resource="jenkins/esxdeploy-aaa111-xk2mq",
            symptom="OOMKilled (container: jnlp)",
            context={},
        ),
        Finding(
            severity="critical",
            category="jenkins_agent",
            resource="jenkins/esxdeploy-bbb222-yl3nr",
            symptom="CrashLoopBackOff (container: jnlp)",
            context={},
        ),
    ]
    grouped = group_agent_findings(findings)
    assert len(grouped) == 2


def test_group_agent_findings_leaves_non_agent_findings_untouched():
    findings = [
        Finding(
            severity="warning",
            category="k8s_workload",
            resource="jenkins/jenkins-agent",
            symptom="Deployment has 1 unavailable replica(s) (2/3 ready)",
            context={},
        ),
        Finding(
            severity="critical",
            category="jenkins_agent",
            resource="jenkins/esxdeploy-aaa111-xk2mq",
            symptom="Agent offline: no reason given",
            context={},
        ),
        Finding(
            severity="critical",
            category="jenkins_agent",
            resource="jenkins/esxdeploy-bbb222-yl3nr",
            symptom="Agent offline: no reason given",
            context={},
        ),
    ]
    grouped = group_agent_findings(findings)
    assert len(grouped) == 2
    assert any(f.category == "k8s_workload" for f in grouped)


def test_job_name_from_build_url():
    url = "https://jenkins.example.com/job/cloudsync/3052/"
    assert _job_name_from_build_url(url) == "cloudsync"

    folder_url = "https://jenkins.example.com/job/folder/job/myjob/42/"
    assert _job_name_from_build_url(folder_url) == "folder/myjob"
