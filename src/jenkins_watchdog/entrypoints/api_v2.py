"""FastAPI entrypoint for durable v2 scan APIs."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from jenkins_watchdog.application.investigations import DailyLLMBudgetExceeded
from jenkins_watchdog.application.pagination import InvalidCursorError
from jenkins_watchdog.application.reasoning import jenkins_build_observations
from jenkins_watchdog.application.scan_service import (
    EnqueueScanCommand,
    ScanAlreadyActiveError,
    UnknownScanCategoryError,
)
from jenkins_watchdog.application.types import ScanEvent
from jenkins_watchdog.domain.model import (
    Action,
    AnalysisDecision,
    AnalysisDecisionOutcome,
    CheckResult,
    FindingObservation,
    Incident,
    Investigation,
    InvestigationRequest,
    InvestigationRequestStatus,
    InvestigationStatus,
    LLMCall,
    Scan,
    ScanMode,
)
from jenkins_watchdog.domain.serialization import to_primitive

router = APIRouter()

ScanAnalysisStatus = Literal[
    "not_started",
    "selecting",
    "queued",
    "running",
    "complete",
    "complete_with_issues",
    "budget_deferred",
]


class V2ScanRequest(BaseModel):
    mode: Literal["regular", "deep"] = "regular"
    categories: list[str] | None = Field(default=None)


class V2ScanAnalysisItemResponse(BaseModel):
    incident_id: str
    incident_title: str
    severity: str
    outcome: str
    reason_code: str
    reason: str
    request_id: str | None = None
    request_status: str | None = None
    investigation_id: str | None = None
    investigation_status: str | None = None
    error_summary: str | None = None
    completed_at: datetime | None = None


class V2ScanAnalysisResponse(BaseModel):
    status: ScanAnalysisStatus = "not_started"
    candidate_count: int = 0
    selected_count: int = 0
    queued_count: int = 0
    running_count: int = 0
    succeeded_count: int = 0
    partial_count: int = 0
    failed_count: int = 0
    reused_count: int = 0
    deferred_count: int = 0
    manual_only_count: int = 0
    budget_deferred_count: int = 0
    active_count: int = 0
    budget_metric: str | None = None
    budget_reset_at: datetime | None = None
    budget_limit_tokens: int | None = None
    budget_spent_tokens: int | None = None
    budget_projected_tokens: int | None = None
    budget_limit_usd: float | None = None
    budget_spent_usd: float | None = None
    budget_projected_usd: float | None = None
    items: list[V2ScanAnalysisItemResponse] = Field(default_factory=list)


class V2ScanResponse(BaseModel):
    id: str
    status: str
    stage: str
    mode: str
    categories: list[str]
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    cancel_requested_at: datetime | None
    attempt_count: int
    failure_summary: str | None
    urls: dict[str, str]
    coverage_status: str | None = None
    checks: list["V2CheckExecutionResponse"] = Field(default_factory=list)
    llm_usage: dict[str, Any] = Field(default_factory=dict)
    analysis: V2ScanAnalysisResponse = Field(default_factory=V2ScanAnalysisResponse)


class V2CheckExecutionResponse(BaseModel):
    name: str
    status: str
    categories: list[str]
    finding_count: int
    summary: dict[str, Any]
    failure_summary: str | None
    started_at: datetime | None
    completed_at: datetime | None


class V2ScanPage(BaseModel):
    items: list[V2ScanResponse]
    next_cursor: str | None


class V2CancelResponse(BaseModel):
    id: str
    status: str
    cancel_requested: bool


class V2IncidentResponse(BaseModel):
    id: str
    status: str
    severity: str
    title: str
    correlation_rule_id: str
    correlation_key: str
    source: dict[str, Any]
    actionability: str | None
    classification: str | None
    priority: str | None
    created_at: datetime
    updated_at: datetime | None
    resolved_at: datetime | None
    suppressed_reason: str | None
    suppressed_by: str | None
    suppressed_at: datetime | None
    occurrence_number: int
    affected_resource_count: int = 0
    current_observation_count: int = 0
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    domain: str = "unknown"


class V2IncidentPage(BaseModel):
    items: list[V2IncidentResponse]
    next_cursor: str | None


class V2ObservationResponse(BaseModel):
    scan_id: str
    check_name: str
    stable_identity: str
    rule_id: str
    resource_id: str
    category: str
    severity: str
    summary: str
    observed_at: datetime
    identity_dimensions: dict[str, Any]
    evidence: dict[str, Any]


class V2OccurrenceResponse(BaseModel):
    id: str
    number: int
    opened_at: datetime
    last_observed_at: datetime | None
    resolved_at: datetime | None
    responsible_checks: list[str]
    observation_identities: list[str]


class V2InvestigationResponse(BaseModel):
    id: str
    status: str
    evidence_hash: str
    input_version: str
    prompt_version: str
    model: str
    confidence: str | None
    usage: dict[str, Any]
    result: dict[str, Any]
    error_summary: str | None
    created_at: datetime
    completed_at: datetime | None
    model_calls: list["V2LLMCallResponse"] = Field(default_factory=list)


class V2LLMCallResponse(BaseModel):
    id: str
    purpose: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    total_tokens: int
    estimated_cost_usd: float | None
    cost_source: str
    created_at: datetime


class V2AnalysisDecisionResponse(BaseModel):
    id: str
    outcome: str
    reason_code: str
    reason: str
    source: str
    mode: str
    priority: int
    evidence_hash: str
    scan_id: str | None
    request_id: str | None
    created_at: datetime


class V2InvestigationRequestResponse(BaseModel):
    id: str
    incident_id: str
    occurrence_id: str
    mode: str
    source: str
    priority: int
    evidence_hash: str
    status: str
    scan_id: str | None
    build_id: str | None
    requested_by: str | None
    budget_kind: str
    reserved_tokens: int
    attempt_count: int
    next_attempt_at: datetime | None
    investigation_id: str | None
    error_summary: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class V2ActionResponse(BaseModel):
    id: str
    incident_id: str
    occurrence_id: str
    action_type: str
    destination: str
    status: str
    rendered_payload: dict[str, Any]
    template_version: str
    external_reference: str | None
    attempt_count: int
    retry_cycle: int
    next_attempt_at: datetime | None
    failure_summary: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class V2ActionPage(BaseModel):
    items: list[V2ActionResponse]
    next_cursor: str | None


class V2DeliveryAttemptResponse(BaseModel):
    id: str
    retry_cycle: int
    attempt_number: int
    status: str
    response_metadata: dict[str, Any]
    error_summary: str | None
    started_at: datetime
    completed_at: datetime | None


class V2ActionDetailResponse(BaseModel):
    action: V2ActionResponse
    attempts: list[V2DeliveryAttemptResponse]


class V2IncidentDetailResponse(BaseModel):
    incident: V2IncidentResponse
    observations: list[V2ObservationResponse]
    current_observations: list[V2ObservationResponse] = Field(default_factory=list)
    occurrences: list[V2OccurrenceResponse]
    latest_investigation: V2InvestigationResponse | None
    investigation_request: V2InvestigationRequestResponse | None = None
    analysis_decision: V2AnalysisDecisionResponse | None = None
    jenkins_builds: list[dict[str, Any]] = Field(default_factory=list)
    actions: list[V2ActionResponse]


class V2SuppressionRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class V2ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=10000)
    incident_id: str | None = None
    history: list[dict[str, str]] = Field(default_factory=list, max_length=20)


class V2ChatResponse(BaseModel):
    content: str
    references: list[dict[str, str]] = Field(default_factory=list)
    as_of: datetime | None = None
    coverage_status: str = "unknown"


class V2OverviewResponse(BaseModel):
    environment: str
    status: str
    generated_at: datetime
    latest_scan: V2ScanResponse | None
    coverage_status: str
    active_incident_count: int
    critical_incident_count: int
    warning_incident_count: int
    affected_resource_count: int
    jenkins: dict[str, Any]
    kubernetes: dict[str, Any]
    top_incidents: list[V2IncidentResponse]
    llm_usage: dict[str, Any] = Field(default_factory=dict)


class V2JenkinsBuildResponse(BaseModel):
    id: str
    job_name: str
    build_number: int
    result: str
    url: str
    started_at: datetime
    completed_at: datetime | None = None
    duration_ms: int
    building: bool = False
    job_type: str = "other"
    parent: str | None = None
    head_type: str = "unknown"
    head_name: str | None = None
    source_provider: str | None = None
    repository: str | None = None
    change_number: str | None = None
    change_url: str | None = None
    source_kind: str = "unresolved"
    source_status: str = "pending"
    source_profile_id: str | None = None
    source_profile_registered: bool = False
    source_branch: str | None = None
    source_commit_sha: str | None = None
    source_url: str | None = None
    source_title: str | None = None
    source_state: str | None = None
    source_resolution_method: str = "none"
    source_reason: str | None = None
    source_allow_mr_comments: bool = False
    source_verified_at: datetime | None = None
    trigger_kind: str = "unknown"
    root_job: str
    root_build_number: int
    logical_run_key: str
    propagated_failure: bool = False
    recovered: bool = False
    failed_stage: str | None = None
    failure_summary: str | None = None
    failure_classification: str = "unknown"
    failure_signature: str = ""
    novelty: str = "unclassified"
    priority_score: int = 0
    priority_reasons: list[str] = Field(default_factory=list)
    coverage: str = "unknown"
    enrichment_status: str = "pending"
    incident_id: str | None = None


class V2LogicalExecutionResponse(BaseModel):
    logical_run_key: str
    title: str
    classification: str
    priority_score: int
    priority_reasons: list[str]
    first_seen_at: datetime
    last_seen_at: datetime
    root_job: str
    root_build_number: int
    source_provider: str | None = None
    repository: str | None = None
    change_number: str | None = None
    change_url: str | None = None
    source_kind: str = "unresolved"
    source_status: str = "pending"
    source_profile_id: str | None = None
    source_profile_registered: bool = False
    source_branch: str | None = None
    source_commit_sha: str | None = None
    source_url: str | None = None
    source_title: str | None = None
    source_state: str | None = None
    source_resolution_method: str = "none"
    source_reason: str | None = None
    source_verified_at: datetime | None = None
    affected_build_count: int
    propagated_build_count: int
    builds: list[dict[str, Any]]
    primary_build_id: str


class V2FailurePatternResponse(BaseModel):
    signature: str
    title: str
    classification: str
    occurrence_count: int
    affected_jobs: list[str]
    first_seen_at: datetime
    last_seen_at: datetime
    failed_wall_hours: float
    priority_score: int
    latest_build_id: str


class V2JobFamilyResponse(BaseModel):
    job_name: str
    job_type: str
    parent: str | None = None
    head_type: str
    head_name: str | None = None
    source_provider: str | None = None
    repository: str | None = None
    change_number: str | None = None
    source_kind: str = "unresolved"
    source_status: str = "pending"
    source_profile_id: str | None = None
    source_profile_registered: bool = False
    source_branch: str | None = None
    source_commit_sha: str | None = None
    source_url: str | None = None
    source_reason: str | None = None
    url: str
    coverage: str
    run_count: int
    result_counts: dict[str, int]
    failure_rate: float
    wall_hours: float
    median_duration_minutes: float
    p95_duration_minutes: float
    latest_result: str
    last_build_at: datetime


class V2MultibranchFamilyResponse(BaseModel):
    parent: str
    url: str
    child_count: int
    active_child_count: int
    run_count: int
    result_counts: dict[str, int]
    head_counts: dict[str, int]
    children: list[dict[str, Any]]


class V2JenkinsWorkspaceResponse(BaseModel):
    generated_at: datetime
    window_hours: int
    summary: dict[str, Any]
    new_failures: list[V2JenkinsBuildResponse]
    active_executions: list[V2LogicalExecutionResponse]
    recurring_patterns: list[V2FailurePatternResponse]
    busy_jobs: list[V2JobFamilyResponse]
    multibranch: list[V2MultibranchFamilyResponse]


class V2JenkinsFailurePage(BaseModel):
    items: list[V2JenkinsBuildResponse]
    next_cursor: str | None
    total_count: int


class V2JenkinsBuildDetailResponse(V2JenkinsBuildResponse):
    evidence: dict[str, Any]
    upstream_builds: list[dict[str, Any]]
    downstream_builds: list[dict[str, Any]]
    incident: V2IncidentResponse | None = None
    investigation_request: V2InvestigationRequestResponse | None = None
    latest_investigation: V2InvestigationResponse | None = None


class V2AnalyzeBuildRequest(BaseModel):
    mode: Literal["regular", "deep"] = "regular"


@router.post("/chat", response_model=V2ChatResponse)
async def chat(request: Request, body: V2ChatRequest) -> V2ChatResponse:
    try:
        container = _container(request)
        await container.investigation_queue.ensure_chat_budget_available()
        result = await container.reasoning_service.chat(
            message=body.message,
            incident_id=body.incident_id,
            history=tuple(body.history),
        )
    except DailyLLMBudgetExceeded as exc:
        raise HTTPException(
            status_code=429,
            detail={"code": "llm_budget_exhausted", "message": str(exc)},
        ) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail={"code": "incident_not_found"}) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail={"code": "reasoning_unavailable"}) from exc
    return _chat_response(result)


@router.post("/chat/stream")
async def chat_stream(request: Request, body: V2ChatRequest) -> EventSourceResponse:
    container = _container(request)
    try:
        await container.investigation_queue.ensure_chat_budget_available()
    except DailyLLMBudgetExceeded as exc:
        raise HTTPException(
            status_code=429,
            detail={"code": "llm_budget_exhausted", "message": str(exc)},
        ) from exc

    async def stream():
        progress: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        async def publish(event: dict[str, Any]) -> None:
            await progress.put(event)

        task = asyncio.create_task(
            container.reasoning_service.chat(
                message=body.message,
                incident_id=body.incident_id,
                history=tuple(body.history),
                on_progress=publish,
            )
        )
        try:
            while not task.done() or not progress.empty():
                if await request.is_disconnected():
                    task.cancel()
                    return
                try:
                    event = await asyncio.wait_for(progress.get(), timeout=0.5)
                except TimeoutError:
                    continue
                event_type = str(event.get("type") or "progress")
                yield {"event": event_type, "data": json.dumps(event, default=str, separators=(",", ":"))}
            result = await task
            payload = _chat_response(result).model_dump(mode="json")
            yield {"event": "message", "data": json.dumps(payload, separators=(",", ":"))}
        except LookupError:
            yield {"event": "error", "data": json.dumps({"code": "incident_not_found"})}
        except Exception as exc:
            yield {
                "event": "error",
                "data": json.dumps({"code": "reasoning_unavailable", "detail": type(exc).__name__}),
            }
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    return EventSourceResponse(stream(), ping=15)


@router.get("/overview", response_model=V2OverviewResponse)
async def overview(request: Request) -> V2OverviewResponse:
    container = _container(request)
    generated_at = datetime.now(timezone.utc)
    day_start = generated_at.replace(hour=0, minute=0, second=0, microsecond=0)
    async with container.uow_factory() as uow:
        latest_scan = await uow.scans.latest_completed()
        checks = await uow.checks.for_scan(latest_scan.id) if latest_scan else ()
        latest_scan_analysis = (
            await _scan_analysis_response(uow, latest_scan) if latest_scan else None
        )
        incident_page = await uow.incidents.list(limit=100, status="open")
        incident_rows: list[tuple[Incident, tuple[FindingObservation, ...]]] = []
        for incident in incident_page.items:
            observations = await uow.incidents.current_observations(incident.id)
            builds = await uow.jenkins.builds_for_incident(incident.id)
            incident_rows.append((incident, observations + jenkins_build_observations(builds)))
        llm_usage = await uow.llm_calls.summary_since(day_start)

    check_summaries = {check.check_name: to_primitive(check.summary) for check in checks}
    coverage_status = _coverage_status(checks, latest_scan)
    incident_rows.sort(
        key=lambda row: (
            _severity_rank(row[0].severity.value),
            len({item.resource_id for item in row[1]}),
            row[0].updated_at or row[0].created_at,
        ),
        reverse=True,
    )
    critical_count = sum(1 for incident, _ in incident_rows if incident.severity.value == "critical")
    warning_count = sum(1 for incident, _ in incident_rows if incident.severity.value == "warning")
    status = "unknown"
    if latest_scan:
        status = (
            "critical"
            if critical_count
            else "degraded"
            if warning_count or coverage_status != "complete"
            else "healthy"
        )

    jobs = check_summaries.get("jenkins_jobs", {})
    failed_builds = check_summaries.get("jenkins_failed_builds", {})
    connectivity = check_summaries.get("jenkins_agent_connectivity", {})
    agent_pods = check_summaries.get("jenkins_agent_pods", {})
    agent_resources = check_summaries.get("jenkins_agent_resources", {})
    nodes = check_summaries.get("k8s_nodes", {})
    workloads = check_summaries.get("k8s_workloads", {})
    events = check_summaries.get("k8s_events", {})
    return V2OverviewResponse(
        environment=container.settings.kubernetes_environment,
        status=status,
        generated_at=generated_at,
        latest_scan=(
            _scan_response(latest_scan, checks=checks, analysis=latest_scan_analysis)
            if latest_scan
            else None
        ),
        coverage_status=coverage_status,
        active_incident_count=len(incident_rows),
        critical_incident_count=critical_count,
        warning_incident_count=warning_count,
        affected_resource_count=sum(_affected_resource_count(current) for _, current in incident_rows),
        jenkins={
            "queue_size": jobs.get("queue_size"),
            "stuck_queue_count": jobs.get("stuck_queue_count"),
            "oldest_queue_wait_minutes": jobs.get("oldest_queue_wait_minutes"),
            "running_build_count": jobs.get("running_build_count"),
            "long_running_build_count": jobs.get("long_running_build_count"),
            "oldest_running_build_hours": jobs.get("oldest_running_build_hours"),
            "long_running_builds": jobs.get("long_running_builds", []),
            "failed_build_count": failed_builds.get("failed_build_count"),
            "failed_job_count": failed_builds.get("failed_job_count"),
            "failed_build_window_hours": failed_builds.get(
                "window_hours", container.settings.jenkins_failed_build_window_hours
            ),
            "recent_failed_builds": failed_builds.get("recent_failed_builds", []),
            "agent_count": connectivity.get("agent_count"),
            "online_agent_count": connectivity.get("online_agent_count"),
            "offline_agent_count": connectivity.get("offline_agent_count"),
            "executor_count": connectivity.get("executor_count"),
            "agent_pod_count": agent_pods.get("agent_pod_count"),
            "pod_phases": agent_pods.get("pod_phases", {}),
            "containers_missing_limits": agent_resources.get("containers_missing_limits"),
        },
        kubernetes={
            "node_count": nodes.get("node_count"),
            "ready_node_count": nodes.get("ready_node_count"),
            "not_ready_node_count": nodes.get("not_ready_node_count"),
            "metrics_node_count": nodes.get("metrics_node_count"),
            "jenkins_deployment_count": workloads.get("jenkins_deployment_count"),
            "jenkins_statefulset_count": workloads.get("jenkins_statefulset_count"),
            "meaningful_event_group_count": events.get("meaningful_event_group_count"),
        },
        top_incidents=[_incident_response(incident, current) for incident, current in incident_rows[:12]],
        llm_usage=llm_usage,
    )


@router.get("/jenkins", response_model=V2JenkinsWorkspaceResponse)
async def jenkins_workspace(
    request: Request,
    window_hours: Annotated[int, Query(ge=1, le=720)] = 168,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> V2JenkinsWorkspaceResponse:
    generated_at = datetime.now(timezone.utc)
    since = generated_at - timedelta(hours=window_hours)
    async with _container(request).uow_factory() as uow:
        summary = await uow.jenkins.jenkins_summary(since=since)
        new_failure_page = await uow.jenkins.failure_builds(
            since=since,
            limit=limit,
            novelty=frozenset({"new_failure", "new_regression"}),
        )
        executions = await uow.jenkins.logical_executions(since=since, limit=limit)
        patterns = await uow.jenkins.recurring_patterns(since=since, limit=limit)
        families = await uow.jenkins.job_families(since=since, limit=limit)
        multibranch = await uow.jenkins.multibranch_families(since=since, limit=100)
    return V2JenkinsWorkspaceResponse(
        generated_at=generated_at,
        window_hours=window_hours,
        summary=summary,
        new_failures=[V2JenkinsBuildResponse.model_validate(item) for item in new_failure_page.items],
        active_executions=[V2LogicalExecutionResponse.model_validate(item) for item in executions],
        recurring_patterns=[V2FailurePatternResponse.model_validate(item) for item in patterns],
        busy_jobs=[V2JobFamilyResponse.model_validate(item) for item in families],
        multibranch=[V2MultibranchFamilyResponse.model_validate(item) for item in multibranch],
    )


@router.get("/jenkins/failures", response_model=V2JenkinsFailurePage)
async def jenkins_failures(
    request: Request,
    window_hours: Annotated[int, Query(ge=1, le=720)] = 168,
    view: Literal["all", "new"] = "all",
    result: Literal["FAILURE", "UNSTABLE", "ABORTED"] | None = None,
    job: Annotated[str | None, Query(max_length=300)] = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> V2JenkinsFailurePage:
    since = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    novelty = frozenset({"new_failure", "new_regression"}) if view == "new" else None
    try:
        async with _container(request).uow_factory() as uow:
            page = await uow.jenkins.failure_builds(
                since=since,
                limit=limit,
                cursor=cursor,
                novelty=novelty,
                job=job,
                result=result,
            )
    except (InvalidCursorError, ValueError) as exc:
        raise HTTPException(status_code=422, detail={"code": "invalid_cursor"}) from exc
    return V2JenkinsFailurePage(
        items=[V2JenkinsBuildResponse.model_validate(item) for item in page.items],
        next_cursor=page.next_cursor,
        total_count=page.total_count or 0,
    )


@router.get("/jenkins/builds/{build_id}", response_model=V2JenkinsBuildDetailResponse)
async def jenkins_build_detail(request: Request, build_id: str) -> V2JenkinsBuildDetailResponse:
    async with _container(request).uow_factory() as uow:
        detail = await uow.jenkins.build_detail(build_id)
        incident = await uow.incidents.get(str(detail["incident_id"])) if detail and detail.get("incident_id") else None
        investigation_request = (
            await uow.investigation_requests.latest_for_incident(incident.id) if incident else None
        )
        investigation = await uow.investigations.latest_for_incident(incident.id) if incident else None
        model_calls = await uow.llm_calls.for_investigation(investigation.id) if investigation else ()
    if detail is None:
        raise HTTPException(status_code=404, detail={"code": "jenkins_build_not_found"})
    if incident:
        builds = await _build_observations_for_response(request, incident.id)
        detail["incident"] = _incident_response(incident, builds).model_dump(mode="python")
    detail["investigation_request"] = (
        _investigation_request_response(investigation_request).model_dump(mode="python")
        if investigation_request
        else None
    )
    detail["latest_investigation"] = (
        _investigation_response(investigation, model_calls=model_calls).model_dump(mode="python")
        if investigation
        else None
    )
    return V2JenkinsBuildDetailResponse.model_validate(detail)


@router.post(
    "/jenkins/builds/{build_id}/analyze",
    response_model=V2InvestigationRequestResponse,
    status_code=202,
)
async def analyze_jenkins_build(
    request: Request,
    build_id: str,
    body: V2AnalyzeBuildRequest,
) -> V2InvestigationRequestResponse:
    container = _container(request)
    async with container.uow_factory() as uow:
        build = await uow.jenkins.build_detail(build_id)
    if build is None:
        raise HTTPException(status_code=404, detail={"code": "jenkins_build_not_found"})
    if build.get("result") not in {"FAILURE", "UNSTABLE", "ABORTED"}:
        raise HTTPException(status_code=409, detail={"code": "jenkins_build_not_failed"})
    try:
        incident = await container.incident_service.correlate_jenkins_build(
            build,
            now=datetime.now(timezone.utc),
        )
        queued = await container.selection_service.request_manual(
            incident.id,
            source="manual_build",
            mode=ScanMode(body.mode),
            build_id=build_id,
            requested_by=_actor_email(request),
            force=True,
        )
    except DailyLLMBudgetExceeded as exc:
        raise HTTPException(
            status_code=429,
            detail={"code": "llm_budget_exhausted", "message": str(exc)},
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": "build_incident_conflict"}) from exc
    if queued is None:
        raise HTTPException(status_code=503, detail={"code": "investigation_unavailable"})
    return _investigation_request_response(queued)


@router.post("/scans", response_model=V2ScanResponse, status_code=202)
async def enqueue_scan(request: Request, body: V2ScanRequest) -> V2ScanResponse:
    container = _container(request)
    try:
        scan = await container.scan_service.enqueue(
            EnqueueScanCommand(
                mode=ScanMode(body.mode),
                categories=frozenset(body.categories or ()),
                triggering_user_email=_actor_email(request),
            )
        )
    except UnknownScanCategoryError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "unknown_categories", "categories": sorted(exc.categories)},
        ) from exc
    except ScanAlreadyActiveError as exc:
        active = _scan_response(exc.active_scan)
        raise HTTPException(
            status_code=409,
            detail={"code": "scan_active", "active_scan": active.model_dump(mode="json")},
        ) from exc
    return _scan_response(scan)


@router.get("/scans", response_model=V2ScanPage)
async def list_scans(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: str | None = None,
) -> V2ScanPage:
    try:
        async with _container(request).uow_factory() as uow:
            page = await uow.scans.list(limit=limit, cursor=cursor)
            items = [
                _scan_response(
                    scan,
                    analysis=await _scan_analysis_response(uow, scan),
                )
                for scan in page.items
            ]
    except InvalidCursorError as exc:
        raise HTTPException(status_code=422, detail={"code": "invalid_cursor"}) from exc
    return V2ScanPage(
        items=items,
        next_cursor=page.next_cursor,
    )


@router.get("/scans/{scan_id}", response_model=V2ScanResponse)
async def get_scan(request: Request, scan_id: str) -> V2ScanResponse:
    async with _container(request).uow_factory() as uow:
        scan = await uow.scans.get(scan_id)
        checks = await uow.checks.for_scan(scan_id) if scan else ()
        llm_usage = await uow.llm_calls.summary_for_scan(scan_id) if scan else {}
        analysis = await _scan_analysis_response(uow, scan) if scan else None
    if scan is None:
        raise HTTPException(status_code=404, detail={"code": "scan_not_found"})
    return _scan_response(scan, checks=checks, llm_usage=llm_usage, analysis=analysis)


@router.post("/scans/{scan_id}/cancel", response_model=V2CancelResponse)
async def cancel_scan(request: Request, scan_id: str) -> V2CancelResponse:
    now = datetime.now(timezone.utc)
    scan = await _container(request).scan_service.cancel(scan_id, now=now)
    if scan is None:
        raise HTTPException(status_code=404, detail={"code": "scan_not_found"})
    return V2CancelResponse(
        id=scan.id,
        status=scan.status.value,
        cancel_requested=scan.cancel_requested_at is not None,
    )


@router.get("/scans/{scan_id}/events")
async def scan_events(
    request: Request,
    scan_id: str,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> EventSourceResponse:
    try:
        sequence = int(last_event_id or 0)
        if sequence < 0:
            raise ValueError
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "invalid_last_event_id"}) from exc

    async with _container(request).uow_factory() as uow:
        if await uow.scans.get(scan_id) is None:
            raise HTTPException(status_code=404, detail={"code": "scan_not_found"})

    async def stream():
        current_sequence = sequence
        stream_id = "$"
        last_emission = datetime.now(timezone.utc)
        while not await request.is_disconnected():
            async with _container(request).uow_factory() as uow:
                events = await uow.events.after(scan_id, current_sequence)
                scan = await uow.scans.get(scan_id)
                requests = await uow.investigation_requests.for_scan(scan_id)
            for event in events:
                current_sequence = event.sequence
                last_emission = datetime.now(timezone.utc)
                yield _sse_event(event)
            analysis_active = any(
                item.status
                in {
                    InvestigationRequestStatus.QUEUED,
                    InvestigationRequestStatus.RUNNING,
                }
                for item in requests
            )
            if scan is None or (scan.terminal and not events and not analysis_active):
                return
            now = datetime.now(timezone.utc)
            if (now - last_emission).total_seconds() >= 15:
                last_emission = now
                heartbeat = {
                    "sequence": current_sequence,
                    "type": "heartbeat",
                    "occurred_at": now.isoformat(),
                    "payload_version": 1,
                    "payload": {},
                }
                yield {"event": "heartbeat", "data": json.dumps(heartbeat, separators=(",", ":"))}
            stream_id = await _container(request).notifier.wait(scan_id, stream_id, timeout_seconds=5.0)

    return EventSourceResponse(stream(), ping=15)


@router.get("/incidents", response_model=V2IncidentPage)
async def list_incidents(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: str | None = None,
    status: Literal["open", "resolved", "suppressed"] | None = None,
    severity: Literal["low", "warning", "critical"] | None = None,
    source_type: Literal[
        "merge_request",
        "repository",
        "pipeline",
        "multiple",
        "infrastructure",
        "unknown",
    ] | None = None,
) -> V2IncidentPage:
    try:
        async with _container(request).uow_factory() as uow:
            page = await uow.incidents.list(
                limit=limit,
                cursor=cursor,
                status=status,
                severity=severity,
                source_type=source_type,
            )
    except InvalidCursorError as exc:
        raise HTTPException(status_code=422, detail={"code": "invalid_cursor"}) from exc
    items = []
    async with _container(request).uow_factory() as uow:
        for incident in page.items:
            observations = await uow.incidents.current_observations(incident.id)
            builds = await uow.jenkins.builds_for_incident(incident.id)
            items.append(_incident_response(incident, observations + jenkins_build_observations(builds)))
    return V2IncidentPage(items=items, next_cursor=page.next_cursor)


@router.get("/incidents/{incident_id}", response_model=V2IncidentDetailResponse)
async def get_incident(request: Request, incident_id: str) -> V2IncidentDetailResponse:
    try:
        async with _container(request).uow_factory() as uow:
            incident = await uow.incidents.get(incident_id)
            if incident is None:
                raise HTTPException(status_code=404, detail={"code": "incident_not_found"})
            observations = await uow.incidents.observations(incident_id)
            current_observations = await uow.incidents.current_observations(incident_id)
            builds = await uow.jenkins.builds_for_incident(incident_id)
            build_observations = jenkins_build_observations(builds)
            investigation = await uow.investigations.latest_for_incident(incident_id)
            investigation_request = await uow.investigation_requests.latest_for_incident(incident_id)
            analysis_decision = await uow.analysis_decisions.latest_for_incident(incident_id)
            model_calls = await uow.llm_calls.for_investigation(investigation.id) if investigation else ()
            actions = await uow.actions.for_incident(incident_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={"code": "incident_not_found"}) from exc
    return V2IncidentDetailResponse(
        incident=_incident_response(incident, current_observations + build_observations),
        observations=[_observation_response(item) for item in observations + build_observations],
        current_observations=[_observation_response(item) for item in current_observations + build_observations],
        occurrences=[
            V2OccurrenceResponse(
                id=item.id,
                number=item.number,
                opened_at=item.opened_at,
                last_observed_at=item.last_observed_at,
                resolved_at=item.resolved_at,
                responsible_checks=sorted(item.responsible_checks),
                observation_identities=sorted(item.observation_identities),
            )
            for item in incident.occurrence_history
        ],
        latest_investigation=(
            _investigation_response(investigation, model_calls=model_calls) if investigation else None
        ),
        investigation_request=(
            _investigation_request_response(investigation_request) if investigation_request else None
        ),
        analysis_decision=_analysis_decision_response(analysis_decision) if analysis_decision else None,
        jenkins_builds=[to_primitive(item) for item in builds],
        actions=[_action_response(item) for item in actions],
    )


@router.post("/incidents/{incident_id}/suppress", response_model=V2IncidentResponse)
async def suppress_incident(request: Request, incident_id: str, body: V2SuppressionRequest) -> V2IncidentResponse:
    actor = _actor_email(request)
    if not actor:
        raise HTTPException(status_code=401, detail={"code": "authenticated_actor_required"})
    now = datetime.now(timezone.utc)
    try:
        async with _container(request).uow_factory() as uow:
            incident = await uow.incidents.get(incident_id)
            if incident is None:
                raise HTTPException(status_code=404, detail={"code": "incident_not_found"})
            incident = incident.suppress(reason=body.reason, actor=actor, suppressed_at=now)
            await uow.incidents.save(incident)
            await uow.commit()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "invalid_suppression"}) from exc
    return _incident_response(incident)


@router.post("/incidents/{incident_id}/unsuppress", response_model=V2IncidentResponse)
async def unsuppress_incident(request: Request, incident_id: str) -> V2IncidentResponse:
    if not _actor_email(request):
        raise HTTPException(status_code=401, detail={"code": "authenticated_actor_required"})
    try:
        async with _container(request).uow_factory() as uow:
            incident = await uow.incidents.get(incident_id)
            if incident is None:
                raise HTTPException(status_code=404, detail={"code": "incident_not_found"})
            incident = incident.unsuppress()
            await uow.incidents.save(incident)
            await uow.commit()
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={"code": "incident_not_found"}) from exc
    return _incident_response(incident)


@router.post(
    "/incidents/{incident_id}/reinvestigate",
    response_model=V2InvestigationRequestResponse,
    status_code=202,
)
async def reinvestigate_incident(request: Request, incident_id: str) -> V2InvestigationRequestResponse:
    try:
        investigation = await _container(request).selection_service.request_manual(
            incident_id,
            source="manual_incident",
            mode=ScanMode.DEEP,
            requested_by=_actor_email(request),
            force=True,
        )
    except DailyLLMBudgetExceeded as exc:
        raise HTTPException(
            status_code=429,
            detail={"code": "llm_budget_exhausted", "message": str(exc)},
        ) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail={"code": "incident_not_found"}) from exc
    if investigation is None:
        raise HTTPException(status_code=503, detail={"code": "investigation_unavailable"})
    return _investigation_request_response(investigation)


@router.post("/incidents/{incident_id}/chat", response_model=V2ChatResponse)
async def incident_chat(request: Request, incident_id: str, body: V2ChatRequest) -> V2ChatResponse:
    try:
        container = _container(request)
        await container.investigation_queue.ensure_chat_budget_available()
        result = await container.reasoning_service.chat(message=body.message, incident_id=incident_id)
    except DailyLLMBudgetExceeded as exc:
        raise HTTPException(
            status_code=429,
            detail={"code": "llm_budget_exhausted", "message": str(exc)},
        ) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail={"code": "incident_not_found"}) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail={"code": "reasoning_unavailable"}) from exc
    return _chat_response(result)


@router.get("/actions", response_model=V2ActionPage)
async def list_actions(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: str | None = None,
    status: Literal["pending", "running", "retry_scheduled", "succeeded", "permanently_failed"] | None = None,
    action_type: Literal["email", "jira_create", "jira_update", "github_comment", "gitlab_comment"] | None = None,
    incident_id: str | None = None,
) -> V2ActionPage:
    try:
        async with _container(request).uow_factory() as uow:
            page = await uow.actions.list(
                limit=limit,
                cursor=cursor,
                status=status,
                action_type=action_type,
                incident_id=incident_id,
            )
    except (InvalidCursorError, ValueError) as exc:
        raise HTTPException(status_code=422, detail={"code": "invalid_filter_or_cursor"}) from exc
    return V2ActionPage(
        items=[_action_response(item) for item in page.items],
        next_cursor=page.next_cursor,
    )


@router.get("/actions/{action_id}", response_model=V2ActionDetailResponse)
async def get_action(request: Request, action_id: str) -> V2ActionDetailResponse:
    try:
        async with _container(request).uow_factory() as uow:
            action = await uow.actions.get(action_id)
            if action is None:
                raise HTTPException(status_code=404, detail={"code": "action_not_found"})
            attempts = await uow.delivery_attempts.for_action(action_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={"code": "action_not_found"}) from exc
    return V2ActionDetailResponse(
        action=_action_response(action),
        attempts=[
            V2DeliveryAttemptResponse(
                id=item.id,
                retry_cycle=item.retry_cycle,
                attempt_number=item.attempt_number,
                status=item.status.value,
                response_metadata=to_primitive(item.response_metadata),
                error_summary=item.error_summary,
                started_at=item.started_at,
                completed_at=item.completed_at,
            )
            for item in attempts
        ],
    )


@router.post("/actions/{action_id}/retry", response_model=V2ActionResponse)
async def retry_action(request: Request, action_id: str) -> V2ActionResponse:
    try:
        action = await _container(request).make_delivery_worker("api-manual-retry").manual_retry(action_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail={"code": "action_not_found"}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": "action_not_permanently_failed"}) from exc
    return _action_response(action)


def _container(request: Request) -> Any:
    container = getattr(request.app.state, "container", None)
    if container is None:
        raise HTTPException(status_code=503, detail={"code": "service_not_ready"})
    return container


def _actor_email(request: Request) -> str | None:
    user = getattr(request.state, "user", None)
    return user.get("email") if isinstance(user, dict) else None


async def _scan_analysis_response(uow: Any, scan: Scan) -> V2ScanAnalysisResponse:
    decisions = await uow.analysis_decisions.for_scan(scan.id)
    requests = await uow.investigation_requests.for_scan(scan.id)
    request_by_id = {item.id: item for item in requests}
    request_by_incident = {item.incident_id: item for item in requests}
    investigation_by_request: dict[str, Investigation] = {}
    for item in requests:
        if not item.investigation_id:
            continue
        investigation = await uow.investigations.get(item.investigation_id)
        if investigation is not None:
            investigation_by_request[item.id] = investigation
    incidents = {}
    for incident_id in dict.fromkeys(item.incident_id for item in decisions):
        incident = await uow.incidents.get(incident_id)
        if incident is not None:
            incidents[incident_id] = incident

    selected_count = sum(
        item.outcome is AnalysisDecisionOutcome.SELECTED for item in decisions
    )
    queued_count = sum(
        item.status is InvestigationRequestStatus.QUEUED for item in requests
    )
    running_count = sum(
        item.status is InvestigationRequestStatus.RUNNING for item in requests
    )
    partial_count = sum(
        item.status is InvestigationRequestStatus.SUCCEEDED
        and investigation_by_request.get(item.id) is not None
        and investigation_by_request[item.id].status is InvestigationStatus.PARTIAL
        for item in requests
    )
    succeeded_count = (
        sum(item.status is InvestigationRequestStatus.SUCCEEDED for item in requests)
        - partial_count
    )
    failed_count = sum(
        item.status is InvestigationRequestStatus.FAILED for item in requests
    )
    reused_count = sum(
        item.outcome is AnalysisDecisionOutcome.REUSED for item in decisions
    )
    deferred_count = sum(
        item.outcome is AnalysisDecisionOutcome.DEFERRED for item in decisions
    )
    manual_only_count = sum(
        item.outcome is AnalysisDecisionOutcome.MANUAL_ONLY for item in decisions
    )
    budget_deferred_count = sum(
        item.outcome is AnalysisDecisionOutcome.BUDGET_DEFERRED for item in decisions
    )
    active_count = queued_count + running_count
    all_budget_deferred = bool(decisions) and budget_deferred_count == len(decisions) and not requests

    status: ScanAnalysisStatus
    if running_count:
        status = "running"
    elif queued_count:
        status = "queued"
    elif not scan.terminal and scan.stage.value in {
        "investigating",
        "planning_actions",
        "completed",
    }:
        status = "selecting"
    elif scan.terminal and all_budget_deferred:
        status = "budget_deferred"
    elif scan.terminal and (
        partial_count or failed_count or budget_deferred_count or scan.status.value != "succeeded"
    ):
        status = "complete_with_issues"
    elif scan.terminal:
        status = "complete"
    else:
        status = "not_started"

    items = []
    for decision in decisions:
        linked_request = (
            request_by_id.get(decision.request_id or "")
            or request_by_incident.get(decision.incident_id)
        )
        incident = incidents.get(decision.incident_id)
        investigation = investigation_by_request.get(linked_request.id) if linked_request else None
        error_summary = linked_request.error_summary if linked_request else None
        if not error_summary and investigation is not None:
            error_summary = investigation.error_summary
        items.append(
            V2ScanAnalysisItemResponse(
                incident_id=decision.incident_id,
                incident_title=incident.title if incident else "Incident unavailable",
                severity=incident.severity.value if incident else "unknown",
                outcome=decision.outcome.value,
                reason_code=decision.reason_code,
                reason=decision.reason,
                request_id=linked_request.id if linked_request else decision.request_id,
                request_status=linked_request.status.value if linked_request else None,
                investigation_id=linked_request.investigation_id if linked_request else None,
                investigation_status=investigation.status.value if investigation else None,
                error_summary=error_summary,
                completed_at=linked_request.completed_at if linked_request else None,
            )
        )

    return V2ScanAnalysisResponse(
        status=status,
        candidate_count=len(decisions),
        selected_count=selected_count,
        queued_count=queued_count,
        running_count=running_count,
        succeeded_count=succeeded_count,
        partial_count=partial_count,
        failed_count=failed_count,
        reused_count=reused_count,
        deferred_count=deferred_count,
        manual_only_count=manual_only_count,
        budget_deferred_count=budget_deferred_count,
        active_count=active_count,
        budget_metric=_decision_budget_metric(decisions),
        budget_reset_at=_decision_budget_reset_at(decisions),
        budget_limit_tokens=_decision_budget_int(decisions, "budget_limit_tokens"),
        budget_spent_tokens=_decision_budget_int(decisions, "budget_spent_tokens"),
        budget_projected_tokens=_decision_budget_int(decisions, "budget_projected_tokens"),
        budget_limit_usd=_decision_budget_float(decisions, "budget_limit_usd"),
        budget_spent_usd=_decision_budget_float(decisions, "budget_spent_usd"),
        budget_projected_usd=_decision_budget_float(decisions, "budget_projected_usd"),
        items=items,
    )


def _decision_budget_reset_at(decisions: tuple[AnalysisDecision, ...]) -> datetime | None:
    values = []
    for decision in decisions:
        value = decision.metadata.get("budget_reset_at")
        if isinstance(value, datetime):
            values.append(value)
            continue
        if isinstance(value, str):
            try:
                values.append(datetime.fromisoformat(value.replace("Z", "+00:00")))
            except ValueError:
                continue
    if values:
        return min(values)
    legacy_budget_decisions = [
        decision for decision in decisions if decision.reason_code == "daily_budget_exhausted"
    ]
    if not legacy_budget_decisions:
        return None
    earliest = min(decision.created_at for decision in legacy_budget_decisions).astimezone(timezone.utc)
    return datetime.combine(earliest.date(), datetime.min.time(), tzinfo=timezone.utc) + timedelta(days=1)


def _decision_budget_int(decisions: tuple[AnalysisDecision, ...], key: str) -> int | None:
    values = [
        int(value)
        for decision in decisions
        if isinstance((value := decision.metadata.get(key)), int)
    ]
    return max(values) if values else None


def _decision_budget_float(decisions: tuple[AnalysisDecision, ...], key: str) -> float | None:
    values = [
        float(value)
        for decision in decisions
        if isinstance((value := decision.metadata.get(key)), (int, float, Decimal)) and not isinstance(value, bool)
    ]
    return max(values) if values else None


def _decision_budget_metric(decisions: tuple[AnalysisDecision, ...]) -> str | None:
    values = {
        value
        for decision in decisions
        if isinstance((value := decision.metadata.get("budget_metric")), str)
    }
    if "cost_usd" in values:
        return "cost_usd"
    return "tokens" if "tokens" in values else None


def _scan_response(
    scan: Scan,
    *,
    checks: tuple[CheckResult, ...] = (),
    llm_usage: dict[str, Any] | None = None,
    analysis: V2ScanAnalysisResponse | None = None,
) -> V2ScanResponse:
    return V2ScanResponse(
        id=scan.id,
        status=scan.status.value,
        stage=scan.stage.value,
        mode=scan.mode.value,
        categories=sorted(scan.categories),
        created_at=scan.created_at,
        started_at=scan.started_at,
        completed_at=scan.completed_at,
        cancel_requested_at=scan.cancel_requested_at,
        attempt_count=scan.attempt_count,
        failure_summary=scan.failure_summary,
        coverage_status=_coverage_status(checks, scan),
        checks=[_check_response(check) for check in checks],
        llm_usage=llm_usage or {},
        analysis=analysis or _empty_scan_analysis(scan),
        urls={
            "detail": f"/api/v2/scans/{scan.id}",
            "events": f"/api/v2/scans/{scan.id}/events",
            "cancel": f"/api/v2/scans/{scan.id}/cancel",
        },
    )


def _empty_scan_analysis(scan: Scan) -> V2ScanAnalysisResponse:
    status: ScanAnalysisStatus
    if scan.terminal:
        status = "complete" if scan.status.value == "succeeded" else "complete_with_issues"
    elif scan.stage.value in {"investigating", "planning_actions", "completed"}:
        status = "selecting"
    else:
        status = "not_started"
    return V2ScanAnalysisResponse(status=status)


def _check_response(check: CheckResult) -> V2CheckExecutionResponse:
    return V2CheckExecutionResponse(
        name=check.check_name,
        status=check.status.value,
        categories=sorted(check.categories),
        finding_count=len(check.findings),
        summary=to_primitive(check.summary),
        failure_summary=check.failure_summary,
        started_at=check.started_at,
        completed_at=check.completed_at,
    )


def _coverage_status(checks: tuple[CheckResult, ...], scan: Scan | None) -> str:
    if scan is None:
        return "unknown"
    if not checks:
        return "running" if not scan.terminal else "unavailable"
    succeeded = sum(check.status.value == "succeeded" for check in checks)
    if succeeded == len(checks):
        return "complete"
    if succeeded == 0:
        return "unavailable"
    return "partial"


def _incident_response(
    incident: Incident,
    current_observations: tuple[FindingObservation, ...] = (),
) -> V2IncidentResponse:
    affected_resource_count = _affected_resource_count(current_observations)
    return V2IncidentResponse(
        id=incident.id,
        status=incident.status.value,
        severity=incident.severity.value,
        title=_display_incident_title(incident, current_observations),
        correlation_rule_id=incident.correlation_rule_id,
        correlation_key=incident.correlation_key,
        source=to_primitive(incident.source),
        actionability=incident.actionability,
        classification=incident.classification,
        priority=incident.priority,
        created_at=incident.created_at,
        updated_at=incident.updated_at,
        resolved_at=incident.resolved_at,
        suppressed_reason=incident.suppressed_reason,
        suppressed_by=incident.suppressed_by,
        suppressed_at=incident.suppressed_at,
        occurrence_number=incident.current_occurrence.number,
        affected_resource_count=affected_resource_count,
        current_observation_count=len(current_observations),
        first_seen_at=incident.current_occurrence.opened_at,
        last_seen_at=max(
            (item.observed_at for item in current_observations),
            default=incident.current_occurrence.last_observed_at,
        ),
        domain=_incident_domain(incident, current_observations),
    )


def _display_incident_title(
    incident: Incident,
    observations: tuple[FindingObservation, ...],
) -> str:
    count = _affected_resource_count(observations)
    if incident.correlation_rule_id == "jenkins_runtime_condition_v2":
        return f"{count} Jenkins build{'s' if count != 1 else ''} running longer than 2 hours"
    if incident.correlation_rule_id == "jenkins_agent_configuration_v2":
        return f"{count} Jenkins agent container{'s' if count != 1 else ''} missing resource limits"
    if incident.correlation_rule_id == "jenkins_queue_condition_v2" and observations:
        return f"{count} Jenkins job{'s' if count != 1 else ''} stuck in queue"
    return incident.title


def _incident_domain(
    incident: Incident,
    observations: tuple[FindingObservation, ...],
) -> str:
    categories = {item.category for item in observations}
    if incident.correlation_rule_id.startswith("jenkins_runtime") or "jenkins_build" in categories:
        return "builds"
    if "jenkins_queue" in categories:
        return "queue"
    if "jenkins_agent" in categories:
        return "agents"
    if any(category.startswith("k8s_") for category in categories):
        return "kubernetes"
    if incident.source.get("kind") == "merge_request":
        return "merge_request"
    return "unknown"


def _affected_resource_count(observations: tuple[FindingObservation, ...]) -> int:
    queue_tasks = {
        str(item.evidence["queue_task"])
        for item in observations
        if item.category == "jenkins_queue" and item.evidence.get("queue_task")
    }
    if queue_tasks:
        return len(queue_tasks)
    return len({item.resource_id for item in observations})


def _severity_rank(value: str) -> int:
    return {"critical": 2, "warning": 1, "low": 0}.get(value, 0)


def _chat_response(result: Any) -> V2ChatResponse:
    if isinstance(result, str):
        return V2ChatResponse(content=result)
    return V2ChatResponse(
        content=result.content,
        references=list(result.references),
        as_of=result.as_of,
        coverage_status=result.coverage_status,
    )


def _observation_response(observation: Any) -> V2ObservationResponse:
    return V2ObservationResponse(
        scan_id=observation.scan_id,
        check_name=observation.check_name,
        stable_identity=observation.stable_identity,
        rule_id=observation.rule_id,
        resource_id=observation.resource_id,
        category=observation.category,
        severity=observation.severity.value,
        summary=observation.summary,
        observed_at=observation.observed_at,
        identity_dimensions=to_primitive(observation.identity_dimensions),
        evidence=to_primitive(observation.evidence),
    )


def _investigation_response(
    investigation: Investigation,
    *,
    model_calls: tuple[LLMCall, ...] = (),
) -> V2InvestigationResponse:
    return V2InvestigationResponse(
        id=investigation.id,
        status=investigation.status.value,
        evidence_hash=investigation.evidence_hash,
        input_version=investigation.input_version,
        prompt_version=investigation.prompt_version,
        model=investigation.model,
        confidence=investigation.confidence.value if investigation.confidence else None,
        usage=to_primitive(investigation.usage),
        result=to_primitive(investigation.result),
        error_summary=investigation.error_summary,
        created_at=investigation.created_at,
        completed_at=investigation.completed_at,
        model_calls=[_llm_call_response(call) for call in model_calls],
    )


def _llm_call_response(call: LLMCall) -> V2LLMCallResponse:
    return V2LLMCallResponse(
        id=call.id,
        purpose=call.purpose,
        model=call.model,
        prompt_tokens=call.prompt_tokens,
        completion_tokens=call.completion_tokens,
        cache_read_input_tokens=call.cache_read_input_tokens,
        cache_creation_input_tokens=call.cache_creation_input_tokens,
        total_tokens=call.total_tokens,
        estimated_cost_usd=float(call.estimated_cost_usd) if call.estimated_cost_usd is not None else None,
        cost_source=call.cost_source,
        created_at=call.created_at,
    )


def _analysis_decision_response(decision: AnalysisDecision) -> V2AnalysisDecisionResponse:
    return V2AnalysisDecisionResponse(
        id=decision.id,
        outcome=decision.outcome.value,
        reason_code=decision.reason_code,
        reason=decision.reason,
        source=decision.source,
        mode=decision.mode.value,
        priority=decision.priority,
        evidence_hash=decision.evidence_hash,
        scan_id=decision.scan_id,
        request_id=decision.request_id,
        created_at=decision.created_at,
    )


def _investigation_request_response(request: InvestigationRequest) -> V2InvestigationRequestResponse:
    return V2InvestigationRequestResponse(
        id=request.id,
        incident_id=request.incident_id,
        occurrence_id=request.occurrence_id,
        mode=request.mode.value,
        source=request.source,
        priority=request.priority,
        evidence_hash=request.evidence_hash,
        status=request.status.value,
        scan_id=request.scan_id,
        build_id=request.build_id,
        requested_by=request.requested_by,
        budget_kind=request.budget_kind.value,
        reserved_tokens=request.reserved_tokens,
        attempt_count=request.attempt_count,
        next_attempt_at=request.next_attempt_at,
        investigation_id=request.investigation_id,
        error_summary=request.error_summary,
        created_at=request.created_at,
        updated_at=request.updated_at,
        completed_at=request.completed_at,
    )


async def _build_observations_for_response(
    request: Request, incident_id: str
) -> tuple[FindingObservation, ...]:
    async with _container(request).uow_factory() as uow:
        observations = await uow.incidents.current_observations(incident_id)
        builds = await uow.jenkins.builds_for_incident(incident_id)
    return observations + jenkins_build_observations(builds)


def _action_response(action: Action) -> V2ActionResponse:
    return V2ActionResponse(
        id=action.id,
        incident_id=action.incident_id,
        occurrence_id=action.occurrence_id,
        action_type=action.action_type.value,
        destination=action.destination,
        status=action.status.value,
        rendered_payload=to_primitive(action.rendered_payload),
        template_version=action.template_version,
        external_reference=action.external_reference,
        attempt_count=action.attempt_count,
        retry_cycle=action.retry_cycle,
        next_attempt_at=action.next_attempt_at,
        failure_summary=action.failure_summary,
        created_at=action.created_at,
        updated_at=action.updated_at,
        completed_at=action.completed_at,
    )


def _sse_event(event: ScanEvent) -> dict[str, str]:
    envelope = {
        "sequence": event.sequence,
        "type": event.type,
        "occurred_at": event.occurred_at.isoformat(),
        "payload_version": event.payload_version,
        "payload": event.payload,
    }
    return {
        "id": str(event.sequence),
        "event": event.type,
        "data": json.dumps(envelope, separators=(",", ":")),
    }
