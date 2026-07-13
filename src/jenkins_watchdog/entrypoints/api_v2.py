"""FastAPI entrypoint for durable v2 scan APIs."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from jenkins_watchdog.application.pagination import InvalidCursorError
from jenkins_watchdog.application.scan_service import (
    EnqueueScanCommand,
    ScanAlreadyActiveError,
    UnknownScanCategoryError,
)
from jenkins_watchdog.application.types import ScanEvent
from jenkins_watchdog.domain.model import Action, Incident, Investigation, Scan, ScanMode
from jenkins_watchdog.domain.serialization import to_primitive

router = APIRouter()


class V2ScanRequest(BaseModel):
    mode: Literal["regular", "deep"] = "regular"
    categories: list[str] | None = Field(default=None)


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
    occurrences: list[V2OccurrenceResponse]
    latest_investigation: V2InvestigationResponse | None
    actions: list[V2ActionResponse]


class V2SuppressionRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class V2ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=10000)
    incident_id: str | None = None


class V2ChatResponse(BaseModel):
    content: str


@router.post("/chat", response_model=V2ChatResponse)
async def chat(request: Request, body: V2ChatRequest) -> V2ChatResponse:
    try:
        content = await _container(request).reasoning_service.chat(
            message=body.message,
            incident_id=body.incident_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail={"code": "incident_not_found"}) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail={"code": "reasoning_unavailable"}) from exc
    return V2ChatResponse(content=content)


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
    except InvalidCursorError as exc:
        raise HTTPException(status_code=422, detail={"code": "invalid_cursor"}) from exc
    return V2ScanPage(
        items=[_scan_response(scan) for scan in page.items],
        next_cursor=page.next_cursor,
    )


@router.get("/scans/{scan_id}", response_model=V2ScanResponse)
async def get_scan(request: Request, scan_id: str) -> V2ScanResponse:
    async with _container(request).uow_factory() as uow:
        scan = await uow.scans.get(scan_id)
    if scan is None:
        raise HTTPException(status_code=404, detail={"code": "scan_not_found"})
    return _scan_response(scan)


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
            for event in events:
                current_sequence = event.sequence
                last_emission = datetime.now(timezone.utc)
                yield _sse_event(event)
            if scan is None or (scan.terminal and not events):
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
    source_type: Literal["merge_request", "infrastructure", "unknown"] | None = None,
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
    return V2IncidentPage(
        items=[_incident_response(item) for item in page.items],
        next_cursor=page.next_cursor,
    )


@router.get("/incidents/{incident_id}", response_model=V2IncidentDetailResponse)
async def get_incident(request: Request, incident_id: str) -> V2IncidentDetailResponse:
    try:
        async with _container(request).uow_factory() as uow:
            incident = await uow.incidents.get(incident_id)
            if incident is None:
                raise HTTPException(status_code=404, detail={"code": "incident_not_found"})
            observations = await uow.incidents.observations(incident_id)
            investigation = await uow.investigations.latest_for_incident(incident_id)
            actions = await uow.actions.for_incident(incident_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={"code": "incident_not_found"}) from exc
    return V2IncidentDetailResponse(
        incident=_incident_response(incident),
        observations=[_observation_response(item) for item in observations],
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
        latest_investigation=_investigation_response(investigation) if investigation else None,
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


@router.post("/incidents/{incident_id}/reinvestigate", response_model=V2InvestigationResponse)
async def reinvestigate_incident(request: Request, incident_id: str) -> V2InvestigationResponse:
    try:
        investigation = await _container(request).reasoning_service.investigate_if_needed(incident_id, force=True)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail={"code": "incident_not_found"}) from exc
    if investigation is None:
        raise HTTPException(status_code=503, detail={"code": "investigation_unavailable"})
    return _investigation_response(investigation)


@router.post("/incidents/{incident_id}/chat", response_model=V2ChatResponse)
async def incident_chat(request: Request, incident_id: str, body: V2ChatRequest) -> V2ChatResponse:
    try:
        content = await _container(request).reasoning_service.chat(message=body.message, incident_id=incident_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail={"code": "incident_not_found"}) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail={"code": "reasoning_unavailable"}) from exc
    return V2ChatResponse(content=content)


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


def _scan_response(scan: Scan) -> V2ScanResponse:
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
        urls={
            "detail": f"/api/v2/scans/{scan.id}",
            "events": f"/api/v2/scans/{scan.id}/events",
            "cancel": f"/api/v2/scans/{scan.id}/cancel",
        },
    )


def _incident_response(incident: Incident) -> V2IncidentResponse:
    return V2IncidentResponse(
        id=incident.id,
        status=incident.status.value,
        severity=incident.severity.value,
        title=incident.title,
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


def _investigation_response(investigation: Investigation) -> V2InvestigationResponse:
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
    )


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
