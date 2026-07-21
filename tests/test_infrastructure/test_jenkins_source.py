from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from jenkins_watchdog.domain.jenkins import (
    JenkinsBuildSnapshot,
    JenkinsCoverage,
    JenkinsHeadType,
    JenkinsJobSnapshot,
)
from jenkins_watchdog.infrastructure import jenkins_source as source_module
from jenkins_watchdog.infrastructure.jenkins_source import JenkinsSourceAdapter

NOW = datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)


def _job(**overrides: Any) -> JenkinsJobSnapshot:
    values = {
        "full_name": "folder/MR-42",
        "display_name": "MR-42",
        "url": "https://jenkins/job/folder/job/MR-42/",
        "job_class": "org.jenkinsci.plugins.workflow.job.WorkflowJob",
        "color": "red",
        "parent_full_name": "folder",
        "first_build_number": 1,
        "first_build_at": NOW - timedelta(days=2),
        "last_build_number": 8,
        "last_build_at": NOW,
    }
    values.update(overrides)
    return JenkinsJobSnapshot(**values)


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://jenkins.example/api/json")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("failed", request=request, response=response)


class CatalogClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def get_json(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append((path, params))
        if path == "/api/json":
            return {
                "jobs": [
                    {
                        "name": "folder",
                        "fullName": "folder",
                        "url": "https://jenkins/job/folder/",
                        "_class": "com.cloudbees.hudson.plugins.folder.Folder",
                        "jobs": [
                            {
                                "name": "MR-42",
                                "url": "https://jenkins/job/folder/job/MR-42/",
                                "color": "red",
                                "_class": "org.jenkinsci.plugins.workflow.job.WorkflowJob",
                                "firstBuild": {"number": "1", "timestamp": int((NOW - timedelta(days=2)).timestamp() * 1000)},
                                "lastBuild": {"number": 8, "timestamp": int(NOW.timestamp() * 1000)},
                            },
                            {"name": "", "fullName": "///"},
                        ],
                    }
                ]
            }
        if path == "/job/folder/job/MR-42/api/json":
            return {
                "property": [
                    {
                        "_class": "jenkins.branch.BranchJobProperty",
                        "branch": {
                            "head": {
                                "_class": "org.jenkinsci.plugins.github_branch_source.PullRequestSCMHead",
                                "name": "PR-42",
                            }
                        },
                    }
                ],
                "actions": [
                    {
                        "_class": "jenkins.scm.api.metadata.ObjectMetadataAction",
                        "objectUrl": "https://github.com/ctera/portal/pull/42",
                    }
                ],
            }
        raise AssertionError(path)


@pytest.mark.asyncio
async def test_discovers_nested_jobs_and_caches_change_source_metadata() -> None:
    client = CatalogClient()
    adapter = JenkinsSourceAdapter(client, hierarchy_depth=2)  # type: ignore[arg-type]

    jobs = await adapter.discover_jobs()
    assert [job.full_name for job in jobs] == ["folder", "folder/MR-42"]
    assert jobs[1].first_build_at == NOW - timedelta(days=2)
    assert jobs[1].last_build_number == 8

    enriched = await adapter.enrich_job_source(jobs[1])
    cached = await adapter.enrich_job_source(_job(color="yellow"))
    assert enriched.head_type is JenkinsHeadType.CHANGE_REQUEST
    assert enriched.source_provider == "github"
    assert enriched.repository == "ctera/portal"
    assert source_module._change_number_from_url(enriched.source_url) == "42"
    assert cached.color == "yellow"
    assert cached.source_url == enriched.source_url
    assert sum(path == "/job/folder/job/MR-42/api/json" for path, _ in client.calls) == 1


@pytest.mark.asyncio
async def test_source_enrichment_degrades_to_original_job_when_jenkins_rejects_metadata() -> None:
    class FailingClient:
        async def get_json(self, path: str, *, params=None):
            del path, params
            raise TimeoutError("metadata timeout")

    adapter = JenkinsSourceAdapter(FailingClient())  # type: ignore[arg-type]
    original = _job()
    assert await adapter.enrich_job_source(original) == original
    assert await adapter.enrich_job_source(original) == original


class HistoryClient:
    def __init__(self, builds: list[dict[str, Any]]) -> None:
        self.builds = builds

    async def get_json(self, path: str, *, params=None):
        del path, params
        return {"allBuilds": self.builds}


class PagedHistoryClient:
    def __init__(self, pages: tuple[list[dict[str, Any]], ...]) -> None:
        self.pages = pages
        self.calls: list[dict[str, Any]] = []

    async def get_json(self, path: str, *, params=None):
        del path
        self.calls.append(params)
        return {"allBuilds": self.pages[len(self.calls) - 1]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("after_number", "first_build", "rows", "expected"),
    [
        (
            5,
            1,
            [
                {"number": 7, "result": None, "building": True, "timestamp": int(NOW.timestamp() * 1000), "duration": -1},
                {"number": 5, "result": "SUCCESS", "timestamp": int(NOW.timestamp() * 1000), "duration": 10},
                {"number": None, "timestamp": None},
            ],
            JenkinsCoverage.NOT_APPLICABLE,
        ),
        (
            None,
            1,
            [{"number": 1, "result": "SUCCESS", "timestamp": int((NOW - timedelta(days=4)).timestamp() * 1000)}],
            JenkinsCoverage.EXACT,
        ),
        (
            None,
            1,
            [{"number": 1, "result": "SUCCESS", "timestamp": int(NOW.timestamp() * 1000)}],
            JenkinsCoverage.JOB_STARTED_IN_WINDOW,
        ),
        (
            None,
            4,
            [{"number": 8, "result": "FAILURE", "timestamp": int(NOW.timestamp() * 1000)}],
            JenkinsCoverage.RETENTION_LIMITED,
        ),
    ],
)
async def test_build_history_reports_explicit_coverage(
    after_number: int | None,
    first_build: int,
    rows: list[dict[str, Any]],
    expected: JenkinsCoverage,
) -> None:
    adapter = JenkinsSourceAdapter(HistoryClient(rows))  # type: ignore[arg-type]
    page = await adapter.build_history(
        _job(first_build_number=first_build),
        cutoff=NOW - timedelta(days=3),
        after_number=after_number,
    )
    assert page.coverage is expected
    if expected is JenkinsCoverage.NOT_APPLICABLE:
        assert page.builds[0].result == "RUNNING"
        assert page.builds[0].building is True
        assert page.builds[0].duration_ms == 0


@pytest.mark.asyncio
async def test_build_history_pages_exhaustively_until_the_fixed_cutoff() -> None:
    def rows(numbers: range, started_at: datetime) -> list[dict[str, Any]]:
        return [
            {
                "number": number,
                "result": "FAILURE" if number % 2 else "SUCCESS",
                "timestamp": int(started_at.timestamp() * 1000),
                "duration": 1_000,
            }
            for number in numbers
        ]

    client = PagedHistoryClient(
        (
            rows(range(250, 150, -1), NOW - timedelta(hours=1)),
            rows(range(150, 50, -1), NOW - timedelta(hours=2)),
            rows(range(50, 0, -1), NOW - timedelta(hours=5)),
        )
    )
    adapter = JenkinsSourceAdapter(client)  # type: ignore[arg-type]

    page = await adapter.build_history(
        _job(first_build_number=1, last_build_number=250),
        cutoff=NOW - timedelta(hours=4),
        after_number=None,
    )

    assert page.coverage is JenkinsCoverage.EXACT
    assert len(page.builds) == 200
    assert page.builds[0].number == 51
    assert page.builds[-1].number == 250
    assert [call["tree"].rsplit("{", 1)[1] for call in client.calls] == [
        "0,100}",
        "100,200}",
        "200,300}",
    ]


class EnrichmentClient:
    async def get_json(self, path: str, *, params=None):
        del params
        if path == "/job/downstream/2/api/json":
            return {
                "actions": [
                    {
                        "causes": [
                            {
                                "_class": "hudson.model.Cause$UpstreamCause",
                                "upstreamProject": "upstream/job",
                                "upstreamBuild": 9,
                            },
                            {"_class": "com.ctera.DownstreamFailureCause"},
                        ]
                    }
                ]
            }
        if path == "/job/upstream/job/job/9/api/json":
            return {
                "actions": [
                    {
                        "causes": [
                            {
                                "_class": "com.dabsquared.gitlabjenkins.cause.GitLabWebHookCause",
                                "data": {
                                    "mergeRequestIid": 77,
                                    "sourceProjectPathWithNamespace": "ctera/portal",
                                    "sourceBranch": "fix/portal",
                                },
                            }
                        ]
                    }
                ]
            }
        if path == "/job/downstream/api/json":
            return {
                "property": [
                    {
                        "_class": "jenkins.branch.BranchJobProperty",
                        "branch": {"head": {"_class": "MergeRequestSCMHead", "name": "MR-77"}},
                    }
                ],
                "actions": [
                    {
                        "_class": "ObjectMetadataAction",
                        "objectUrl": "https://gitlab.example/ctera/portal/merge_requests/77",
                    }
                ],
            }
        if path == "/job/downstream/2/wfapi/describe":
            return {
                "stages": [
                    {"name": "Compile", "status": "SUCCESS", "durationMillis": 10},
                    {"name": "Regression Tests", "status": "FAILED", "durationMillis": 20},
                ]
            }
        raise AssertionError(path)

    async def get_build_console_tail(self, name: str, number: int):
        assert (name, number) == ("downstream", 2)
        return "[2026-07-15T09:00:00Z] Build child/job #7 completed with FAILURE\nFinished: FAILURE"


@pytest.mark.asyncio
async def test_build_enrichment_traces_upstream_change_and_propagated_failure() -> None:
    adapter = JenkinsSourceAdapter(EnrichmentClient())  # type: ignore[arg-type]
    build = JenkinsBuildSnapshot(
        job_full_name="downstream",
        number=2,
        result="FAILURE",
        url="https://jenkins/job/downstream/2/",
        started_at=NOW,
        duration_ms=120_000,
    )
    result = await adapter.enrich_build(build, include_log=True)
    assert result.upstream_job_full_name == "upstream/job"
    assert result.root_job_full_name == "upstream/job"
    assert result.root_build_number == 9
    assert result.trigger_kind == "gitlab_webhook"
    assert result.source_provider == "gitlab"
    assert result.repository == "ctera/portal"
    assert result.change_number == "77"
    assert result.head_name == "fix/portal"
    assert result.failed_stage == "Regression Tests"
    assert result.propagated_failure is True
    assert result.failure_classification == "propagated"
    assert result.failure_summary == "Downstream failure: child/job #7"
    assert result.log_enriched is True
    assert result.stage_evidence[-1]["duration_ms"] == 20


@pytest.mark.asyncio
async def test_expired_build_and_stage_only_failure_have_stable_fallback_evidence() -> None:
    class ExpiredClient:
        def __init__(self, *, expired: bool) -> None:
            self.expired = expired

        async def get_json(self, path: str, *, params=None):
            del params
            if path.endswith("/api/json") and path.count("/") >= 5:
                if self.expired:
                    raise _http_error(404)
                return {"actions": [{"causes": [{"_class": "hudson.model.Cause$UserIdCause"}]}]}
            if path == "/job/retained/api/json":
                return {}
            if path.endswith("/wfapi/describe"):
                if self.expired:
                    raise TimeoutError("stages unavailable")
                return {"stages": [{"name": "Integration Tests", "status": "UNSTABLE"}]}
            raise AssertionError(path)

        async def get_build_console_tail(self, name: str, number: int):
            del name, number
            raise _http_error(404)

    expired = JenkinsSourceAdapter(ExpiredClient(expired=True))  # type: ignore[arg-type]
    aborted = JenkinsBuildSnapshot("retained", 3, "ABORTED", "https://jenkins/job/retained/3/", NOW, 0)
    result = await expired.enrich_build(aborted, include_log=True)
    assert result.failure_classification == "cancelled"
    assert result.failure_summary == "Build details are no longer retained by Jenkins"
    assert result.log_enriched is True

    retained = JenkinsSourceAdapter(ExpiredClient(expired=False))  # type: ignore[arg-type]
    failed = JenkinsBuildSnapshot("retained", 4, "FAILURE", "https://jenkins/job/retained/4/", NOW, 0)
    stage_result = await retained.enrich_build(failed, include_log=False)
    assert stage_result.failure_classification == "test_failure"
    assert stage_result.failure_summary == "Integration Tests finished with FAILURE"
    assert stage_result.trigger_kind == "manual"


@pytest.mark.parametrize(
    ("class_name", "expected"),
    [
        ("PullRequestSCMHead", JenkinsHeadType.CHANGE_REQUEST),
        ("MergeRequestSCMHead", JenkinsHeadType.CHANGE_REQUEST),
        ("TagSCMHead", JenkinsHeadType.TAG),
        ("BranchSCMHead", JenkinsHeadType.BRANCH),
        ("Other", JenkinsHeadType.UNKNOWN),
    ],
)
def test_source_helper_classification(class_name: str, expected: JenkinsHeadType) -> None:
    assert source_module._head_type(class_name) is expected


def test_source_helpers_cover_urls_causes_triggers_and_summary_selection() -> None:
    assert source_module._source_from_url(None) == (None, None, None)
    assert source_module._source_from_url("https://github.com/acme/repo/pull/9") == ("github", "acme/repo", "9")
    assert source_module._source_from_url("https://gitlab.example/group%20x/repo/merge_requests/4") == (
        "gitlab",
        "group x/repo",
        "4",
    )
    assert source_module._source_from_url("https://scm.example/only") == (None, None, None)
    assert source_module._integer("bad") is None
    assert source_module._timestamp(0) is None
    assert source_module._causes({"actions": [None, {"causes": [None, {"_class": "Cause"}]}]}) == [
        {"_class": "Cause"}
    ]
    assert source_module._first_scalar({"outer": [{"IID": 12}]}, ("iid",)) == "12"
    assert source_module._first_scalar([{"other": "x"}], ("iid",)) is None

    trigger_cases = {
        "GitLabWebHookCause": "gitlab_webhook",
        "GitHubPushCause": "github_webhook",
        "TimerTriggerCause": "scheduled",
        "SCMTriggerCause": "scm_poll",
        "UserIdCause": "manual",
        "UpstreamCause": "upstream",
        "Other": "unknown",
    }
    for class_name, expected in trigger_cases.items():
        assert source_module._trigger_kind([{"_class": class_name}]) == expected

    assert "fatal error" in source_module._failure_summary(["fatal error: compiler stopped"], None, "FAILURE", False)
    assert (
        source_module._failure_summary(
            ["[2026-07-15T10:05:25.564Z] [2026-07-15T10:05:25.564Z] tests failed: 12"],
            None,
            "FAILURE",
            False,
        )
        == "tests failed: 12"
    )
    assert source_module._failure_summary(["at pkg.Type.run(Type.java:1)", "useful failure"], None, "FAILURE", False) == "useful failure"
    assert source_module._failure_summary(["Finished: FAILURE"], "Compile", "FAILURE", False) == "Compile finished with FAILURE"
    assert source_module._failure_summary([], None, "FAILURE", True) == "Downstream build failure propagated to this build"
    assert source_module._summary_score("tests failed") == 100
    assert source_module._summary_score("timeout waiting") == 80
    assert source_module._summary_score("generic failure") == 60
    assert source_module._summary_score("all good") == 0
