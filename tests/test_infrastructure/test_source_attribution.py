from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from jenkins_watchdog.domain.jenkins import JenkinsJobSnapshot
from jenkins_watchdog.domain.source import (
    SourceAttribution,
    SourceKind,
    SourceProfile,
    SourceProfileRegistry,
    SourceStatus,
)
from jenkins_watchdog.infrastructure.source_attribution import (
    JenkinsSourceAttributor,
    ScmSourceVerifier,
    parse_change_url,
    parse_repository_url,
)

NOW = datetime(2026, 7, 15, 18, 0, tzinfo=timezone.utc)


def _job() -> JenkinsJobSnapshot:
    return JenkinsJobSnapshot(
        full_name="Portal_Build_DAILY_MR_PATCH",
        display_name="Portal Build",
        url="https://jenkins/job/Portal_Build_DAILY_MR_PATCH/",
        job_class="org.jenkinsci.plugins.workflow.job.WorkflowJob",
        color="red",
        parent_full_name=None,
    )


def _registry(*profiles: SourceProfile) -> SourceProfileRegistry:
    return SourceProfileRegistry(1, profiles)


def _profile(job: str = "MR_Trigger", repository: str = "Portal/Backend") -> SourceProfile:
    return SourceProfile(
        id="portal",
        root_job_regexes=(f"^{job}$",),
        provider="gitlab",
        primary_repository=repository,
        allowed_repositories=(repository,),
        allow_mr_comments=True,
    )


@pytest.mark.parametrize(
    ("url", "hint", "expected"),
    [
        (
            "http://git.ctera.local/Portal/Backend/merge_requests/6836",
            "gitlab",
            ("gitlab", "Portal/Backend", "6836"),
        ),
        (
            "http://git.ctera.local/groups/platform/service/-/merge_requests/42",
            "gitlab",
            ("gitlab", "groups/platform/service", "42"),
        ),
        (
            "https://github.com/ctera/portal/pull/154",
            None,
            ("github", "ctera/portal", "154"),
        ),
    ],
)
def test_parses_provider_change_urls(url: str, hint: str | None, expected: tuple[str, str, str]) -> None:
    parsed = parse_change_url(url, provider_hint=hint)
    assert parsed is not None and parsed[:3] == expected


def test_parses_repository_urls_without_losing_nested_gitlab_groups() -> None:
    assert parse_repository_url(
        "http://git.ctera.local/group/platform/repo.git",
        provider_hint="gitlab",
    ) == (
        "gitlab",
        "group/platform/repo",
        "http://git.ctera.local/group/platform/repo",
    )
    assert parse_repository_url(
        "git@git.ctera.local:Portal/Backend.git",
        provider_hint="gitlab",
    ) == (
        "gitlab",
        "Portal/Backend",
        "https://git.ctera.local/Portal/Backend",
    )


@pytest.mark.asyncio
async def test_profiled_gitlab_webhook_resolves_exact_live_mr_link() -> None:
    attributor = JenkinsSourceAttributor(_registry(_profile()))
    payload = {
        "actions": [
            {
                "causes": [
                    {
                        "_class": "com.dabsquared.gitlabjenkins.cause.GitLabWebHookCause",
                        "shortDescription": (
                            'Triggered by <a href="http://git.ctera.local/Portal/Backend/'
                            'merge_requests/6836">GitLab Merge Request #6836</a>'
                        ),
                    }
                ]
            }
        ]
    }

    source = await attributor.resolve(
        root_job="MR_Trigger",
        root_build_number=18156,
        trigger_kind="gitlab_webhook",
        root_payload=payload,
        job_source=_job(),
        root_url="https://jenkins/job/MR_Trigger/18156/",
        details_available=True,
    )

    assert source.kind is SourceKind.CHANGE_REQUEST
    assert source.status is SourceStatus.RESOLVED
    assert source.repository == "Portal/Backend"
    assert source.change_number == "6836"
    assert source.profile_id == "portal"
    assert source.allow_mr_comments is True
    assert source.resolution_method == "root_cause_url"


@pytest.mark.asyncio
async def test_profile_boundary_marks_mismatched_repository_as_conflict() -> None:
    attributor = JenkinsSourceAttributor(_registry(_profile()))
    source = await attributor.resolve(
        root_job="MR_Trigger",
        root_build_number=18156,
        trigger_kind="gitlab_webhook",
        root_payload={
            "actions": [
                {
                    "causes": [
                        {
                            "_class": "GitLabWebHookCause",
                            "shortDescription": "http://git.ctera.local/PIM/Image/-/merge_requests/42",
                        }
                    ]
                }
            ]
        },
        job_source=_job(),
        root_url="https://jenkins/job/MR_Trigger/18156/",
        details_available=True,
    )

    assert source.kind is SourceKind.CHANGE_REQUEST
    assert source.status is SourceStatus.CONFLICT
    assert source.repository == "PIM/Image"
    assert source.reason == "source_profile_mismatch"


