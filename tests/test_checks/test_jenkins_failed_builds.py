"""Tests for Jenkins failed build detection."""

from unittest.mock import AsyncMock, patch

import pytest

from jenkins_watchdog.checks.jenkins_failed_builds import JenkinsFailedBuildCheck, _build_symptom, _group_by_job
from jenkins_watchdog.clients.jenkins import FailedBuildSummary, is_mr_job


@pytest.mark.parametrize(
    "name,expected",
    [
        ("ctera-portal-zones-assignment-service-pipeline/PR-34", True),
        ("ApplianceAgent_GatedMergeRequest/MR_Deploy_K3S_Run_Systems_Regression", True),
        ("Automation_GatedMergeRequest", True),
        ("Avi-Genesis9_Create_PIM_For_MR", True),
        ("Centos9_RPMS_Portal_Image_MR_CI", True),
        ("App_Pipeline_new", False),
        ("Agents_Daily", False),
    ],
)
def test_is_mr_job(name: str, expected: bool):
    assert is_mr_job(name) is expected


def test_group_by_job_sorts_newest_first():
    builds = [
        FailedBuildSummary("job-a", 1, "FAILURE", 1000, 100, "url/1", False),
        FailedBuildSummary("job-a", 2, "UNSTABLE", 2000, 200, "url/2", False),
        FailedBuildSummary("job-b", 3, "ABORTED", 3000, 300, "url/3", True),
    ]
    grouped = _group_by_job(builds)
    assert [build.build_number for build in grouped["job-a"]] == [2, 1]
    assert grouped["job-b"][0].build_number == 3


def test_build_symptom_single_and_multiple():
    single = [
        FailedBuildSummary("App_Pipeline_new", 15, "FAILURE", 1000, 100, "url", False),
    ]
    multiple = [
        FailedBuildSummary("App_Pipeline_new", 15, "FAILURE", 1000, 200, "url/15", False),
        FailedBuildSummary("App_Pipeline_new", 14, "UNSTABLE", 1000, 100, "url/14", False),
    ]
    assert _build_symptom("App_Pipeline_new", single) == "Build FAILURE: App_Pipeline_new #15"
    assert "2 failed builds" in _build_symptom("App_Pipeline_new", multiple)


@pytest.mark.asyncio
async def test_failed_build_check_emits_grouped_findings():
    builds = [
        FailedBuildSummary("App_Pipeline_new", 15, "FAILURE", 1000, 200, "url/15", False),
        FailedBuildSummary("App_Pipeline_new", 14, "UNSTABLE", 1000, 100, "url/14", False),
        FailedBuildSummary("Automation_GatedMergeRequest", 9, "FAILURE", 500, 300, "url/9", True),
    ]
    with patch(
        "jenkins_watchdog.checks.jenkins_failed_builds.get_recent_failed_builds",
        new=AsyncMock(return_value=builds),
    ), patch(
        "jenkins_watchdog.checks.jenkins_failed_builds.get_build_console_output",
        new=AsyncMock(return_value="ERROR: test failed\nBUILD FAILED"),
    ):
        findings = await JenkinsFailedBuildCheck().run()

    assert len(findings) == 2
    by_job = {finding.context["job_name"]: finding for finding in findings}
    assert by_job["App_Pipeline_new"].severity == "critical"
    assert len(by_job["App_Pipeline_new"].context["failed_builds"]) == 2
    assert by_job["Automation_GatedMergeRequest"].severity == "critical"
    assert by_job["Automation_GatedMergeRequest"].context["is_mr"] is True
