"""v2 domain entities and value objects."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from uuid import uuid4

from jenkins_watchdog.domain.identity import stable_finding_identity


class Severity(StrEnum):
    CRITICAL = "critical"
    WARNING = "warning"
    LOW = "low"


class ScanMode(StrEnum):
    REGULAR = "regular"
    DEEP = "deep"


class ScanStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ScanStage(StrEnum):
    QUEUED = "queued"
    DETECTING = "detecting"
    FINDINGS_STORED = "findings_stored"
    CORRELATING = "correlating"
    RECONCILING = "reconciling"
    INVESTIGATING = "investigating"
    PLANNING_ACTIONS = "planning_actions"
    COMPLETED = "completed"


class CheckStatus(StrEnum):
    SELECTED = "selected"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    FILTERED = "filtered"


class IncidentStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    SUPPRESSED = "suppressed"


class InvestigationStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class InvestigationRequestStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class AnalysisDecisionOutcome(StrEnum):
    SELECTED = "selected"
    DEFERRED = "deferred"
    REUSED = "reused"
    MANUAL_ONLY = "manual_only"
    BUDGET_DEFERRED = "budget_deferred"


class InvestigationBudgetKind(StrEnum):
    AUTOMATIC = "automatic"
    MANUAL = "manual"


class Confidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ActionType(StrEnum):
    EMAIL = "email"
    JIRA_CREATE = "jira_create"
    JIRA_UPDATE = "jira_update"
    GITHUB_COMMENT = "github_comment"
    GITLAB_COMMENT = "gitlab_comment"


class ActionStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    RETRY_SCHEDULED = "retry_scheduled"
    SUCCEEDED = "succeeded"
    PERMANENTLY_FAILED = "permanently_failed"


class DeliveryAttemptStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    RETRYABLE_FAILED = "retryable_failed"
    PERMANENT_FAILED = "permanent_failed"


@dataclass(frozen=True, slots=True)
class FindingObservation:
    """A per-scan finding observation, before incident correlation."""

    scan_id: str
    check_name: str
    rule_id: str
    resource_id: str
    severity: Severity
    category: str
    summary: str
    observed_at: datetime
    identity_dimensions: Mapping[str, Any] = field(default_factory=dict)
    evidence: Mapping[str, Any] = field(default_factory=dict)
    stable_identity: str = field(init=False)

    def __post_init__(self) -> None:
        frozen_dimensions = _freeze_mapping(self.identity_dimensions)
        frozen_evidence = _freeze_mapping(self.evidence)
        object.__setattr__(self, "identity_dimensions", frozen_dimensions)
        object.__setattr__(self, "evidence", frozen_evidence)
        object.__setattr__(
            self,
            "stable_identity",
            stable_finding_identity(self.rule_id, self.resource_id, frozen_dimensions),
        )


@dataclass(frozen=True, slots=True)
class CheckExecution:
    scan_id: str
    check_name: str
    categories: frozenset[str]
    status: CheckStatus
    started_at: datetime | None = None
    completed_at: datetime | None = None
    failure_summary: str | None = None


@dataclass(frozen=True, slots=True)
class CheckResult:
    scan_id: str
    check_name: str
    status: CheckStatus
    findings: tuple[FindingObservation, ...] = ()
    failure_summary: str | None = None
    categories: frozenset[str] = frozenset()
    summary: Mapping[str, Any] = field(default_factory=dict)
    started_at: datetime | None = None
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "summary", _freeze_mapping(self.summary))

    @classmethod
    def succeeded(
        cls,
        *,
        scan_id: str,
        check_name: str,
        findings: list[FindingObservation] | tuple[FindingObservation, ...],
    ) -> "CheckResult":
        return cls(scan_id=scan_id, check_name=check_name, status=CheckStatus.SUCCEEDED, findings=tuple(findings))


@dataclass(frozen=True, slots=True)
class Scan:
    id: str
    mode: ScanMode
    categories: frozenset[str]
    status: ScanStatus
    created_at: datetime
    stage: ScanStage = ScanStage.QUEUED
    triggering_user_email: str | None = None
    scheduled: bool = False
    cancel_requested_at: datetime | None = None
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    attempt_count: int = 0
    next_attempt_at: datetime | None = None
    failure_summary: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def terminal(self) -> bool:
        return self.status in {ScanStatus.SUCCEEDED, ScanStatus.FAILED, ScanStatus.CANCELLED}

    @property
    def cancellation_requested(self) -> bool:
        return self.cancel_requested_at is not None

    def claim(self, *, owner: str, now: datetime, lease_seconds: int) -> "Scan":
        if self.terminal:
            raise ValueError("terminal scan cannot be claimed")
        return replace(
            self,
            status=ScanStatus.RUNNING,
            lease_owner=owner,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            attempt_count=self.attempt_count + 1,
            started_at=self.started_at or now,
            updated_at=now,
        )

    def heartbeat(self, *, owner: str, now: datetime, lease_seconds: int) -> "Scan":
        if self.lease_owner != owner or self.status is not ScanStatus.RUNNING:
            raise ValueError("scan lease is not owned by worker")
        return replace(self, lease_expires_at=now + timedelta(seconds=lease_seconds), updated_at=now)

    def advance(self, stage: ScanStage, *, now: datetime) -> "Scan":
        if self.terminal:
            return self
        if _SCAN_STAGE_ORDER[stage] < _SCAN_STAGE_ORDER[self.stage]:
            return self
        return replace(self, stage=stage, updated_at=now)

    def request_cancel(self, *, now: datetime) -> "Scan":
        if self.terminal or self.cancel_requested_at is not None:
            return self
        return replace(self, cancel_requested_at=now, updated_at=now)

    def cancel(self, *, now: datetime) -> "Scan":
        if self.terminal:
            return self
        return replace(
            self,
            status=ScanStatus.CANCELLED,
            stage=ScanStage.COMPLETED,
            completed_at=now,
            updated_at=now,
            lease_owner=None,
            lease_expires_at=None,
        )

    def succeed(self, *, now: datetime) -> "Scan":
        return replace(
            self,
            status=ScanStatus.SUCCEEDED,
            stage=ScanStage.COMPLETED,
            completed_at=now,
            updated_at=now,
            lease_owner=None,
            lease_expires_at=None,
            failure_summary=None,
        )

    def fail(self, *, summary: str, now: datetime, retry_at: datetime | None = None) -> "Scan":
        terminal = retry_at is None
        return replace(
            self,
            status=ScanStatus.FAILED if terminal else ScanStatus.QUEUED,
            completed_at=now if terminal else None,
            updated_at=now,
            next_attempt_at=retry_at,
            failure_summary=summary,
            lease_owner=None,
            lease_expires_at=None,
        )


@dataclass(frozen=True, slots=True)
class IncidentOccurrence:
    id: str
    opened_at: datetime
    number: int = 1
    last_observed_at: datetime | None = None
    resolved_at: datetime | None = None
    observation_identities: frozenset[str] = frozenset()
    responsible_checks: frozenset[str] = frozenset()

    @property
    def active(self) -> bool:
        return self.resolved_at is None

    def with_observation(self, observation: FindingObservation) -> "IncidentOccurrence":
        return replace(
            self,
            observation_identities=self.observation_identities | {observation.stable_identity},
            responsible_checks=self.responsible_checks | {observation.check_name},
            last_observed_at=observation.observed_at,
        )

    def resolve(self, resolved_at: datetime) -> "IncidentOccurrence":
        if not self.active:
            return self
        return replace(self, resolved_at=resolved_at)


@dataclass(frozen=True, slots=True)
class Incident:
    """Incident aggregate controlled by deterministic lifecycle transitions."""

    id: str
    correlation_rule_id: str
    correlation_key: str
    status: IncidentStatus
    created_at: datetime
    current_occurrence: IncidentOccurrence
    occurrence_history: tuple[IncidentOccurrence, ...] = ()
    severity: Severity = Severity.LOW
    title: str = ""
    source: MappingProxyType[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    actionability: str | None = None
    classification: str | None = None
    priority: str | None = None
    updated_at: datetime | None = None
    resolved_at: datetime | None = None
    suppressed_reason: str | None = None
    suppressed_by: str | None = None
    suppressed_at: datetime | None = None
    metadata: MappingProxyType[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", _freeze_mapping(self.source))
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata))
        if not self.occurrence_history:
            object.__setattr__(self, "occurrence_history", (self.current_occurrence,))

    @classmethod
    def open_new(
        cls,
        *,
        id: str,
        correlation_rule_id: str,
        correlation_key: str,
        observation: FindingObservation,
        opened_at: datetime,
        title: str | None = None,
    ) -> "Incident":
        occurrence = IncidentOccurrence(
            id=str(uuid4()),
            opened_at=opened_at,
            observation_identities=frozenset({observation.stable_identity}),
            responsible_checks=frozenset({observation.check_name}),
            last_observed_at=observation.observed_at,
        )
        return cls(
            id=id,
            correlation_rule_id=correlation_rule_id,
            correlation_key=correlation_key,
            status=IncidentStatus.OPEN,
            created_at=opened_at,
            current_occurrence=occurrence,
            occurrence_history=(occurrence,),
            severity=observation.severity,
            title=title or observation.summary,
            updated_at=opened_at,
        )

    @property
    def automation_allowed(self) -> bool:
        return self.status is not IncidentStatus.SUPPRESSED

    def observe(self, observation: FindingObservation) -> "Incident":
        occurrence = self.current_occurrence.with_observation(observation)
        status = self.status
        if not self.current_occurrence.active:
            occurrence = IncidentOccurrence(
                id=str(uuid4()),
                opened_at=observation.observed_at,
                number=self.current_occurrence.number + 1,
                last_observed_at=observation.observed_at,
                observation_identities=frozenset({observation.stable_identity}),
                responsible_checks=frozenset({observation.check_name}),
            )
            status = IncidentStatus.SUPPRESSED if self.status is IncidentStatus.SUPPRESSED else IncidentStatus.OPEN
            history = (*self.occurrence_history, occurrence)
            severity = observation.severity
        else:
            history = (*self.occurrence_history[:-1], occurrence)
            severity = max((self.severity, observation.severity), key=_severity_rank)
        return replace(
            self,
            status=status,
            current_occurrence=occurrence,
            occurrence_history=history,
            severity=severity,
            updated_at=observation.observed_at,
            resolved_at=None,
        )

    def reconcile_after_scan(
        self,
        *,
        selected_checks: frozenset[str],
        successful_checks: frozenset[str],
        reconciled_at: datetime,
    ) -> "Incident":
        occurrence = self.current_occurrence
        if not occurrence.active:
            return self
        responsible = occurrence.responsible_checks
        if not responsible.issubset(selected_checks):
            return self
        if not responsible.issubset(successful_checks):
            return self

        resolved = occurrence.resolve(reconciled_at)
        if self.status is IncidentStatus.SUPPRESSED:
            return replace(
                self,
                current_occurrence=resolved,
                occurrence_history=(*self.occurrence_history[:-1], resolved),
                updated_at=reconciled_at,
                resolved_at=reconciled_at,
            )
        return replace(
            self,
            status=IncidentStatus.RESOLVED,
            current_occurrence=resolved,
            occurrence_history=(*self.occurrence_history[:-1], resolved),
            updated_at=reconciled_at,
            resolved_at=reconciled_at,
        )

    def suppress(self, *, reason: str, actor: str, suppressed_at: datetime) -> "Incident":
        if not reason.strip():
            raise ValueError("suppression requires an audit reason")
        if not actor.strip():
            raise ValueError("suppression requires an authenticated actor")
        return replace(
            self,
            status=IncidentStatus.SUPPRESSED,
            suppressed_reason=reason,
            suppressed_by=actor,
            suppressed_at=suppressed_at,
            updated_at=suppressed_at,
        )

    def unsuppress(self) -> "Incident":
        status = IncidentStatus.OPEN if self.current_occurrence.active else IncidentStatus.RESOLVED
        return replace(
            self,
            status=status,
            suppressed_reason=None,
            suppressed_by=None,
            suppressed_at=None,
            resolved_at=None if status is IncidentStatus.OPEN else self.current_occurrence.resolved_at,
        )

    def apply_triage(self, *, actionability: str, classification: str, priority: str, now: datetime) -> "Incident":
        """Apply advisory triage without changing deterministic lifecycle or severity."""
        return replace(
            self,
            actionability=actionability,
            classification=classification,
            priority=priority,
            updated_at=now,
        )

    def associate_source(self, source: Mapping[str, Any], *, now: datetime) -> "Incident":
        return replace(self, source=_freeze_mapping(source), updated_at=now)


@dataclass(frozen=True, slots=True)
class Investigation:
    id: str
    incident_id: str
    occurrence_id: str
    status: InvestigationStatus
    evidence_hash: str
    input_version: str
    prompt_version: str
    model: str
    created_at: datetime
    confidence: Confidence | None = None
    usage: Mapping[str, Any] = field(default_factory=dict)
    result: Mapping[str, Any] = field(default_factory=dict)
    model_calls: tuple["LLMCall", ...] = ()
    error_summary: str | None = None
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "usage", _freeze_mapping(self.usage))
        object.__setattr__(self, "result", _freeze_mapping(self.result))
        object.__setattr__(self, "model_calls", tuple(self.model_calls))


@dataclass(frozen=True, slots=True)
class LLMCall:
    id: str
    purpose: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    total_tokens: int
    created_at: datetime
    estimated_cost_usd: Decimal | None = None
    cost_source: str = "unavailable"
    incident_id: str | None = None
    investigation_id: str | None = None
    scan_id: str | None = None
    budget_kind: InvestigationBudgetKind | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata))


@dataclass(frozen=True, slots=True)
class AnalysisDecision:
    id: str
    incident_id: str
    occurrence_id: str
    outcome: AnalysisDecisionOutcome
    reason_code: str
    reason: str
    source: str
    mode: ScanMode
    priority: int
    evidence_hash: str
    created_at: datetime
    scan_id: str | None = None
    request_id: str | None = None
    llm_call_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata))


@dataclass(frozen=True, slots=True)
class InvestigationRequest:
    """Durable request to investigate one incident occurrence."""

    id: str
    incident_id: str
    occurrence_id: str
    mode: ScanMode
    source: str
    priority: int
    evidence_hash: str
    status: InvestigationRequestStatus
    created_at: datetime
    updated_at: datetime
    budget_kind: InvestigationBudgetKind = InvestigationBudgetKind.AUTOMATIC
    reserved_tokens: int = 0
    scan_id: str | None = None
    build_id: str | None = None
    requested_by: str | None = None
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    attempt_count: int = 0
    next_attempt_at: datetime | None = None
    investigation_id: str | None = None
    error_summary: str | None = None
    completed_at: datetime | None = None

    def claim(self, *, owner: str, now: datetime, lease_seconds: int) -> "InvestigationRequest":
        reclaiming = (
            self.status is InvestigationRequestStatus.RUNNING
            and self.lease_expires_at is not None
            and self.lease_expires_at <= now
        )
        ready = self.status is InvestigationRequestStatus.QUEUED and (
            self.next_attempt_at is None or self.next_attempt_at <= now
        )
        if not ready and not reclaiming:
            raise ValueError("investigation request is not claimable")
        return replace(
            self,
            status=InvestigationRequestStatus.RUNNING,
            lease_owner=owner,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            attempt_count=self.attempt_count + 1,
            updated_at=now,
            next_attempt_at=None,
            error_summary=None,
        )

    def heartbeat(self, *, owner: str, now: datetime, lease_seconds: int) -> "InvestigationRequest":
        if self.status is not InvestigationRequestStatus.RUNNING or self.lease_owner != owner:
            raise ValueError("investigation request lease is not owned by worker")
        return replace(self, lease_expires_at=now + timedelta(seconds=lease_seconds), updated_at=now)

    def succeed(self, investigation_id: str, *, now: datetime) -> "InvestigationRequest":
        return replace(
            self,
            status=InvestigationRequestStatus.SUCCEEDED,
            investigation_id=investigation_id,
            lease_owner=None,
            lease_expires_at=None,
            updated_at=now,
            completed_at=now,
            error_summary=None,
        )

    def fail(self, summary: str, *, now: datetime, retry_at: datetime | None = None) -> "InvestigationRequest":
        return replace(
            self,
            status=(
                InvestigationRequestStatus.QUEUED
                if retry_at is not None
                else InvestigationRequestStatus.FAILED
            ),
            lease_owner=None,
            lease_expires_at=None,
            next_attempt_at=retry_at,
            error_summary=summary[:500],
            updated_at=now,
            completed_at=None if retry_at is not None else now,
        )

    def defer_for_budget(self, summary: str, *, now: datetime, retry_at: datetime) -> "InvestigationRequest":
        """Return a claimed request to the queue without consuming an execution attempt."""
        return replace(
            self,
            status=InvestigationRequestStatus.QUEUED,
            lease_owner=None,
            lease_expires_at=None,
            attempt_count=max(0, self.attempt_count - 1),
            next_attempt_at=retry_at,
            error_summary=summary[:500],
            updated_at=now,
            completed_at=None,
        )


@dataclass(frozen=True, slots=True)
class Action:
    id: str
    incident_id: str
    occurrence_id: str
    action_type: ActionType
    destination: str
    status: ActionStatus
    rendered_payload: Mapping[str, Any]
    template_version: str
    idempotency_key: str
    external_identity: str
    created_at: datetime
    updated_at: datetime
    external_reference: str | None = None
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    attempt_count: int = 0
    retry_cycle: int = 1
    next_attempt_at: datetime | None = None
    failure_summary: str | None = None
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "rendered_payload", _freeze_mapping(self.rendered_payload))

    def claim(self, *, owner: str, now: datetime, lease_seconds: int) -> "Action":
        reclaiming = (
            self.status is ActionStatus.RUNNING and self.lease_expires_at is not None and self.lease_expires_at <= now
        )
        if self.status not in {ActionStatus.PENDING, ActionStatus.RETRY_SCHEDULED} and not reclaiming:
            raise ValueError("action is not claimable")
        return replace(
            self,
            status=ActionStatus.RUNNING,
            lease_owner=owner,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            attempt_count=self.attempt_count + 1,
            updated_at=now,
        )

    def heartbeat(self, *, owner: str, now: datetime, lease_seconds: int) -> "Action":
        if self.lease_owner != owner or self.status is not ActionStatus.RUNNING:
            raise ValueError("action lease is not owned by worker")
        return replace(self, lease_expires_at=now + timedelta(seconds=lease_seconds), updated_at=now)

    def delivered(self, *, external_reference: str | None, now: datetime) -> "Action":
        return replace(
            self,
            status=ActionStatus.SUCCEEDED,
            external_reference=external_reference,
            completed_at=now,
            updated_at=now,
            lease_owner=None,
            lease_expires_at=None,
            failure_summary=None,
        )

    def delivery_failed(self, *, summary: str, now: datetime, retry_at: datetime | None) -> "Action":
        return replace(
            self,
            status=ActionStatus.RETRY_SCHEDULED if retry_at else ActionStatus.PERMANENTLY_FAILED,
            next_attempt_at=retry_at,
            failure_summary=summary,
            completed_at=None if retry_at else now,
            updated_at=now,
            lease_owner=None,
            lease_expires_at=None,
        )

    def manual_retry(self, *, now: datetime) -> "Action":
        if self.status is not ActionStatus.PERMANENTLY_FAILED:
            raise ValueError("only permanently failed actions can be retried")
        return replace(
            self,
            status=ActionStatus.PENDING,
            retry_cycle=self.retry_cycle + 1,
            attempt_count=0,
            next_attempt_at=now,
            failure_summary=None,
            completed_at=None,
            updated_at=now,
        )


@dataclass(frozen=True, slots=True)
class DeliveryAttempt:
    id: str
    action_id: str
    retry_cycle: int
    attempt_number: int
    status: DeliveryAttemptStatus
    started_at: datetime
    response_metadata: Mapping[str, Any] = field(default_factory=dict)
    error_summary: str | None = None
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "response_metadata", _freeze_mapping(self.response_metadata))


_SCAN_STAGE_ORDER = {stage: index for index, stage in enumerate(ScanStage)}


def _severity_rank(severity: Severity) -> int:
    return {Severity.LOW: 0, Severity.WARNING: 1, Severity.CRITICAL: 2}[severity]


def _freeze_mapping(value: Mapping[str, Any]) -> MappingProxyType[str, Any]:
    return MappingProxyType({str(key): _freeze_value(nested) for key, nested in value.items()})


def _freeze_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    if isinstance(value, list | tuple):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, set | frozenset):
        return frozenset(_freeze_value(item) for item in value)
    return value
