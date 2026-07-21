"""Persistence queries for fixed Jenkins failure reports."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jenkins_watchdog.infrastructure.models import (
    InvestigationRecord,
    InvestigationRequestRecord,
    JenkinsBuildRecord,
    JenkinsFailureReportBuildRecord,
    JenkinsFailureReportRecord,
    JenkinsJobRecord,
)


class SqlAlchemyJenkinsFailureReportRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, *, mode: str, start: datetime, end: datetime, now: datetime) -> dict[str, Any]:
        record = JenkinsFailureReportRecord(
            id=uuid.uuid4(), mode=mode, status="collecting", window_started_at=start,
            window_ended_at=end, coverage_exceptions=[], created_at=now, updated_at=now,
        )
        self._session.add(record)
        await self._session.flush()
        return _report(record)

    async def get(self, report_id: str) -> dict[str, Any] | None:
        try:
            record = await self._session.get(JenkinsFailureReportRecord, uuid.UUID(report_id))
        except ValueError:
            return None
        return _report(record) if record else None

    async def list(self, *, limit: int = 25) -> list[dict[str, Any]]:
        records = (await self._session.scalars(
            select(JenkinsFailureReportRecord).order_by(
                JenkinsFailureReportRecord.created_at.desc(), JenkinsFailureReportRecord.id.desc()
            ).limit(limit)
        )).all()
        return [_report(record) for record in records]

    async def fail(self, report_id: str, *, summary: str, now: datetime) -> None:
        record = await self._session.get(JenkinsFailureReportRecord, uuid.UUID(report_id))
        if record is None:
            return
        record.status, record.error_summary, record.updated_at, record.completed_at = "failed", summary[:1000], now, now
        await self._session.flush()

    async def add_build(self, *, report_id: str, build_id: str, now: datetime) -> dict[str, Any]:
        record = JenkinsFailureReportBuildRecord(
            id=uuid.uuid4(), report_id=uuid.UUID(report_id), build_id=uuid.UUID(build_id),
            status="queued", created_at=now, updated_at=now,
        )
        self._session.add(record)
        await self._session.flush()
        return _build(record)

    async def set_collected(
        self, report_id: str, *, jobs_discovered: int, failures_found: int,
        coverage_exceptions: list[dict[str, Any]], now: datetime,
    ) -> None:
        record = await self._session.get(JenkinsFailureReportRecord, uuid.UUID(report_id))
        if record is None:
            raise LookupError(report_id)
        record.jobs_discovered = jobs_discovered
        record.failures_found = failures_found
        record.coverage_exceptions = coverage_exceptions
        record.collected_at = now
        record.status = "investigating"
        record.updated_at = now
        await self._session.flush()

    async def attach_request(self, row_id: str, request_id: str, *, now: datetime) -> None:
        record = await self._session.get(JenkinsFailureReportBuildRecord, uuid.UUID(row_id))
        if record is None:
            raise LookupError(row_id)
        record.investigation_request_id = uuid.UUID(request_id)
        record.updated_at = now
        await self._session.flush()

    async def refresh(self, report_id: str, *, now: datetime) -> dict[str, Any] | None:
        report = await self._session.get(JenkinsFailureReportRecord, uuid.UUID(report_id))
        if report is None:
            return None
        if report.status in {"collecting", "failed", "cancelled"}:
            return _report(report)
        rows = (await self._session.scalars(
            select(JenkinsFailureReportBuildRecord).where(JenkinsFailureReportBuildRecord.report_id == report.id)
        )).all()
        waiting_reset = None
        terminal = {"explained", "evidence_gap", "agent_failed", "cancelled"}
        for row in rows:
            if row.investigation_request_id is None:
                continue
            request = await self._session.get(InvestigationRequestRecord, row.investigation_request_id)
            if request is None:
                row.status, row.error_summary = "agent_failed", "investigation request was not found"
            elif request.status == "running":
                row.status, row.error_summary = "running", None
            elif request.status == "queued" and request.next_attempt_at and request.next_attempt_at > now:
                row.status, row.error_summary = "waiting_budget", request.error_summary
                waiting_reset = max(waiting_reset, request.next_attempt_at) if waiting_reset else request.next_attempt_at
            elif request.status == "queued":
                row.status, row.error_summary = "queued", request.error_summary
            elif request.status == "failed":
                row.status, row.error_summary = "agent_failed", request.error_summary
            elif request.investigation_id:
                investigation = await self._session.get(InvestigationRecord, request.investigation_id)
                if investigation and investigation.status == "succeeded":
                    row.status, row.error_summary = "explained", None
                elif investigation and investigation.status == "partial":
                    row.status, row.error_summary = "evidence_gap", investigation.error_summary
                else:
                    row.status, row.error_summary = "agent_failed", request.error_summary
            row.updated_at = now
        statuses = [row.status for row in rows]
        report.budget_reset_at = waiting_reset
        report.updated_at = now
        if not rows or all(status in terminal for status in statuses):
            report.status, report.completed_at = "complete", now
        elif waiting_reset:
            report.status, report.completed_at = "waiting_budget", None
        else:
            report.status, report.completed_at = "investigating", None
        await self._session.flush()
        return _report(report)

    async def page(
        self, report_id: str, *, limit: int, offset: int, status: str | None = None, job: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        conditions = [JenkinsFailureReportBuildRecord.report_id == uuid.UUID(report_id)]
        if status:
            conditions.append(JenkinsFailureReportBuildRecord.status == status)
        if job:
            conditions.append(JenkinsBuildRecord.job_full_name.ilike(f"%{job}%"))
        statement = select(JenkinsFailureReportBuildRecord, JenkinsBuildRecord, JenkinsJobRecord).join(
            JenkinsBuildRecord, JenkinsBuildRecord.id == JenkinsFailureReportBuildRecord.build_id
        ).join(JenkinsJobRecord, JenkinsJobRecord.full_name == JenkinsBuildRecord.job_full_name).where(*conditions)
        total = int(await self._session.scalar(select(func.count()).select_from(statement.subquery())) or 0)
        rows = (await self._session.execute(statement.order_by(
            JenkinsBuildRecord.started_at.desc(), JenkinsFailureReportBuildRecord.id.desc()
        ).offset(offset).limit(limit))).all()
        items: list[dict[str, Any]] = []
        for row, build, job_record in rows:
            request = await self._session.get(InvestigationRequestRecord, row.investigation_request_id) if row.investigation_request_id else None
            investigation = await self._session.get(InvestigationRecord, request.investigation_id) if request and request.investigation_id else None
            items.append({
                **_build(row), "build_id": str(build.id), "job_name": build.job_full_name,
                "build_number": build.build_number, "result": build.result, "url": build.url,
                "started_at": build.started_at, "duration_ms": build.duration_ms,
                "source": {"provider": build.source_provider or job_record.source_provider, "repository": build.repository or job_record.repository, "change_number": build.change_number, "url": build.change_url or build.source_url},
                "investigation_request_id": str(row.investigation_request_id) if row.investigation_request_id else None,
                "investigation_status": investigation.status if investigation else None,
                "assessment": investigation.result if investigation else None,
            })
        return items, total

    async def status_counts(self, report_id: str) -> dict[str, int]:
        """Return report-wide status totals, independent of result pagination."""
        report_uuid = uuid.UUID(report_id)
        rows = (
            await self._session.execute(
                select(
                    JenkinsFailureReportBuildRecord.status,
                    func.count(JenkinsFailureReportBuildRecord.id),
                )
                .where(JenkinsFailureReportBuildRecord.report_id == report_uuid)
                .group_by(JenkinsFailureReportBuildRecord.status)
            )
        ).all()
        return {str(status): int(count) for status, count in rows}


def _report(record: JenkinsFailureReportRecord) -> dict[str, Any]:
    return {"id": str(record.id), "mode": record.mode, "status": record.status, "window_started_at": record.window_started_at,
            "window_ended_at": record.window_ended_at, "collected_at": record.collected_at, "jobs_discovered": record.jobs_discovered,
            "failures_found": record.failures_found, "coverage_exceptions": list(record.coverage_exceptions or []),
            "budget_reset_at": record.budget_reset_at, "error_summary": record.error_summary, "created_at": record.created_at,
            "updated_at": record.updated_at, "completed_at": record.completed_at}


def _build(record: JenkinsFailureReportBuildRecord) -> dict[str, Any]:
    return {"id": str(record.id), "status": record.status, "error_summary": record.error_summary,
            "created_at": record.created_at, "updated_at": record.updated_at}
