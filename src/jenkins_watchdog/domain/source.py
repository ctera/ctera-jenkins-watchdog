"""Normalized source attribution and versioned Jenkins source contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping


class SourceKind(StrEnum):
    CHANGE_REQUEST = "change_request"
    REPOSITORY_REVISION = "repository_revision"
    PIPELINE = "pipeline"
    UNRESOLVED = "unresolved"


class SourceStatus(StrEnum):
    PENDING = "pending"
    RESOLVED = "resolved"
    VERIFIED = "verified"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True)
class SourceProfile:
    id: str
    root_job_regexes: tuple[str, ...]
    provider: str
    primary_repository: str | None = None
    allowed_repositories: tuple[str, ...] = ()
    allow_mr_comments: bool = False

    def matches(self, root_job: str) -> bool:
        return any(re.search(pattern, root_job) for pattern in self.root_job_regexes)

    def allows_repository(self, repository: str) -> bool:
        expected = self.allowed_repositories or (
            (self.primary_repository,) if self.primary_repository else ()
        )
        return not expected or repository in expected


@dataclass(frozen=True, slots=True)
class SourceProfileRegistry:
    version: int
    profiles: tuple[SourceProfile, ...]

    def match(self, root_job: str) -> SourceProfile | None:
        return next((profile for profile in self.profiles if profile.matches(root_job)), None)


@dataclass(frozen=True, slots=True)
class SourceAttribution:
    kind: SourceKind
    status: SourceStatus
    provider: str | None = None
    repository: str | None = None
    change_number: str | None = None
    url: str | None = None
    branch: str | None = None
    commit_sha: str | None = None
    title: str | None = None
    state: str | None = None
    profile_id: str | None = None
    allow_mr_comments: bool = False
    resolution_method: str = "none"
    reason: str | None = None
    verified_at: datetime | None = None
    provenance: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)

    @property
    def registered(self) -> bool:
        return self.profile_id is not None

    @property
    def complete_change(self) -> bool:
        return (
            self.kind is SourceKind.CHANGE_REQUEST
            and self.provider in {"github", "gitlab"}
            and bool(self.repository and self.change_number)
        )

    def evidence(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "status": self.status.value,
            "provider": self.provider,
            "repository": self.repository,
            "change_number": self.change_number,
            "url": self.url,
            "branch": self.branch,
            "commit_sha": self.commit_sha,
            "title": self.title,
            "state": self.state,
            "profile_id": self.profile_id,
            "profile_registered": self.registered,
            "allow_mr_comments": self.allow_mr_comments,
            "resolution_method": self.resolution_method,
            "reason": self.reason,
            "verified_at": self.verified_at.isoformat() if self.verified_at else None,
            "provenance": [dict(item) for item in self.provenance],
        }