@pytest.mark.asyncio
async def test_distinct_change_numbers_in_one_root_execution_are_a_conflict() -> None:
    attributor = JenkinsSourceAttributor(_registry(_profile()))
    source = await attributor.resolve(
        root_job="MR_Trigger",
        root_build_number=18156,
        trigger_kind="gitlab_webhook",
        root_payload={
            "actions": [
                {
                    "causes": [
                        {
                            "_class": "GitLabWebHookCause",
                            "description": (
                                "http://git.ctera.local/Portal/Backend/-/merge_requests/6836 "
                                "http://git.ctera.local/Portal/Backend/-/merge_requests/6837"
                            ),
                        }
                    ]
                }
            ]
        },
        job_source=_job(),
        root_url="https://jenkins/job/MR_Trigger/18156/",
        details_available=True,
    )

    assert source.status is SourceStatus.CONFLICT
    assert source.reason == "conflicting_change_candidates"


@pytest.mark.asyncio
async def test_profile_selects_primary_product_repository_from_multi_checkout_push() -> None:
    attributor = JenkinsSourceAttributor(_registry(_profile("Genesis9_Build_NAS_RPMs", "Appliance/App")))
    payload = {
        "actions": [
            {
                "causes": [
                    {
                        "_class": "com.dabsquared.gitlabjenkins.cause.GitLabWebHookCause",
                        "shortDescription": "Started by GitLab push by jenkins",
                    }
                ]
            },
            {
                "remoteUrls": ["http://git.ctera.local/Appliance/App.git"],
                "lastBuiltRevision": {
                    "SHA1": "abc123",
                    "branch": [{"name": "refs/remotes/origin/dev"}],
                },
            },
            {
                "remoteUrls": ["http://git.ctera.local/genesis/cicd.git"],
                "lastBuiltRevision": {
                    "SHA1": "def456",
                    "branch": [{"name": "refs/remotes/origin/master"}],
                },
            },
        ]
    }

    source = await attributor.resolve(
        root_job="Genesis9_Build_NAS_RPMs",
        root_build_number=19219,
        trigger_kind="gitlab_webhook",
        root_payload=payload,
        job_source=_job(),
        root_url="https://jenkins/job/Genesis9_Build_NAS_RPMs/19219/",
        details_available=True,
    )

    assert source.kind is SourceKind.REPOSITORY_REVISION
    assert source.repository == "Appliance/App"
    assert source.branch == "dev"
    assert source.commit_sha == "abc123"


@pytest.mark.asyncio
async def test_registered_push_remains_attributed_when_failure_precedes_checkout() -> None:
    attributor = JenkinsSourceAttributor(_registry(_profile("PortalSCMPoll")))
    payload = {
        "actions": [
            {
                "causes": [
                    {
                        "_class": "com.dabsquared.gitlabjenkins.cause.GitLabWebHookCause",
                        "shortDescription": "Started by GitLab push by Asaf Avron",
                    }
                ]
            }
        ]
    }

    source = await attributor.resolve(
        root_job="PortalSCMPoll",
        root_build_number=1813,
        trigger_kind="gitlab_webhook",
        root_payload=payload,
        job_source=_job(),
        root_url="https://jenkins/job/PortalSCMPoll/1813/",
        details_available=True,
    )

    assert source.kind is SourceKind.REPOSITORY_REVISION
    assert source.repository == "Portal/Backend"
    assert source.reason == "profile_repository_without_revision"


@pytest.mark.asyncio
async def test_profile_reconciles_change_identity_from_jenkins_parameters() -> None:
    attributor = JenkinsSourceAttributor(_registry(_profile()))
    source = await attributor.resolve(
        root_job="MR_Trigger",
        root_build_number=18156,
        trigger_kind="gitlab_webhook",
        root_payload={
            "actions": [
                {
                    "parameters": [
                        {"name": "MR_IID", "value": "6836"},
                        {"name": "SOURCE_BRANCH", "value": "PIM-7623-av"},
                    ]
                }
            ]
        },
        job_source=_job(),
        root_url="https://jenkins/job/MR_Trigger/18156/",
        details_available=True,
    )

    assert source.kind is SourceKind.CHANGE_REQUEST
    assert source.status is SourceStatus.RESOLVED
    assert source.repository == "Portal/Backend"
    assert source.change_number == "6836"
    assert source.branch == "PIM-7623-av"
    assert source.resolution_method == "root_parameter_fields"


