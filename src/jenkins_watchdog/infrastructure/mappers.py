"""Explicit conversions between v2 domain values and ORM records."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jenkins_watchdog.domain.model import (
    Action,
    ActionStatus,
    ActionType,
    CheckResult,
    CheckStatus,
    Confidence,
    DeliveryAttempt,
    DeliveryAttemptStatus,
    FindingObservation,
    Incident,
    IncidentOccurrence,
    IncidentStatus,
    Investigation,
    InvestigationRequest,
    InvestigationRequestStatus,
    InvestigationStatus,
    Scan,
    ScanMode,
    ScanStage,
    ScanStatus,
    Severity,
)
from jenkins_watchdog.infrastructure.models import (
    ActionRecord,
    CheckExecutionRecord,
    DeliveryAttemptRecord,
    FindingRecord,
    IncidentRecord,
    InvestigationRecord,
    InvestigationRequestRecord,
    ScanRecord,
)


def scan_from_record(record: ScanRecord) -> Scan:
    return Scan(
        id=str(record.id),
        mode=ScanMode(record.mode),
        categories=frozenset(record.categories),
        status=ScanStatus(record.status),
        created_at=record.created_at,
        stage=ScanStage(record.stage),
        triggering_user_email=record.triggering_user_email,
        scheduled=record.scheduled,
        cancel_requested_at=record.cancel_requested_at,
        lease_owner=record.lease_owner,
        lease_expires_at=record.lease_expires_at,
        attempt_count=record.attempt_count,
        next_attempt_at=record.next_attempt_at,
        failure_summary=record.failure_summary,
        started_at=record.started_at,
        completed_at=record.completed_at,
        updated_at=record.updated_at,
    )


def update_scan_record(record: ScanRecord, scan: Scan) -> None:
    record.mode = scan.mode.value
    record.categories = sorted(scan.categories)
    record.status = scan.status.value
    record.stage = scan.stage.value
    record.active_slot = None if scan.terminal else True
    record.triggering_user_email = scan.triggering_user_email
    record.scheduled = scan.scheduled
    record.cancel_requested_at = scan.cancel_requested_at
    record.lease_owner = scan.lease_owner
    record.lease_expires_at = scan.lease_expires_at
    record.attempt_count = scan.attempt_count
    record.next_attempt_at = scan.next_attempt_at
    record.failure_summary = scan.failure_summary
    record.created_at = scan.created_at
    record.started_at = scan.started_at
    record.completed_at = scan.completed_at
    record.updated_at = scan.updated_at or scan.created_at


def check_result_from_record(
    record: CheckExecutionRecord, findings: tuple[FindingObservation, ...] = ()
) -> CheckResult:
    return CheckResult(
        scan_id=str(record.scan_id),
        check_name=record.check_name,
        status=CheckStatus(record.status),
        findings=findings,
        failure_summary=record.failure_summary,
        categories=frozenset(record.categories),
        summary=record.summary,
        started_at=record.started_at,
        completed_at=record.completed_at,
    )


def observation_from_record(record: FindingRecord) -> FindingObservation:
    return FindingObservation(
        scan_id=str(record.scan_id),
        check_name=record.check_execution.check_name,
        rule_id=record.rule_id,
        resource_id=record.resource_id,
        severity=Severity(record.severity),
        category=record.category,
        summary=record.summary,
        observed_at=record.observed_at,
        identity_dimensions=record.identity_dimensions,
        evidence=record.evidence,
    )


def incident_from_record(record: IncidentRecord) -> Incident:
    occurrences = tuple(
        IncidentOccurrence(
            id=str(item.id),
            opened_at=item.opened_at,
            number=item.number,
            last_observed_at=item.last_observed_at,
            resolved_at=item.resolved_at,
            observation_identities=frozenset(link.finding.stable_identity for link in item.finding_links),
            responsible_checks=frozenset(item.responsible_checks),
        )
        for item in record.occurrences
    )
    occurrence = occurrences[-1]
    return Incident(
        id=str(record.id),
        correlation_rule_id=record.correlation_rule_id,
        correlation_key=record.correlation_key,
        status=IncidentStatus(record.status),
        created_at=record.created_at,
        current_occurrence=occurrence,
        occurrence_history=occurrences,
        severity=Severity(record.severity),
        title=record.title,
        source=record.source,
        actionability=record.actionability,
        classification=record.classification,
        priority=record.priority,
        updated_at=record.updated_at,
        resolved_at=record.resolved_at,
        suppressed_reason=record.suppressed_reason,
        suppressed_by=record.suppressed_by,
        suppressed_at=record.suppressed_at,
    )


def update_incident_record(record: IncidentRecord, incident: Incident) -> None:
    record.correlation_rule_id = incident.correlation_rule_id
    record.correlation_key = incident.correlation_key
    record.status = incident.status.value
    record.severity = incident.severity.value
    record.title = incident.title
    record.source = jsonable(incident.source)
    record.actionability = incident.actionability
    record.classification = incident.classification
    record.priority = incident.priority
    record.suppressed_reason = incident.suppressed_reason
    record.suppressed_by = incident.suppressed_by
    record.suppressed_at = incident.suppressed_at
    record.updated_at = incident.updated_at or incident.created_at
    record.resolved_at = incident.resolved_at


def investigation_from_record(record: InvestigationRecord) -> Investigation:
    return Investigation(
        id=str(record.id),
        incident_id=str(record.incident_id),
        occurrence_id=str(record.occurrence_id),
        status=InvestigationStatus(record.status),
        evidence_hash=record.evidence_hash,
        input_version=record.input_version,
        prompt_version=record.prompt_version,
        model=record.model,
        created_at=record.created_at,
        confidence=Confidence(record.confidence) if record.confidence else None,
        usage=record.usage,
        result=record.result,
        error_summary=record.error_summary,
        completed_at=record.completed_at,
    )


def investigation_request_from_record(record: InvestigationRequestRecord) -> InvestigationRequest:
    return InvestigationRequest(
        id=str(record.id),
        incident_id=str(record.incident_id),
        occurrence_id=str(record.occurrence_id),
        mode=ScanMode(record.mode),
        source=record.source,
        priority=record.priority,
        evidence_hash=record.evidence_hash,
        status=InvestigationRequestStatus(record.status),
        scan_id=str(record.scan_id) if record.scan_id else None,
        build_id=str(record.build_id) if record.build_id else None,
        requested_by=record.requested_by,
        lease_owner=record.lease_owner,
        lease_expires_at=record.lease_expires_at,
        attempt_count=record.attempt_count,
        next_attempt_at=record.next_attempt_at,
        investigation_id=str(record.investigation_id) if record.investigation_id else None,
        error_summary=record.error_summary,
        created_at=record.created_at,
        updated_at=record.updated_at,
        completed_at=record.completed_at,
    )


def update_investigation_request_record(
    record: InvestigationRequestRecord, request: InvestigationRequest
) -> None:
    record.status = request.status.value
    record.priority = request.priority
    record.lease_owner = request.lease_owner
    record.lease_expires_at = request.lease_expires_at
    record.attempt_count = request.attempt_count
    record.next_attempt_at = request.next_attempt_at
    record.investigation_id = _optional_uuid(request.investigation_id)
    record.error_summary = request.error_summary
    record.updated_at = request.updated_at
    record.completed_at = request.completed_at


def action_from_record(record: ActionRecord) -> Action:
    return Action(
        id=str(record.id),
        incident_id=str(record.incident_id),
        occurrence_id=str(record.occurrence_id),
        action_type=ActionType(record.action_type),
        destination=record.destination,
        status=ActionStatus(record.status),
        rendered_payload=record.rendered_payload,
        template_version=record.template_version,
        idempotency_key=record.idempotency_key,
        external_identity=record.external_identity,
        external_reference=record.external_reference,
        lease_owner=record.lease_owner,
        lease_expires_at=record.lease_expires_at,
        attempt_count=record.attempt_count,
        retry_cycle=record.retry_cycle,
        next_attempt_at=record.next_attempt_at,
        failure_summary=record.failure_summary,
        created_at=record.created_at,
        updated_at=record.updated_at,
        completed_at=record.completed_at,
    )


def update_action_record(record: ActionRecord, action: Action) -> None:
    record.status = action.status.value
    record.rendered_payload = jsonable(action.rendered_payload)
    record.external_reference = action.external_reference
    record.lease_owner = action.lease_owner
    record.lease_expires_at = action.lease_expires_at
    record.attempt_count = action.attempt_count
    record.retry_cycle = action.retry_cycle
    record.next_attempt_at = action.next_attempt_at
    record.failure_summary = action.failure_summary
    record.updated_at = action.updated_at
    record.completed_at = action.completed_at


def delivery_attempt_from_record(record: DeliveryAttemptRecord) -> DeliveryAttempt:
    return DeliveryAttempt(
        id=str(record.id),
        action_id=str(record.action_id),
        retry_cycle=record.retry_cycle,
        attempt_number=record.attempt_number,
        status=DeliveryAttemptStatus(record.status),
        response_metadata=record.response_metadata,
        error_summary=record.error_summary,
        started_at=record.started_at,
        completed_at=record.completed_at,
    )


def jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): jsonable(nested) for key, nested in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [jsonable(item) for item in value]
    return value


def _optional_uuid(value: str | None) -> Any:
    if value is None:
        return None
    from uuid import UUID

    return UUID(value)
