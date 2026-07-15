from __future__ import annotations

from pathlib import Path

import pytest

from jenkins_watchdog.infrastructure.source_profiles import (
    InvalidSourceProfileConfig,
    load_source_profiles,
)


def test_loads_versioned_profiles_and_matches_root_jobs(tmp_path: Path) -> None:
    path = tmp_path / "sources.yaml"
    path.write_text(
        """
version: 1
profiles:
  - id: portal
    root_job_regexes: ['^MR_Trigger$']
    provider: gitlab
    primary_repository: Portal/Backend
    allowed_repositories: [Portal/Backend]
    allow_mr_comments: true
""",
        encoding="utf-8",
    )

    registry = load_source_profiles(path)

    profile = registry.match("MR_Trigger")
    assert profile is not None
    assert profile.id == "portal"
    assert profile.allows_repository("Portal/Backend")
    assert profile.allow_mr_comments is True
    assert registry.match("other") is None


@pytest.mark.parametrize(
    "content",
    [
        "version: 2\nprofiles: []\n",
        "version: 1\nprofiles: [{id: duplicate, root_job_regexes: ['['], provider: gitlab}]\n",
        (
            "version: 1\nprofiles:\n"
            "- {id: duplicate, root_job_regexes: ['x'], provider: gitlab}\n"
            "- {id: duplicate, root_job_regexes: ['y'], provider: gitlab}\n"
        ),
        (
            "version: 1\nprofiles:\n"
            "- id: bad\n  root_job_regexes: ['x']\n  provider: gitlab\n"
            "  primary_repository: a/b\n  allowed_repositories: [c/d]\n"
        ),
    ],
)
def test_rejects_invalid_source_profile_contracts(tmp_path: Path, content: str) -> None:
    path = tmp_path / "sources.yaml"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(InvalidSourceProfileConfig):
        load_source_profiles(path)


def test_repository_source_profiles_cover_known_root_families() -> None:
    registry = load_source_profiles("config/source-profiles.yaml")

    expected = {
        "MR_Trigger": "Portal/Backend",
        "ApplianceAgent_GatedMergeRequest/Appliance_TrigerGitLabMergeReqPipe": "Appliance/App",
        "Automation_GatedMergeRequest": "Automation/Automation",
        "PortalSCMPoll": "Portal/Backend",
        "Portal_Build_portal-env-conf_RPM": "PIM/portal-env-conf",
    }
    assert {
        job: registry.match(job).primary_repository if registry.match(job) else None
        for job in expected
    } == expected
