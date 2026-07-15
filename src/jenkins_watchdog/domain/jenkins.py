"""Jenkins catalog and build observation value objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping

from jenkins_watchdog.domain.source import SourceAttribution, SourceKind, SourceStatus


class JenkinsHeadType(StrEnum):
    BRANCH = "branch"
    CHANGE_REQUEST = "change_request"
    TAG = "tag"
    UNKNOWN = "unknown"


class JenkinsCoverage(StrEnum):
    EXACT = "exact"
    JOB_STARTED_IN_WINDOW = "job_started_in_window"
    RETENTION_LIMITED = "retention_limited"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"


class JenkinsNovelty(StrEnum):
    NEW_FAILURE = "new_failure"
    NEW_REGRESSION = "new_regression"
    RECURRING = "recurring"
    FLAKY = "flaky"
    PROPAGATED = "propagated"
    UNCLASSIFIED = "unclassified"


@dataclass(frozen=True, slots=True)
class JenkinsJobSnapshot:
    full_name: str
    display_name: str
    url: str
    job_class: str
    color: str | None
    parent_full_name: str | None
    first_build_number: int | None = None
    first_build_at: datetime | None = None
    last_build_number: int | None = None
    last_build_at: datetime | None = None
    head_type: JenkinsHeadType = JenkinsHeadType.UNKNOWN
    head_name: str | None = None
    source_provider: str | None = None
    repository: str | None = None
    source_url: str | None = None

    @property
    def is_container(self) -> bool:
        return self.job_class.endswith("Folder") or self.job_class.endswith("WorkflowMultiBranchProject")

    @property
    def job_type(self) -> str:
        if self.job_class.endswith("WorkflowMultiBranchProject"):
            return "multibranch"
        if self.job_class.endswith("Folder"):
            return "folder"
        if self.job_class.endswith("WorkflowJob"):
            return "pipeline"
        if self.job_class.endswith("FreeStyleProject"):
            return "freestyle"
        if self.job_class.endswith("MultiJobProject"):
            return "multi_job"
        return "other"


@dataclass(frozen=True, slots=True)
class JenkinsBuildSnapshot:
    job_full_name: str
    number: int
    result: str
    url: str
    started_at: datetime
    duration_ms: int
    building: bool = False
    enrichment_status: str = "pending"
    source_status: str = "pending"
    logical_run_key: str | None = None

    @property
    def failure_like(self) -> bool:
        return self.result in {"FAILURE", "UNSTABLE", "ABORTED"}


@dataclass(frozen=True, slots=True)
class JenkinsBuildEnrichment:
    job_full_name: str
    number: int
    attribution: "JenkinsBuildAttribution"
    failed_stage: str | None = None
    failure_classification: str = "unknown"
    failure_signature: str = ""
    failure_summary: str | None = None
    propagated_failure: bool = False
    error_lines: tuple[str, ...] = ()
    stage_evidence: tuple[Mapping[str, Any], ...] = ()
    log_enriched: bool = False

    @property
    def logical_run_key(self) -> str:
        return self.attribution.logical_run_key

    @property
    def upstream_job_full_name(self) -> str | None:
        return self.attribution.upstream_job_full_name

    @property
    def upstream_build_number(self) -> int | None:
        return self.attribution.upstream_build_number

    @property
    def root_job_full_name(self) -> str | None:
        return self.attribution.root_job_full_name

    @property
    def root_build_number(self) -> int | None:
        return self.attribution.root_build_number

    @property
    def trigger_kind(self) -> str:
        return self.attribution.trigger_kind

    @property
    def source_provider(self) -> str | None:
        return self.attribution.source.provider

    @property
    def repository(self) -> str | None:
        return self.attribution.source.repository

    @property
    def change_number(self) -> str | None:
        return self.attribution.source.change_number

    @property
    def change_url(self) -> str | None:
        return self.attribution.source.url

    @property
    def head_name(self) -> str | None:
        return self.attribution.head_name

    @property
    def cause_evidence(self) -> tuple[Mapping[str, Any], ...]:
        return self.attribution.cause_evidence


@dataclass(frozen=True, slots=True)
class JenkinsBuildAttribution:
    job_full_name: str
    number: int
    upstream_job_full_name: str | None = None
    upstream_build_number: int | None = None
    root_job_full_name: str | None = None
    root_build_number: int | None = None
    trigger_kind: str = "unknown"
    source: SourceAttribution = field(
        default_factory=lambda: SourceAttribution(SourceKind.UNRESOLVED, SourceStatus.PENDING)
    )
    head_name: str | None = None
    cause_evidence: tuple[Mapping[str, Any], ...] = ()

    @property
    def logical_run_key(self) -> str:
        job = self.root_job_full_name or self.job_full_name
        number = self.root_build_number or self.number
        return f"{job}#{number}"


@dataclass(frozen=True, slots=True)
class JenkinsBuildHistoryPage:
    builds: tuple[JenkinsBuildSnapshot, ...]
    coverage: JenkinsCoverage


@dataclass(frozen=True, slots=True)
class JenkinsSyncStats:
    started_at: datetime
    completed_at: datetime
    cutoff_at: datetime
    jobs_discovered: int
    active_jobs: int
    builds_observed: int
    new_builds: int
    enriched_builds: int
    exact_jobs: int
    retention_limited_jobs: int
    errors: tuple[str, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)
