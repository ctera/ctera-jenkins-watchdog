"""SQLAlchemy implementations of the v2 repository ports."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from jenkins_watchdog.application.pagination import decode_cursor, encode_cursor
from jenkins_watchdog.application.types import CursorPage, EnqueueScan, ScanEvent
from jenkins_watchdog.domain.model import (
    Action,
    ActionStatus,
    AnalysisDecision,
    CheckResult,
    DeliveryAttempt,
    FindingObservation,
    Incident,
    Investigation,
    InvestigationBudgetKind,
    InvestigationRequest,
    InvestigationRequestStatus,
    LLMCall,
    Scan,
    ScanStatus,
)
from jenkins_watchdog.infrastructure.mappers import (
    action_from_record,
    analysis_decision_from_record,
    check_result_from_record,
    delivery_attempt_from_record,
    incident_from_record,
    investigation_from_record,
    investigation_request_from_record,
    jsonable,
    llm_call_from_record,
    observation_from_record,
    scan_from_record,
    update_action_record,
    update_incident_record,
    update_investigation_request_record,
    update_scan_record,
)
from jenkins_watchdog.infrastructure.models import (
    ActionRecord,
    AnalysisDecisionRecord,
    CheckExecutionRecord,
    DeliveryAttemptRecord,
    FindingRecord,
    IncidentFindingRecord,
    IncidentOccurrenceRecord,
    IncidentRecord,
    InvestigationRecord,
    InvestigationRequestRecord,
    LLMCallRecord,
    ScanEventRecord,
    ScanRecord,
)


def _uuid(value: str) -> uuid.UUID:
    return uuid.UUID(value)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SqlAlchemyScanRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lock_enqueue(self) -> None:
        # Serializes the short active-scan check/insert transaction across API processes.
        await self._session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": 7463616})

    async def active(self) -> Scan | None:
        record = await self._session.scalar(
            select(ScanRecord).where(ScanRecord.active_slot.is_(True)).order_by(ScanRecord.created_at)
        )
        return scan_from_record(record) if record else None

    async def add(self, request: EnqueueScan) -> Scan:
        now = _utcnow()
        scan = Scan(
            id=str(uuid.uuid4()),
            mode=request.mode,
            categories=frozenset(request.categories),
            status=ScanStatus.QUEUED,
            created_at=now,
            triggering_user_email=request.triggering_user_email,
            scheduled=request.scheduled,
            next_attempt_at=now,
            updated_at=now,
        )
        record = ScanRecord(id=_uuid(scan.id))
        update_scan_record(record, scan)
        self._session.add(record)
        await self._session.flush()
        return scan

    async def get(self, scan_id: str) -> Scan | None:
        record = await self._session.get(ScanRecord, _uuid(scan_id))
        return scan_from_record(record) if record else None

    async def list(self, *, limit: int, cursor: str | None = None) -> CursorPage:
        statement = select(ScanRecord).order_by(ScanRecord.created_at.desc(), ScanRecord.id.desc()).limit(limit + 1)
        if cursor:
            created_at, item_id = decode_cursor(cursor)
            statement = statement.where(
                or_(
                    ScanRecord.created_at < created_at,
                    and_(ScanRecord.created_at == created_at, ScanRecord.id < _uuid(item_id)),
                )
            )
        records = list((await self._session.scalars(statement)).all())
        page = records[:limit]
        next_cursor = None
        if len(records) > limit:
            last = page[-1]
            next_cursor = encode_cursor(last.created_at, str(last.id))
        return CursorPage(tuple(scan_from_record(record) for record in page), next_cursor)

    async def latest_completed(self) -> Scan | None:
        record = await self._session.scalar(
            select(ScanRecord)
            .where(
                ScanRecord.status.in_([ScanStatus.SUCCEEDED.value, ScanStatus.FAILED.value, ScanStatus.CANCELLED.value])
            )
            .order_by(ScanRecord.completed_at.desc(), ScanRecord.created_at.desc())
            .limit(1)
        )
        return scan_from_record(record) if record else None

    async def claim(self, *, owner: str, now: datetime, lease_seconds: int) -> Scan | None:
        exhausted = await self._session.scalar(
            select(ScanRecord)
            .where(
                ScanRecord.active_slot.is_(True),
                ScanRecord.status == ScanStatus.RUNNING.value,
                ScanRecord.attempt_count >= 3,
                ScanRecord.lease_expires_at.is_not(None),
                ScanRecord.lease_expires_at <= now,
            )
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if exhausted is not None:
            failed = scan_from_record(exhausted).fail(summary="worker lease expired after final scan attempt", now=now)
            update_scan_record(exhausted, failed)
            await self._session.flush()
            return None
        claimable = or_(
            and_(
                ScanRecord.status == ScanStatus.QUEUED.value,
                or_(ScanRecord.next_attempt_at.is_(None), ScanRecord.next_attempt_at <= now),
            ),
            and_(
                ScanRecord.status == ScanStatus.RUNNING.value,
                ScanRecord.lease_expires_at.is_not(None),
                ScanRecord.lease_expires_at <= now,
            ),
        )
        statement = (
            select(ScanRecord)
            .where(ScanRecord.active_slot.is_(True), ScanRecord.attempt_count < 3, claimable)
            .order_by(ScanRecord.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        record = await self._session.scalar(statement)
        if record is None:
            return None
        scan = scan_from_record(record).claim(owner=owner, now=now, lease_seconds=lease_seconds)
        update_scan_record(record, scan)
        await self._session.flush()
        return scan

    async def heartbeat(self, scan_id: str, *, owner: str, now: datetime, lease_seconds: int) -> bool:
        record = await self._session.scalar(select(ScanRecord).where(ScanRecord.id == _uuid(scan_id)).with_for_update())
        if record is None or record.lease_owner != owner or record.status != ScanStatus.RUNNING.value:
            return False
        scan = scan_from_record(record).heartbeat(owner=owner, now=now, lease_seconds=lease_seconds)
        update_scan_record(record, scan)
        await self._session.flush()
        return True

    async def request_cancel(self, scan_id: str, *, now: datetime) -> tuple[Scan, bool] | None:
        record = await self._session.scalar(select(ScanRecord).where(ScanRecord.id == _uuid(scan_id)).with_for_update())
        if record is None:
            return None
        current = scan_from_record(record)
        scan = current.request_cancel(now=now)
        update_scan_record(record, scan)
        await self._session.flush()
        return scan, scan != current

    async def save(self, scan: Scan) -> None:
        record = await self._session.get(ScanRecord, _uuid(scan.id))
        if record is None:
            raise LookupError(f"scan {scan.id} does not exist")
        update_scan_record(record, scan)
        await self._session.flush()


class SqlAlchemyCheckExecutionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, scan_id: str, check_name: str) -> CheckResult | None:
        record = await self._session.scalar(
            select(CheckExecutionRecord).where(
                CheckExecutionRecord.scan_id == _uuid(scan_id),
                CheckExecutionRecord.check_name == check_name,
            )
        )
        return check_result_from_record(record) if record else None

    async def save(self, scan_id: str, result: CheckResult) -> None:
        record = await self._session.scalar(
            select(CheckExecutionRecord).where(
                CheckExecutionRecord.scan_id == _uuid(scan_id),
                CheckExecutionRecord.check_name == result.check_name,
            )
        )
        now = _utcnow()
        if record is None:
            record = CheckExecutionRecord(
                id=uuid.uuid4(),
                scan_id=_uuid(scan_id),
                check_name=result.check_name,
                categories=sorted(result.categories),
                status=result.status.value,
                failure_summary=result.failure_summary,
                summary=jsonable(result.summary),
                started_at=result.started_at or now,
                completed_at=result.completed_at,
            )
            self._session.add(record)
        else:
            record.categories = sorted(result.categories)
            record.status = result.status.value
            record.failure_summary = result.failure_summary
            record.summary = jsonable(result.summary)
            record.started_at = result.started_at or record.started_at
            record.completed_at = result.completed_at
        await self._session.flush()

    async def for_scan(self, scan_id: str) -> tuple[CheckResult, ...]:
        records = (
            await self._session.scalars(
                select(CheckExecutionRecord)
                .where(CheckExecutionRecord.scan_id == _uuid(scan_id))
                .options(selectinload(CheckExecutionRecord.findings))
                .order_by(CheckExecutionRecord.started_at, CheckExecutionRecord.check_name)
            )
        ).all()
        return tuple(
            check_result_from_record(
                record,
                tuple(observation_from_record(finding) for finding in record.findings),
            )
            for record in records
        )


class SqlAlchemyFindingRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_observations(self, scan_id: str, observations: tuple[FindingObservation, ...]) -> None:
        if not observations:
            return
        executions = {
            record.check_name: record
            for record in (
                await self._session.scalars(
                    select(CheckExecutionRecord).where(CheckExecutionRecord.scan_id == _uuid(scan_id))
                )
            ).all()
        }
        existing = set(
            (
                await self._session.scalars(
                    select(FindingRecord.stable_identity).where(FindingRecord.scan_id == _uuid(scan_id))
                )
            ).all()
        )
        for observation in observations:
            if observation.stable_identity in existing:
                continue
            execution = executions.get(observation.check_name)
            if execution is None:
                raise LookupError(f"check execution {observation.check_name} must be stored before findings")
            self._session.add(
                FindingRecord(
                    id=uuid.uuid4(),
                    scan_id=_uuid(scan_id),
                    check_execution_id=execution.id,
                    stable_identity=observation.stable_identity,
                    rule_id=observation.rule_id,
                    resource_id=observation.resource_id,
                    category=observation.category,
                    severity=observation.severity.value,
                    summary=observation.summary,
                    identity_dimensions=jsonable(observation.identity_dimensions),
                    evidence=jsonable(observation.evidence),
                    observed_at=observation.observed_at,
                )
            )
            existing.add(observation.stable_identity)
        await self._session.flush()

    async def unlinked_for_scan(self, scan_id: str) -> tuple[FindingObservation, ...]:
        statement = (
            select(FindingRecord)
            .outerjoin(IncidentFindingRecord, IncidentFindingRecord.finding_id == FindingRecord.id)
            .where(FindingRecord.scan_id == _uuid(scan_id), IncidentFindingRecord.id.is_(None))
            .options(selectinload(FindingRecord.check_execution))
            .order_by(FindingRecord.observed_at, FindingRecord.id)
        )
        records = (await self._session.scalars(statement)).all()
        return tuple(observation_from_record(record) for record in records)


def _incident_load_options() -> tuple[Any, ...]:
    return (
        selectinload(IncidentRecord.occurrences)
        .selectinload(IncidentOccurrenceRecord.finding_links)
        .selectinload(IncidentFindingRecord.finding),
    )


class SqlAlchemyIncidentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, incident_id: str) -> Incident | None:
        record = await self._session.scalar(
            select(IncidentRecord).where(IncidentRecord.id == _uuid(incident_id)).options(*_incident_load_options())
        )
        return incident_from_record(record) if record else None

    async def get_by_correlation(self, rule_id: str, key: str) -> Incident | None:
        record = await self._session.scalar(
            select(IncidentRecord)
            .where(IncidentRecord.correlation_rule_id == rule_id, IncidentRecord.correlation_key == key)
            .options(*_incident_load_options())
        )
        return incident_from_record(record) if record else None

    async def list(
        self,
        *,
        limit: int,
        cursor: str | None = None,
        status: str | None = None,
        severity: str | None = None,
        source_type: str | None = None,
    ) -> CursorPage:
        statement = (
            select(IncidentRecord)
            .options(*_incident_load_options())
            .order_by(IncidentRecord.created_at.desc(), IncidentRecord.id.desc())
            .limit(limit + 1)
        )
        if status:
            statement = statement.where(IncidentRecord.status == status)
        if severity:
            statement = statement.where(IncidentRecord.severity == severity)
        if source_type:
            statement = statement.where(IncidentRecord.source["kind"].as_string() == source_type)
        if cursor:
            created_at, item_id = decode_cursor(cursor)
            statement = statement.where(
                or_(
                    IncidentRecord.created_at < created_at,
                    and_(IncidentRecord.created_at == created_at, IncidentRecord.id < _uuid(item_id)),
                )
            )
        records = list((await self._session.scalars(statement)).all())
        page = records[:limit]
        next_cursor = encode_cursor(page[-1].created_at, str(page[-1].id)) if len(records) > limit else None
        return CursorPage(tuple(incident_from_record(record) for record in page), next_cursor)

    async def active(self) -> tuple[Incident, ...]:
        records = (
            await self._session.scalars(
                select(IncidentRecord)
                .where(IncidentRecord.status.in_(["open", "suppressed"]))
                .options(*_incident_load_options())
                .order_by(IncidentRecord.created_at, IncidentRecord.id)
            )
        ).all()
        return tuple(incident_from_record(record) for record in records)

    async def observed_ids_for_scan(self, scan_id: str) -> frozenset[str]:
        ids = (
            await self._session.scalars(
                select(IncidentFindingRecord.incident_id)
                .join(FindingRecord, FindingRecord.id == IncidentFindingRecord.finding_id)
                .where(FindingRecord.scan_id == _uuid(scan_id))
                .distinct()
            )
        ).all()
        return frozenset(str(item) for item in ids)

    async def observations(self, incident_id: str) -> tuple[FindingObservation, ...]:
        records = (
            await self._session.scalars(
                select(FindingRecord)
                .join(IncidentFindingRecord, IncidentFindingRecord.finding_id == FindingRecord.id)
                .where(IncidentFindingRecord.incident_id == _uuid(incident_id))
                .options(selectinload(FindingRecord.check_execution))
                .order_by(FindingRecord.observed_at, FindingRecord.id)
            )
        ).all()
        return tuple(observation_from_record(record) for record in records)

    async def current_observations(self, incident_id: str) -> tuple[FindingObservation, ...]:
        latest_scan_id = await self._session.scalar(
            select(FindingRecord.scan_id)
            .join(IncidentFindingRecord, IncidentFindingRecord.finding_id == FindingRecord.id)
            .join(ScanRecord, ScanRecord.id == FindingRecord.scan_id)
            .where(IncidentFindingRecord.incident_id == _uuid(incident_id))
            .order_by(ScanRecord.created_at.desc(), FindingRecord.observed_at.desc())
            .limit(1)
        )
        if latest_scan_id is None:
            return ()
        records = (
            await self._session.scalars(
                select(FindingRecord)
                .join(IncidentFindingRecord, IncidentFindingRecord.finding_id == FindingRecord.id)
                .where(
                    IncidentFindingRecord.incident_id == _uuid(incident_id),
                    FindingRecord.scan_id == latest_scan_id,
                )
                .options(selectinload(FindingRecord.check_execution))
                .order_by(FindingRecord.severity, FindingRecord.resource_id)
            )
        ).all()
        return tuple(observation_from_record(record) for record in records)

    async def save(self, incident: Incident) -> None:
        statement = (
            select(IncidentRecord)
            .where(IncidentRecord.id == _uuid(incident.id))
            .options(selectinload(IncidentRecord.occurrences))
        )
        record = await self._session.scalar(statement)
        if record is None:
            record = IncidentRecord(
                id=_uuid(incident.id),
                correlation_rule_id=incident.correlation_rule_id,
                correlation_key=incident.correlation_key,
                status=incident.status.value,
                severity=incident.severity.value,
                title=incident.title,
                source=jsonable(incident.source),
                created_at=incident.created_at,
                updated_at=incident.updated_at or incident.created_at,
            )
            self._session.add(record)
        update_incident_record(record, incident)
        occurrence = next(
            (item for item in record.occurrences if item.id == _uuid(incident.current_occurrence.id)), None
        )
        if occurrence is None:
            occurrence = IncidentOccurrenceRecord(
                id=_uuid(incident.current_occurrence.id),
                incident_id=_uuid(incident.id),
                number=incident.current_occurrence.number,
                responsible_checks=sorted(incident.current_occurrence.responsible_checks),
                opened_at=incident.current_occurrence.opened_at,
                last_observed_at=incident.current_occurrence.last_observed_at or incident.current_occurrence.opened_at,
                resolved_at=incident.current_occurrence.resolved_at,
            )
            self._session.add(occurrence)
        else:
            occurrence.responsible_checks = sorted(incident.current_occurrence.responsible_checks)
            occurrence.last_observed_at = (
                incident.current_occurrence.last_observed_at or incident.current_occurrence.opened_at
            )
            occurrence.resolved_at = incident.current_occurrence.resolved_at
        await self._session.flush()

    async def link_observation(self, incident: Incident, observation: FindingObservation) -> None:
        finding = await self._session.scalar(
            select(FindingRecord).where(
                FindingRecord.scan_id == _uuid(observation.scan_id),
                FindingRecord.stable_identity == observation.stable_identity,
            )
        )
        if finding is None:
            raise LookupError("finding must be persisted before correlation")
        existing = await self._session.scalar(
            select(IncidentFindingRecord).where(IncidentFindingRecord.finding_id == finding.id)
        )
        if existing is not None:
            if existing.incident_id != _uuid(incident.id):
                raise ValueError("finding is already linked to another incident")
            return
        self._session.add(
            IncidentFindingRecord(
                id=uuid.uuid4(),
                incident_id=_uuid(incident.id),
                occurrence_id=_uuid(incident.current_occurrence.id),
                finding_id=finding.id,
                linked_at=observation.observed_at,
            )
        )
        await self._session.flush()


class SqlAlchemyInvestigationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def latest_for_incident(self, incident_id: str) -> Investigation | None:
        record = await self._session.scalar(
            select(InvestigationRecord)
            .where(InvestigationRecord.incident_id == _uuid(incident_id))
            .order_by(InvestigationRecord.created_at.desc())
            .limit(1)
        )
        return investigation_from_record(record) if record else None

    async def save(self, investigation: Investigation) -> None:
        record = await self._session.get(InvestigationRecord, _uuid(investigation.id))
        values = dict(
            incident_id=_uuid(investigation.incident_id),
            occurrence_id=_uuid(investigation.occurrence_id),
            status=investigation.status.value,
            evidence_hash=investigation.evidence_hash,
            input_version=investigation.input_version,
            prompt_version=investigation.prompt_version,
            model=investigation.model,
            confidence=investigation.confidence.value if investigation.confidence else None,
            usage=jsonable(investigation.usage),
            result=jsonable(investigation.result),
            error_summary=investigation.error_summary,
            created_at=investigation.created_at,
            completed_at=investigation.completed_at,
        )
        if record is None:
            self._session.add(InvestigationRecord(id=_uuid(investigation.id), **values))
        else:
            for name, value in values.items():
                setattr(record, name, value)
        await self._session.flush()


class SqlAlchemyInvestigationRequestRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, request_id: str) -> InvestigationRequest | None:
        record = await self._session.get(InvestigationRequestRecord, _uuid(request_id))
        return investigation_request_from_record(record) if record else None

    async def active_for_incident(self, incident_id: str) -> InvestigationRequest | None:
        record = await self._session.scalar(
            select(InvestigationRequestRecord)
            .where(
                InvestigationRequestRecord.incident_id == _uuid(incident_id),
                InvestigationRequestRecord.status.in_([
                    InvestigationRequestStatus.QUEUED.value,
                    InvestigationRequestStatus.RUNNING.value,
                ]),
            )
            .order_by(InvestigationRequestRecord.created_at.desc())
            .limit(1)
        )
        return investigation_request_from_record(record) if record else None

    async def latest_for_incident(self, incident_id: str) -> InvestigationRequest | None:
        record = await self._session.scalar(
            select(InvestigationRequestRecord)
            .where(InvestigationRequestRecord.incident_id == _uuid(incident_id))
            .order_by(InvestigationRequestRecord.created_at.desc(), InvestigationRequestRecord.id.desc())
            .limit(1)
        )
        return investigation_request_from_record(record) if record else None

    async def enqueue(self, request: InvestigationRequest) -> InvestigationRequest:
        await self._session.scalar(
            select(IncidentRecord.id).where(IncidentRecord.id == _uuid(request.incident_id)).with_for_update()
        )
        existing = await self.active_for_incident(request.incident_id)
        if existing is not None:
            return existing
        record = InvestigationRequestRecord(
            id=_uuid(request.id),
            incident_id=_uuid(request.incident_id),
            occurrence_id=_uuid(request.occurrence_id),
            mode=request.mode.value,
            source=request.source,
            priority=request.priority,
            evidence_hash=request.evidence_hash,
            status=request.status.value,
            scan_id=_uuid(request.scan_id) if request.scan_id else None,
            build_id=_uuid(request.build_id) if request.build_id else None,
            requested_by=request.requested_by,
            budget_kind=request.budget_kind.value,
            reserved_tokens=request.reserved_tokens,
            lease_owner=request.lease_owner,
            lease_expires_at=request.lease_expires_at,
            attempt_count=request.attempt_count,
            next_attempt_at=request.next_attempt_at,
            investigation_id=_uuid(request.investigation_id) if request.investigation_id else None,
            error_summary=request.error_summary,
            created_at=request.created_at,
            updated_at=request.updated_at,
            completed_at=request.completed_at,
        )
        self._session.add(record)
        await self._session.flush()
        return request

    async def lock_budget(self) -> None:
        bind = self._session.get_bind()
        if bind.dialect.name == "postgresql":
            await self._session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": 7463617})

    async def active_reserved_tokens(
        self,
        *,
        budget_kind: InvestigationBudgetKind | None = None,
    ) -> int:
        statement = select(func.coalesce(func.sum(InvestigationRequestRecord.reserved_tokens), 0)).where(
            InvestigationRequestRecord.status.in_([
                InvestigationRequestStatus.QUEUED.value,
                InvestigationRequestStatus.RUNNING.value,
            ])
        )
        if budget_kind is not None:
            statement = statement.where(InvestigationRequestRecord.budget_kind == budget_kind.value)
        return int(await self._session.scalar(statement) or 0)

    async def claim(self, *, owner: str, now: datetime, lease_seconds: int) -> InvestigationRequest | None:
        queued = and_(
            InvestigationRequestRecord.status == InvestigationRequestStatus.QUEUED.value,
            or_(
                InvestigationRequestRecord.next_attempt_at.is_(None),
                InvestigationRequestRecord.next_attempt_at <= now,
            ),
        )
        expired = and_(
            InvestigationRequestRecord.status == InvestigationRequestStatus.RUNNING.value,
            InvestigationRequestRecord.lease_expires_at.is_not(None),
            InvestigationRequestRecord.lease_expires_at <= now,
        )
        record = await self._session.scalar(
            select(InvestigationRequestRecord)
            .where(or_(queued, expired))
            .order_by(InvestigationRequestRecord.priority.desc(), InvestigationRequestRecord.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if record is None:
            return None
        request = investigation_request_from_record(record).claim(
            owner=owner, now=now, lease_seconds=lease_seconds
        )
        update_investigation_request_record(record, request)
        await self._session.flush()
        return request

    async def heartbeat(self, request_id: str, *, owner: str, now: datetime, lease_seconds: int) -> bool:
        record = await self._session.scalar(
            select(InvestigationRequestRecord)
            .where(InvestigationRequestRecord.id == _uuid(request_id))
            .with_for_update()
        )
        if (
            record is None
            or record.status != InvestigationRequestStatus.RUNNING.value
            or record.lease_owner != owner
        ):
            return False
        request = investigation_request_from_record(record).heartbeat(
            owner=owner, now=now, lease_seconds=lease_seconds
        )
        update_investigation_request_record(record, request)
        await self._session.flush()
        return True

    async def save(self, request: InvestigationRequest) -> None:
        record = await self._session.get(InvestigationRequestRecord, _uuid(request.id))
        if record is None:
            raise LookupError(f"investigation request {request.id} does not exist")
        update_investigation_request_record(record, request)
        await self._session.flush()


class SqlAlchemyAnalysisDecisionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def latest_for_incident(self, incident_id: str) -> AnalysisDecision | None:
        record = await self._session.scalar(
            select(AnalysisDecisionRecord)
            .where(AnalysisDecisionRecord.incident_id == _uuid(incident_id))
            .order_by(AnalysisDecisionRecord.created_at.desc(), AnalysisDecisionRecord.id.desc())
            .limit(1)
        )
        return analysis_decision_from_record(record) if record else None

    async def for_incident(self, incident_id: str, *, limit: int = 50) -> tuple[AnalysisDecision, ...]:
        records = (
            await self._session.scalars(
                select(AnalysisDecisionRecord)
                .where(AnalysisDecisionRecord.incident_id == _uuid(incident_id))
                .order_by(AnalysisDecisionRecord.created_at.desc(), AnalysisDecisionRecord.id.desc())
                .limit(limit)
            )
        ).all()
        return tuple(analysis_decision_from_record(record) for record in records)

    async def save(self, decision: AnalysisDecision) -> None:
        record = AnalysisDecisionRecord(
            id=_uuid(decision.id),
            incident_id=_uuid(decision.incident_id),
            occurrence_id=_uuid(decision.occurrence_id),
            outcome=decision.outcome.value,
            reason_code=decision.reason_code,
            reason=decision.reason,
            source=decision.source[:32],
            mode=decision.mode.value,
            priority=decision.priority,
            evidence_hash=decision.evidence_hash,
            scan_id=_uuid(decision.scan_id) if decision.scan_id else None,
            request_id=_uuid(decision.request_id) if decision.request_id else None,
            llm_call_id=_uuid(decision.llm_call_id) if decision.llm_call_id else None,
            metadata_json=jsonable(decision.metadata),
            created_at=decision.created_at,
        )
        self._session.add(record)
        await self._session.flush()


class SqlAlchemyLLMCallRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save_many(self, calls: tuple[LLMCall, ...]) -> None:
        for call in calls:
            if await self._session.get(LLMCallRecord, _uuid(call.id)) is not None:
                continue
            self._session.add(
                LLMCallRecord(
                    id=_uuid(call.id),
                    purpose=call.purpose[:32],
                    model=call.model[:160],
                    prompt_tokens=call.prompt_tokens,
                    completion_tokens=call.completion_tokens,
                    cache_read_input_tokens=call.cache_read_input_tokens,
                    cache_creation_input_tokens=call.cache_creation_input_tokens,
                    total_tokens=call.total_tokens,
                    estimated_cost_usd=call.estimated_cost_usd,
                    cost_source=call.cost_source[:32],
                    budget_kind=call.budget_kind.value if call.budget_kind else None,
                    incident_id=_uuid(call.incident_id) if call.incident_id else None,
                    investigation_id=_uuid(call.investigation_id) if call.investigation_id else None,
                    scan_id=_uuid(call.scan_id) if call.scan_id else None,
                    metadata_json=jsonable(call.metadata),
                    created_at=call.created_at,
                )
            )
        await self._session.flush()

    async def for_investigation(self, investigation_id: str) -> tuple[LLMCall, ...]:
        records = (
            await self._session.scalars(
                select(LLMCallRecord)
                .where(LLMCallRecord.investigation_id == _uuid(investigation_id))
                .order_by(LLMCallRecord.created_at, LLMCallRecord.id)
            )
        ).all()
        return tuple(llm_call_from_record(record) for record in records)

    async def summary_since(
        self,
        since: datetime,
        *,
        budget_kind: InvestigationBudgetKind | None = None,
    ) -> dict[str, Any]:
        conditions = [LLMCallRecord.created_at >= since]
        if budget_kind is not None:
            conditions.append(LLMCallRecord.budget_kind == budget_kind.value)
        return await self._summary(*conditions)

    async def summary_for_scan(self, scan_id: str) -> dict[str, Any]:
        return await self._summary(LLMCallRecord.scan_id == _uuid(scan_id))

    async def _summary(self, *conditions: Any) -> dict[str, Any]:
        columns = (
            func.count(LLMCallRecord.id),
            func.coalesce(func.sum(LLMCallRecord.prompt_tokens), 0),
            func.coalesce(func.sum(LLMCallRecord.completion_tokens), 0),
            func.coalesce(func.sum(LLMCallRecord.cache_read_input_tokens), 0),
            func.coalesce(func.sum(LLMCallRecord.cache_creation_input_tokens), 0),
            func.coalesce(func.sum(LLMCallRecord.total_tokens), 0),
            func.coalesce(func.sum(LLMCallRecord.estimated_cost_usd), 0),
        )
        row = (await self._session.execute(select(*columns).where(*conditions))).one()
        purpose_rows = (
            await self._session.execute(
                select(
                    LLMCallRecord.purpose,
                    func.count(LLMCallRecord.id),
                    func.coalesce(func.sum(LLMCallRecord.total_tokens), 0),
                    func.coalesce(func.sum(LLMCallRecord.estimated_cost_usd), 0),
                )
                .where(*conditions)
                .group_by(LLMCallRecord.purpose)
                .order_by(LLMCallRecord.purpose)
            )
        ).all()
        return {
            "call_count": int(row[0] or 0),
            "prompt_tokens": int(row[1] or 0),
            "completion_tokens": int(row[2] or 0),
            "cache_read_input_tokens": int(row[3] or 0),
            "cache_creation_input_tokens": int(row[4] or 0),
            "total_tokens": int(row[5] or 0),
            "estimated_cost_usd": float(row[6] or 0),
            "by_purpose": {
                purpose: {
                    "call_count": int(count),
                    "total_tokens": int(tokens or 0),
                    "estimated_cost_usd": float(cost or 0),
                }
                for purpose, count, tokens, cost in purpose_rows
            },
        }


class SqlAlchemyActionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, action_id: str) -> Action | None:
        record = await self._session.get(ActionRecord, _uuid(action_id))
        return action_from_record(record) if record else None

    async def add(self, action: Action) -> Action:
        existing = await self._session.scalar(
            select(ActionRecord).where(
                ActionRecord.destination == action.destination,
                ActionRecord.idempotency_key == action.idempotency_key,
            )
        )
        if existing:
            return action_from_record(existing)
        record = ActionRecord(
            id=_uuid(action.id),
            incident_id=_uuid(action.incident_id),
            occurrence_id=_uuid(action.occurrence_id),
            action_type=action.action_type.value,
            destination=action.destination,
            status=action.status.value,
            rendered_payload=jsonable(action.rendered_payload),
            template_version=action.template_version,
            idempotency_key=action.idempotency_key,
            external_identity=action.external_identity,
            external_reference=action.external_reference,
            lease_owner=action.lease_owner,
            lease_expires_at=action.lease_expires_at,
            attempt_count=action.attempt_count,
            retry_cycle=action.retry_cycle,
            next_attempt_at=action.next_attempt_at,
            failure_summary=action.failure_summary,
            created_at=action.created_at,
            updated_at=action.updated_at,
            completed_at=action.completed_at,
        )
        self._session.add(record)
        await self._session.flush()
        return action

    async def list(
        self,
        *,
        limit: int,
        cursor: str | None = None,
        status: str | None = None,
        action_type: str | None = None,
        incident_id: str | None = None,
    ) -> CursorPage:
        statement = (
            select(ActionRecord).order_by(ActionRecord.created_at.desc(), ActionRecord.id.desc()).limit(limit + 1)
        )
        if status:
            statement = statement.where(ActionRecord.status == status)
        if action_type:
            statement = statement.where(ActionRecord.action_type == action_type)
        if incident_id:
            statement = statement.where(ActionRecord.incident_id == _uuid(incident_id))
        if cursor:
            created_at, item_id = decode_cursor(cursor)
            statement = statement.where(
                or_(
                    ActionRecord.created_at < created_at,
                    and_(ActionRecord.created_at == created_at, ActionRecord.id < _uuid(item_id)),
                )
            )
        records = list((await self._session.scalars(statement)).all())
        page = records[:limit]
        next_cursor = encode_cursor(page[-1].created_at, str(page[-1].id)) if len(records) > limit else None
        return CursorPage(tuple(action_from_record(record) for record in page), next_cursor)

    async def for_incident(self, incident_id: str) -> tuple[Action, ...]:
        records = (
            await self._session.scalars(
                select(ActionRecord)
                .where(ActionRecord.incident_id == _uuid(incident_id))
                .order_by(ActionRecord.created_at, ActionRecord.id)
            )
        ).all()
        return tuple(action_from_record(record) for record in records)

    async def claim(self, *, owner: str, now: datetime, lease_seconds: int) -> Action | None:
        pending = and_(
            ActionRecord.status.in_([ActionStatus.PENDING.value, ActionStatus.RETRY_SCHEDULED.value]),
            or_(ActionRecord.next_attempt_at.is_(None), ActionRecord.next_attempt_at <= now),
            or_(ActionRecord.lease_expires_at.is_(None), ActionRecord.lease_expires_at <= now),
        )
        expired_running = and_(
            ActionRecord.status == ActionStatus.RUNNING.value,
            ActionRecord.lease_expires_at.is_not(None),
            ActionRecord.lease_expires_at <= now,
        )
        statement = (
            select(ActionRecord)
            .where(or_(pending, expired_running))
            .order_by(ActionRecord.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        record = await self._session.scalar(statement)
        if record is None:
            return None
        action = action_from_record(record).claim(owner=owner, now=now, lease_seconds=lease_seconds)
        update_action_record(record, action)
        await self._session.flush()
        return action

    async def heartbeat(self, action_id: str, *, owner: str, now: datetime, lease_seconds: int) -> bool:
        record = await self._session.scalar(
            select(ActionRecord).where(ActionRecord.id == _uuid(action_id)).with_for_update()
        )
        if record is None or record.lease_owner != owner or record.status != ActionStatus.RUNNING.value:
            return False
        action = action_from_record(record).heartbeat(owner=owner, now=now, lease_seconds=lease_seconds)
        update_action_record(record, action)
        await self._session.flush()
        return True

    async def save(self, action: Action) -> None:
        record = await self._session.get(ActionRecord, _uuid(action.id))
        if record is None:
            raise LookupError(f"action {action.id} does not exist")
        update_action_record(record, action)
        await self._session.flush()


class SqlAlchemyDeliveryAttemptRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(self, attempt: DeliveryAttempt) -> None:
        record = await self._session.get(DeliveryAttemptRecord, _uuid(attempt.id))
        values = dict(
            action_id=_uuid(attempt.action_id),
            retry_cycle=attempt.retry_cycle,
            attempt_number=attempt.attempt_number,
            status=attempt.status.value,
            response_metadata=jsonable(attempt.response_metadata),
            error_summary=attempt.error_summary,
            started_at=attempt.started_at,
            completed_at=attempt.completed_at,
        )
        if record is None:
            self._session.add(DeliveryAttemptRecord(id=_uuid(attempt.id), **values))
        else:
            for name, value in values.items():
                setattr(record, name, value)
        await self._session.flush()

    async def for_action(self, action_id: str) -> tuple[DeliveryAttempt, ...]:
        records = (
            await self._session.scalars(
                select(DeliveryAttemptRecord)
                .where(DeliveryAttemptRecord.action_id == _uuid(action_id))
                .order_by(
                    DeliveryAttemptRecord.retry_cycle,
                    DeliveryAttemptRecord.attempt_number,
                )
            )
        ).all()
        return tuple(delivery_attempt_from_record(record) for record in records)


class SqlAlchemyEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, scan_id: str, event_type: str, payload: dict[str, Any], *, now: datetime) -> ScanEvent:
        await self._session.scalar(select(ScanRecord.id).where(ScanRecord.id == _uuid(scan_id)).with_for_update())
        last_sequence = await self._session.scalar(
            select(func.max(ScanEventRecord.sequence)).where(ScanEventRecord.scan_id == _uuid(scan_id))
        )
        event = ScanEvent(
            scan_id=scan_id,
            sequence=(last_sequence or 0) + 1,
            type=event_type,
            occurred_at=now,
            payload_version=1,
            payload=payload,
        )
        self._session.add(
            ScanEventRecord(
                scan_id=_uuid(scan_id),
                sequence=event.sequence,
                type=event.type,
                occurred_at=event.occurred_at,
                payload_version=event.payload_version,
                payload=jsonable(event.payload),
            )
        )
        await self._session.flush()
        return event

    async def after(self, scan_id: str, sequence: int, *, limit: int = 500) -> tuple[ScanEvent, ...]:
        records = (
            await self._session.scalars(
                select(ScanEventRecord)
                .where(ScanEventRecord.scan_id == _uuid(scan_id), ScanEventRecord.sequence > sequence)
                .order_by(ScanEventRecord.sequence)
                .limit(limit)
            )
        ).all()
        return tuple(
            ScanEvent(
                scan_id=str(record.scan_id),
                sequence=record.sequence,
                type=record.type,
                occurred_at=record.occurred_at,
                payload_version=record.payload_version,
                payload=record.payload,
            )
            for record in records
        )
