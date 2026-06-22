"""Tests for Jenkins agent pod detection helpers."""

from jenkins_watchdog.checks.agent_utils import is_jenkins_agent_pod
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


def test_job_name_from_build_url():
    url = "https://jenkins.example.com/job/cloudsync/3052/"
    assert _job_name_from_build_url(url) == "cloudsync"

    folder_url = "https://jenkins.example.com/job/folder/job/myjob/42/"
    assert _job_name_from_build_url(folder_url) == "folder/myjob"
