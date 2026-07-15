"""Ports implemented by v2 infrastructure adapters."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Any, Protocol, Self

from jenkins_watchdog.application.types import (
    CursorPage,
    EnqueueScan,
    ReasoningReply,
    ScanEvent,
    TriageBatchResult,
    TriageCandidate,
)
from jenkins_watchdog.domain.jenkins import (
    JenkinsBuildEnrichment,
    JenkinsBuildHistoryPage,
    JenkinsBuildSnapshot,
    JenkinsJobSnapshot,
    JenkinsSyncStats,
)
from jenkins_watchdog.domain.model import (
    Action,
    AnalysisDecision,
    CheckResult,
    DeliveryAttempt,
    FindingObservation,
    Incident,
    Investigation,
    InvestigationBudgetKind,
    InvestigationRequest,
    LLMCall,
    Scan,
    ScanMode,
)


class ScanRepository(Protocol):
    async def lock_enqueue(self) -> None: ...

    async def active(self) -> Scan | None: ...

    async def add(self, request: EnqueueScan) -> Scan: ...

    async def get(self, scan_id: str) -> Scan | None: ...

    async def list(self, *, limit: int, cursor: str | None = None) -> CursorPage: ...

    async def latest_completed(self) -> Scan | None: ...

    async def claim(self, *, owner: str, now: datetime, lease_seconds: int) -> Scan | None: ...

    async def heartbeat(self, scan_id: str, *, owner: str, now: datetime, lease_seconds: int) -> bool: ...

    async def request_cancel(self, scan_id: str, *, now: datetime) -> tuple[Scan, bool] | None: ...

    async def save(self, scan: Scan) -> None: ...


class CheckExecutionRepository(Protocol):
    async def get(self, scan_id: str, check_name: str) -> CheckResult | None: ...

    async def save(self, scan_id: str, result: CheckResult) -> None: ...

    async def for_scan(self, scan_id: str) -> tuple[CheckResult, ...]: ...


class FindingRepository(Protocol):
    async def add_observations(self, scan_id: str, observations: tuple[FindingObservation, ...]) -> None: ...

    async def unlinked_for_scan(self, scan_id: str) -> tuple[FindingObservation, ...]: ...


class IncidentRepository(Protocol):
    async def get(self, incident_id: str) -> Incident | None: ...

    async def get_by_correlation(self, rule_id: str, key: str) -> Incident | None: ...

    async def list(
        self,
        *,
        limit: int,
        cursor: str | None = None,
        status: str | None = None,
        severity: str | None = None,
        source_type: str | None = None,
    ) -> CursorPage: ...

    async def active(self) -> tuple[Incident, ...]: ...

    async def observed_ids_for_scan(self, scan_id: str) -> frozenset[str]: ...

    async def observations(self, incident_id: str) -> tuple[FindingObservation, ...]: ...

    async def current_observations(self, incident_id: str) -> tuple[FindingObservation, ...]: ...

    async def save(self, incident: Incident) -> None: ...

    async def link_observation(self, incident: Incident, observation: FindingObservation) -> None: ...


class InvestigationRepository(Protocol):
    async def latest_for_incident(self, incident_id: str) -> Investigation | None: ...

    async def save(self, investigation: Investigation) -> None: ...


class InvestigationRequestRepository(Protocol):
    async def get(self, request_id: str) -> InvestigationRequest | None: ...

    async def active_for_incident(self, incident_id: str) -> InvestigationRequest | None: ...

    async def latest_for_incident(self, incident_id: str) -> InvestigationRequest | None: ...

    async def enqueue(self, request: InvestigationRequest) -> InvestigationRequest: ...

    async def lock_budget(self) -> None: ...

    async def active_reserved_tokens(self, *, budget_kind: InvestigationBudgetKind | None = None) -> int: ...

    async def claim(self, *, owner: str, now: datetime, lease_seconds: int) -> InvestigationRequest | None: ...

    async def heartbeat(self, request_id: str, *, owner: str, now: datetime, lease_seconds: int) -> bool: ...

    async def save(self, request: InvestigationRequest) -> None: ...


class AnalysisDecisionRepository(Protocol):
    async def latest_for_incident(self, incident_id: str) -> AnalysisDecision | None: ...

    async def for_incident(self, incident_id: str, *, limit: int = 50) -> tuple[AnalysisDecision, ...]: ...

    async def save(self, decision: AnalysisDecision) -> None: ...


class LLMCallRepository(Protocol):
    async def save_many(self, calls: tuple[LLMCall, ...]) -> None: ...

    async def for_investigation(self, investigation_id: str) -> tuple[LLMCall, ...]: ...

    async def summary_since(
        self,
        since: datetime,
        *,
        budget_kind: InvestigationBudgetKind | None = None,
    ) -> dict[str, Any]: ...

    async def summary_for_scan(self, scan_id: str) -> dict[str, Any]: ...


class ActionRepository(Protocol):
    async def get(self, action_id: str) -> Action | None: ...

    async def add(self, action: Action) -> Action: ...

    async def list(
        self,
        *,
        limit: int,
        cursor: str | None = None,
        status: str | None = None,
        action_type: str | None = None,
        incident_id: str | None = None,
    ) -> CursorPage: ...

    async def for_incident(self, incident_id: str) -> tuple[Action, ...]: ...

    async def claim(self, *, owner: str, now: datetime, lease_seconds: int) -> Action | None: ...

    async def heartbeat(self, action_id: str, *, owner: str, now: datetime, lease_seconds: int) -> bool: ...

    async def save(self, action: Action) -> None: ...


class DeliveryAttemptRepository(Protocol):
    async def save(self, attempt: DeliveryAttempt) -> None: ...

    async def for_action(self, action_id: str) -> tuple[DeliveryAttempt, ...]: ...


class EventRepository(Protocol):
    async def append(self, scan_id: str, event_type: str, payload: dict[str, Any], *, now: datetime) -> ScanEvent: ...

    async def after(self, scan_id: str, sequence: int, *, limit: int = 500) -> tuple[ScanEvent, ...]: ...


class JenkinsSourcePort(Protocol):
    async def discover_jobs(self) -> tuple[JenkinsJobSnapshot, ...]: ...

    async def enrich_job_source(self, job: JenkinsJobSnapshot) -> JenkinsJobSnapshot: ...

    async def build_history(
        self,
        job: JenkinsJobSnapshot,
        *,
        cutoff: datetime,
        after_number: int | None,
    ) -> JenkinsBuildHistoryPage: ...

    async def enrich_build(self, build: JenkinsBuildSnapshot, *, include_log: bool) -> JenkinsBuildEnrichment: ...


class JenkinsRepository(Protocol):
    async def claim_sync(self, *, owner: str, now: datetime, lease_seconds: int) -> bool: ...

    async def heartbeat_sync(self, *, owner: str, now: datetime, lease_seconds: int) -> bool: ...

    async def complete_sync(self, *, owner: str, stats: JenkinsSyncStats) -> None: ...

    async def fail_sync(self, *, owner: str, now: datetime, summary: str) -> None: ...

    async def upsert_jobs(self, jobs: tuple[JenkinsJobSnapshot, ...], *, now: datetime) -> None: ...

    async def watermarks(self, job_names: tuple[str, ...]) -> dict[str, int | None]: ...

    async def running_build_numbers(self) -> dict[str, tuple[int, ...]]: ...

    async def upsert_builds(
        self,
        builds: tuple[JenkinsBuildSnapshot, ...],
        *,
        now: datetime,
    ) -> int: ...

    async def set_job_coverage(self, job_name: str, coverage: str, *, now: datetime) -> None: ...

    async def pending_enrichment(
        self,
        *,
        limit: int,
        log_limit: int,
    ) -> tuple[JenkinsBuildSnapshot, ...]: ...

    async def save_enrichment(self, enrichment: JenkinsBuildEnrichment, *, now: datetime) -> None: ...

    async def mark_enrichment_failed(self, job_name: str, number: int, *, now: datetime, summary: str) -> None: ...

    async def refresh_classifications(self, *, now: datetime) -> None: ...

    async def sync_status(self) -> dict[str, Any]: ...

    async def jenkins_summary(self, *, since: datetime) -> dict[str, Any]: ...

    async def failure_builds(
        self,
        *,
        since: datetime,
        limit: int,
        cursor: str | None = None,
        novelty: frozenset[str] | None = None,
        job: str | None = None,
        result: str | None = None,
    ) -> CursorPage: ...

    async def logical_executions(self, *, since: datetime, limit: int) -> tuple[dict[str, Any], ...]: ...

    async def recurring_patterns(self, *, since: datetime, limit: int) -> tuple[dict[str, Any], ...]: ...

    async def job_families(self, *, since: datetime, limit: int) -> tuple[dict[str, Any], ...]: ...

    async def multibranch_families(self, *, since: datetime, limit: int) -> tuple[dict[str, Any], ...]: ...

    async def build_detail(self, build_id: str) -> dict[str, Any] | None: ...

    async def builds_for_incident(self, incident_id: str) -> tuple[dict[str, Any], ...]: ...

    async def link_incident(self, build_id: str, incident_id: str) -> None: ...

    async def analysis_candidates(self, *, min_priority: int, limit: int) -> tuple[dict[str, Any], ...]: ...


class UnitOfWork(AbstractAsyncContextManager["UnitOfWork"], Protocol):
    scans: ScanRepository
    checks: CheckExecutionRepository
    findings: FindingRepository
    incidents: IncidentRepository
    investigations: InvestigationRepository
    investigation_requests: InvestigationRequestRepository
    analysis_decisions: AnalysisDecisionRepository
    llm_calls: LLMCallRepository
    actions: ActionRepository
    delivery_attempts: DeliveryAttemptRepository
    events: EventRepository
    jenkins: JenkinsRepository

    async def __aenter__(self) -> Self: ...

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


class UnitOfWorkFactory(Protocol):
    def __call__(self) -> UnitOfWork: ...


class CheckRunner(Protocol):
    @property
    def check_names(self) -> tuple[str, ...]: ...

    def checks_for_categories(self, categories: tuple[str, ...]) -> tuple[str, ...]: ...

    async def run(self, scan_id: str, check_name: str, mode: ScanMode) -> CheckResult: ...


class EventNotifier(Protocol):
    async def publish(self, event: ScanEvent) -> None: ...


class ReasoningProgress(Protocol):
    async def __call__(self, event: dict[str, Any]) -> None: ...


class ReasoningPort(Protocol):
    async def triage_batch(self, candidates: tuple[TriageCandidate, ...]) -> TriageBatchResult: ...

    async def investigate(
        self,
        incident: Incident,
        observations: tuple[FindingObservation, ...],
        *,
        context: dict[str, Any] | None = None,
        mode: ScanMode = ScanMode.REGULAR,
        on_progress: ReasoningProgress | None = None,
    ) -> Investigation: ...

    async def chat(
        self,
        *,
        message: str,
        incident: Incident | None = None,
        context: dict[str, Any] | None = None,
        history: tuple[dict[str, str], ...] = (),
        on_progress: ReasoningProgress | None = None,
    ) -> ReasoningReply: ...


class ActionDeliveryPort(Protocol):
    async def deliver(self, action: Action) -> dict[str, Any]: ...


class PayloadRenderer(Protocol):
    template_version: str

    def render(self, action_type: str, context: dict[str, Any]) -> dict[str, Any]: ...