@pytest.mark.asyncio
async def test_profile_reconciles_repository_revision_from_jenkins_parameters() -> None:
    attributor = JenkinsSourceAttributor(_registry(_profile(job="PortalSCMPoll")))
    source = await attributor.resolve(
        root_job="PortalSCMPoll",
        root_build_number=1813,
        trigger_kind="scm_poll",
        root_payload={
            "actions": [
                {
                    "parameters": [
                        {"name": "GIT_URL", "value": "http://git.ctera.local/Portal/Backend.git"},
                        {"name": "GIT_BRANCH", "value": "origin/main"},
                        {"name": "GIT_COMMIT", "value": "444e7bd"},
                    ]
                }
            ]
        },
        job_source=_job(),
        root_url="https://jenkins/job/PortalSCMPoll/1813/",
        details_available=True,
    )

    assert source.kind is SourceKind.REPOSITORY_REVISION
    assert source.status is SourceStatus.RESOLVED
    assert source.repository == "Portal/Backend"
    assert source.branch == "main"
    assert source.commit_sha == "444e7bd"
    assert source.resolution_method == "jenkins_parameters"


@pytest.mark.asyncio
async def test_non_scm_execution_is_a_pipeline_source_instead_of_unknown() -> None:
    attributor = JenkinsSourceAttributor(_registry())
    source = await attributor.resolve(
        root_job="DeployGenesisAndRunSyncTests",
        root_build_number=14276,
        trigger_kind="manual",
        root_payload={"actions": [{"causes": [{"_class": "hudson.model.Cause$UserIdCause"}]}]},
        job_source=_job(),
        root_url="https://jenkins/job/DeployGenesisAndRunSyncTests/14276/",
        details_available=True,
    )

    assert source.kind is SourceKind.PIPELINE
    assert source.status is SourceStatus.RESOLVED
    assert source.provider == "jenkins"


@pytest.mark.asyncio
async def test_gitlab_verifier_confirms_and_enriches_change_identity() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["PRIVATE-TOKEN"] == "secret"
        return httpx.Response(
            200,
            json={
                "iid": 6836,
                "title": "PIM-7623 - infected files are missing device_id",
                "state": "opened",
                "web_url": "http://git.ctera.local/Portal/Backend/-/merge_requests/6836",
                "source_branch": "PIM-7623-av",
                "sha": "444e7bd",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        verifier = ScmSourceVerifier(
            client,
            now=lambda: NOW,
            github_api_url="https://api.github.com",
            github_token="",
            gitlab_api_url="http://git.ctera.local/api/v4",
            gitlab_token="secret",
        )
        attributor = JenkinsSourceAttributor(_registry(_profile()), verifier)
        source = await attributor.resolve(
            root_job="MR_Trigger",
            root_build_number=18156,
            trigger_kind="gitlab_webhook",
            root_payload={
                "actions": [
                    {
                        "causes": [
                            {
                                "_class": "GitLabWebHookCause",
                                "shortDescription": (
                                    "http://git.ctera.local/Portal/Backend/merge_requests/6836"
                                ),
                            }
                        ]
                    }
                ]
            },
            job_source=_job(),
            root_url="https://jenkins/job/MR_Trigger/18156/",
            details_available=True,
        )

    assert source.status is SourceStatus.VERIFIED
    assert source.title == "PIM-7623 - infected files are missing device_id"
    assert source.branch == "PIM-7623-av"
    assert source.commit_sha == "444e7bd"
    assert source.verified_at == NOW


@pytest.mark.asyncio
async def test_gitlab_verifier_uses_branch_when_revision_sha_is_missing() -> None:
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "id": "abcdef1234567890",
                "title": "Branch head",
                "web_url": "http://git.ctera.local/Portal/Backend/-/commit/abcdef1234567890",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        verifier = ScmSourceVerifier(
            client,
            now=lambda: NOW,
            github_api_url="https://api.github.com",
            github_token="",
            gitlab_api_url="http://git.ctera.local/api/v4",
            gitlab_token="secret",
        )
        source = await verifier.verify(
            SourceAttribution(
                kind=SourceKind.REPOSITORY_REVISION,
                status=SourceStatus.RESOLVED,
                provider="gitlab",
                repository="Portal/Backend",
                branch="feature/source-attribution",
            )
        )

    assert requested_urls == [
        "http://git.ctera.local/api/v4/projects/Portal%2FBackend/repository/commits/feature%2Fsource-attribution"
    ]
    assert source.status is SourceStatus.VERIFIED
    assert source.commit_sha == "abcdef1234567890"


@pytest.mark.asyncio
async def test_provider_not_found_is_a_visible_conflict() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(404, request=request))
    ) as client:
        verifier = ScmSourceVerifier(
            client,
            now=lambda: NOW,
            github_api_url="https://api.github.com",
            github_token="",
            gitlab_api_url="http://git.ctera.local/api/v4",
            gitlab_token="secret",
        )
        source = await verifier.verify(
            SourceAttribution(
                kind=SourceKind.CHANGE_REQUEST,
                status=SourceStatus.RESOLVED,
                provider="gitlab",
                repository="Portal/Backend",
                change_number="6836",
                profile_id="portal-backend",
                allow_mr_comments=True,
            )
        )

    assert source.status is SourceStatus.CONFLICT
    assert source.reason == "provider_record_not_found"
    assert source.allow_mr_comments is True
