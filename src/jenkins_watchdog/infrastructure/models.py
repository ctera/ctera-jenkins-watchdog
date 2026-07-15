"""SQLAlchemy ORM metadata for the v2 PostgreSQL schema."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

JSON_TYPE = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON_TYPE, list[str]: JSON_TYPE}


class ScanRecord(Base):
    __tablename__ = "scans"
    __table_args__ = (
        Index(
            "uq_scans_one_active",
            "active_slot",
            unique=True,
            postgresql_where=text("active_slot = true"),
            sqlite_where=text("active_slot = 1"),
        ),
        Index("ix_scans_claim", "status", "next_attempt_at", "lease_expires_at"),
        Index("ix_scans_created_cursor", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    categories: Mapped[list[str]] = mapped_column(JSON_TYPE, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    stage: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    active_slot: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    scheduled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    triggering_user_email: Mapped[str | None] = mapped_column(String(320))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(255))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    checks: Mapped[list[CheckExecutionRecord]] = relationship(back_populates="scan", cascade="all, delete-orphan")
    findings: Mapped[list[FindingRecord]] = relationship(back_populates="scan", cascade="all, delete-orphan")
    events: Mapped[list[ScanEventRecord]] = relationship(back_populates="scan", cascade="all, delete-orphan")


class CheckExecutionRecord(Base):
    __tablename__ = "check_executions"
    __table_args__ = (UniqueConstraint("scan_id", "check_name", name="uq_check_executions_scan_check"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scans.id", ondelete="CASCADE"), nullable=False, index=True)
    check_name: Mapped[str] = mapped_column(String(128), nullable=False)
    categories: Mapped[list[str]] = mapped_column(JSON_TYPE, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    failure_summary: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    scan: Mapped[ScanRecord] = relationship(back_populates="checks")
    findings: Mapped[list[FindingRecord]] = relationship(back_populates="check_execution")


class FindingRecord(Base):
    __tablename__ = "findings"
    __table_args__ = (
        UniqueConstraint("scan_id", "stable_identity", name="uq_findings_scan_identity"),
        Index("ix_findings_identity_observed", "stable_identity", "observed_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scans.id", ondelete="CASCADE"), nullable=False)
    check_execution_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("check_executions.id", ondelete="CASCADE"), nullable=False
    )
    stable_identity: Mapped[str] = mapped_column(String(64), nullable=False)
    rule_id: Mapped[str] = mapped_column(String(160), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(512), nullable=False)
    category: Mapped[str] = mapped_column(String(80), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    identity_dimensions: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    scan: Mapped[ScanRecord] = relationship(back_populates="findings")
    check_execution: Mapped[CheckExecutionRecord] = relationship(back_populates="findings")
    incident_link: Mapped[IncidentFindingRecord | None] = relationship(back_populates="finding", uselist=False)


class IncidentRecord(Base):
    __tablename__ = "incidents"
    __table_args__ = (
        UniqueConstraint("correlation_rule_id", "correlation_key", name="uq_incidents_correlation"),
        Index("ix_incidents_created_cursor", "created_at", "id"),
        Index("ix_incidents_status_updated", "status", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    correlation_rule_id: Mapped[str] = mapped_column(String(128), nullable=False)
    correlation_key: Mapped[str] = mapped_column(String(768), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    actionability: Mapped[str | None] = mapped_column(String(32))
    classification: Mapped[str | None] = mapped_column(String(80))
    priority: Mapped[str | None] = mapped_column(String(32))
    suppressed_reason: Mapped[str | None] = mapped_column(Text)
    suppressed_by: Mapped[str | None] = mapped_column(String(320))
    suppressed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    occurrences: Mapped[list[IncidentOccurrenceRecord]] = relationship(
        back_populates="incident", cascade="all, delete-orphan", order_by="IncidentOccurrenceRecord.number"
    )
    finding_links: Mapped[list[IncidentFindingRecord]] = relationship(back_populates="incident")
    investigations: Mapped[list[InvestigationRecord]] = relationship(back_populates="incident")
    investigation_requests: Mapped[list[InvestigationRequestRecord]] = relationship(back_populates="incident")
    actions: Mapped[list[ActionRecord]] = relationship(back_populates="incident")


class IncidentOccurrenceRecord(Base):
    __tablename__ = "incident_occurrences"
    __table_args__ = (UniqueConstraint("incident_id", "number", name="uq_occurrences_incident_number"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    responsible_checks: Mapped[list[str]] = mapped_column(JSON_TYPE, nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    incident: Mapped[IncidentRecord] = relationship(back_populates="occurrences")
    finding_links: Mapped[list[IncidentFindingRecord]] = relationship(back_populates="occurrence")


class IncidentFindingRecord(Base):
    __tablename__ = "incident_findings"
    __table_args__ = (UniqueConstraint("finding_id", name="uq_incident_findings_finding"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False)
    occurrence_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("incident_occurrences.id", ondelete="CASCADE"), nullable=False
    )
    finding_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("findings.id", ondelete="CASCADE"), nullable=False)
    linked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    incident: Mapped[IncidentRecord] = relationship(back_populates="finding_links")
    occurrence: Mapped[IncidentOccurrenceRecord] = relationship(back_populates="finding_links")
    finding: Mapped[FindingRecord] = relationship(back_populates="incident_link")


class InvestigationRecord(Base):
    __tablename__ = "investigations"
    __table_args__ = (Index("ix_investigations_incident_created", "incident_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False)
    occurrence_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("incident_occurrences.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    input_version: Mapped[str] = mapped_column(String(32), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(160), nullable=False)
    confidence: Mapped[str | None] = mapped_column(String(16))
    usage: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    result: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    error_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    incident: Mapped[IncidentRecord] = relationship(back_populates="investigations")


class InvestigationRequestRecord(Base):
    __tablename__ = "investigation_requests"
    __table_args__ = (
        Index(
            "uq_investigation_requests_active_incident",
            "incident_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
            sqlite_where=text("status IN ('queued', 'running')"),
        ),
        Index("ix_investigation_requests_claim", "status", "next_attempt_at", "lease_expires_at", "priority"),
        Index("ix_investigation_requests_incident_created", "incident_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False)
    occurrence_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("incident_occurrences.id", ondelete="CASCADE"), nullable=False
    )
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    scan_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("scans.id", ondelete="SET NULL"))
    build_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jenkins_builds.id", ondelete="SET NULL"))
    requested_by: Mapped[str | None] = mapped_column(String(320))
    budget_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="automatic")
    reserved_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(255))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    investigation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("investigations.id", ondelete="SET NULL")
    )
    error_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    incident: Mapped[IncidentRecord] = relationship(back_populates="investigation_requests")


class LLMCallRecord(Base):
    __tablename__ = "llm_calls"
    __table_args__ = (
        Index("ix_llm_calls_created", "created_at"),
        Index("ix_llm_calls_incident_created", "incident_id", "created_at"),
        Index("ix_llm_calls_investigation", "investigation_id", "created_at"),
        Index("ix_llm_calls_scan", "scan_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(160), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_read_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_creation_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    estimated_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    cost_source: Mapped[str] = mapped_column(String(32), nullable=False, default="unavailable")
    budget_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    incident_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("incidents.id", ondelete="SET NULL"))
    investigation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("investigations.id", ondelete="SET NULL")
    )
    scan_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("scans.id", ondelete="SET NULL"))
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON_TYPE, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AnalysisDecisionRecord(Base):
    __tablename__ = "analysis_decisions"
    __table_args__ = (
        Index("ix_analysis_decisions_incident_created", "incident_id", "created_at"),
        Index("ix_analysis_decisions_outcome_created", "outcome", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False)
    occurrence_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("incident_occurrences.id", ondelete="CASCADE"), nullable=False
    )
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scan_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("scans.id", ondelete="SET NULL"))
    request_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("investigation_requests.id", ondelete="SET NULL")
    )
    llm_call_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("llm_calls.id", ondelete="SET NULL"))
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON_TYPE, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ActionRecord(Base):
    __tablename__ = "actions"
    __table_args__ = (
        UniqueConstraint("destination", "idempotency_key", name="uq_actions_destination_idempotency"),
        Index("ix_actions_claim", "status", "next_attempt_at", "lease_expires_at"),
        Index("ix_actions_created_cursor", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False)
    occurrence_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("incident_occurrences.id", ondelete="CASCADE"), nullable=False
    )
    action_type: Mapped[str] = mapped_column(String(32), nullable=False)
    destination: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    rendered_payload: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    template_version: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(768), nullable=False)
    external_identity: Mapped[str] = mapped_column(String(768), nullable=False)
    external_reference: Mapped[str | None] = mapped_column(String(768))
    lease_owner: Mapped[str | None] = mapped_column(String(255))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retry_cycle: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    incident: Mapped[IncidentRecord] = relationship(back_populates="actions")
    attempts: Mapped[list[DeliveryAttemptRecord]] = relationship(back_populates="action")


class DeliveryAttemptRecord(Base):
    __tablename__ = "delivery_attempts"
    __table_args__ = (
        UniqueConstraint("action_id", "retry_cycle", "attempt_number", name="uq_delivery_attempt_cycle_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    action_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("actions.id", ondelete="CASCADE"), nullable=False)
    retry_cycle: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    response_metadata: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    error_summary: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    action: Mapped[ActionRecord] = relationship(back_populates="attempts")


class ScanEventRecord(Base):
    __tablename__ = "scan_events"
    __table_args__ = (
        UniqueConstraint("scan_id", "sequence", name="uq_scan_events_scan_sequence"),
        Index("ix_scan_events_replay", "scan_id", "sequence"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scans.id", ondelete="CASCADE"), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(String(80), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)

    scan: Mapped[ScanRecord] = relationship(back_populates="events")


class JenkinsJobRecord(Base):
    __tablename__ = "jenkins_jobs"
    __table_args__ = (
        Index("ix_jenkins_jobs_parent", "parent_full_name"),
        Index("ix_jenkins_jobs_last_build", "last_build_at"),
        Index("ix_jenkins_jobs_type", "job_type", "head_type"),
    )

    full_name: Mapped[str] = mapped_column(String(768), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(512), nullable=False)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    job_class: Mapped[str] = mapped_column(String(512), nullable=False)
    job_type: Mapped[str] = mapped_column(String(32), nullable=False)
    color: Mapped[str | None] = mapped_column(String(32))
    parent_full_name: Mapped[str | None] = mapped_column(String(768))
    first_build_number: Mapped[int | None] = mapped_column(Integer)
    first_build_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_build_number: Mapped[int | None] = mapped_column(Integer)
    last_build_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    watermark_build_number: Mapped[int | None] = mapped_column(Integer)
    history_coverage: Mapped[str] = mapped_column(String(40), nullable=False, default="unknown")
    head_type: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    head_name: Mapped[str | None] = mapped_column(String(768))
    source_provider: Mapped[str | None] = mapped_column(String(32))
    repository: Mapped[str | None] = mapped_column(String(768))
    source_url: Mapped[str | None] = mapped_column(String(2048))
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class JenkinsBuildRecord(Base):
    __tablename__ = "jenkins_builds"
    __table_args__ = (
        UniqueConstraint("job_full_name", "build_number", name="uq_jenkins_build_job_number"),
        Index("ix_jenkins_builds_started", "started_at", "id"),
        Index("ix_jenkins_builds_result_started", "result", "started_at"),
        Index("ix_jenkins_builds_logical_run", "logical_run_key"),
        Index("ix_jenkins_builds_signature", "failure_signature", "started_at"),
        Index("ix_jenkins_builds_enrichment", "enrichment_status", "started_at"),
        Index("ix_jenkins_builds_incident", "incident_id", "started_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    incident_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("incidents.id", ondelete="SET NULL"))
    job_full_name: Mapped[str] = mapped_column(
        ForeignKey("jenkins_jobs.full_name", ondelete="CASCADE"), nullable=False
    )
    build_number: Mapped[int] = mapped_column(Integer, nullable=False)
    result: Mapped[str] = mapped_column(String(24), nullable=False)
    building: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    upstream_job_full_name: Mapped[str | None] = mapped_column(String(768))
    upstream_build_number: Mapped[int | None] = mapped_column(Integer)
    root_job_full_name: Mapped[str | None] = mapped_column(String(768))
    root_build_number: Mapped[int | None] = mapped_column(Integer)
    logical_run_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    trigger_kind: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    source_provider: Mapped[str | None] = mapped_column(String(32))
    repository: Mapped[str | None] = mapped_column(String(768))
    change_number: Mapped[str | None] = mapped_column(String(128))
    change_url: Mapped[str | None] = mapped_column(String(2048))
    head_name: Mapped[str | None] = mapped_column(String(768))
    failed_stage: Mapped[str | None] = mapped_column(String(768))
    failure_classification: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    failure_signature: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    failure_summary: Mapped[str | None] = mapped_column(Text)
    propagated_failure: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    recovered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    novelty: Mapped[str] = mapped_column(String(32), nullable=False, default="unclassified")
    priority_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    priority_reasons: Mapped[list[str]] = mapped_column(JSON_TYPE, nullable=False, default=list)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    enrichment_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class JenkinsBuildEdgeRecord(Base):
    __tablename__ = "jenkins_build_edges"
    __table_args__ = (
        UniqueConstraint("upstream_build_id", "downstream_build_id", name="uq_jenkins_build_edge"),
        Index("ix_jenkins_build_edges_downstream", "downstream_build_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    upstream_build_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("jenkins_builds.id", ondelete="CASCADE"), nullable=False
    )
    downstream_build_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("jenkins_builds.id", ondelete="CASCADE"), nullable=False
    )
    relationship_type: Mapped[str] = mapped_column(String(32), nullable=False, default="triggered")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class JenkinsSyncStateRecord(Base):
    __tablename__ = "jenkins_sync_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(255))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cutoff_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_summary: Mapped[str | None] = mapped_column(Text)
    stats: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
