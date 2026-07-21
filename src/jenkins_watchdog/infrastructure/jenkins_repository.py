"""PostgreSQL repository for Jenkins catalog, build observations, and operator views."""

from __future__ import annotations

import math
import uuid
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from jenkins_watchdog.application.pagination import decode_cursor, encode_cursor
from jenkins_watchdog.application.types import CursorPage
from jenkins_watchdog.domain.jenkins import (
    JenkinsBuildAttribution,
    JenkinsBuildEnrichment,
    JenkinsBuildSnapshot,
    JenkinsJobSnapshot,
    JenkinsNovelty,
    JenkinsSyncStats,
)
from jenkins_watchdog.domain.source import SourceAttribution, SourceKind
from jenkins_watchdog.infrastructure.models import (
    IncidentRecord,
    JenkinsBuildEdgeRecord,
    JenkinsBuildRecord,
    JenkinsJobRecord,
    JenkinsSyncStateRecord,
)

_FAILURE_RESULTS = frozenset({"FAILURE", "UNSTABLE", "ABORTED"})


class SqlAlchemyJenkinsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim_sync(self, *, owner: str, now: datetime, lease_seconds: int) -> bool:
        record = await self._session.scalar(
            select(JenkinsSyncStateRecord).where(JenkinsSyncStateRecord.id == 1).with_for_update()
        )
        if record is None:
            record = JenkinsSyncStateRecord(id=1, status="idle", stats={}, updated_at=now)
            self._session.add(record)
            await self._session.flush()
        if record.status == "running" and record.lease_expires_at and record.lease_expires_at > now:
            return False
        record.status = "running"
        record.lease_owner = owner
        record.lease_expires_at = now + timedelta(seconds=lease_seconds)
        record.started_at = now
        record.failure_summary = None
        record.updated_at = now
        await self._session.flush()
        return True

    async def heartbeat_sync(self, *, owner: str, now: datetime, lease_seconds: int) -> bool:
        record = await self._session.scalar(
            select(JenkinsSyncStateRecord).where(JenkinsSyncStateRecord.id == 1).with_for_update()
        )
        if record is None or record.status != "running" or record.lease_owner != owner:
            return False
        record.lease_expires_at = now + timedelta(seconds=lease_seconds)
        record.updated_at = now
        await self._session.flush()
        return True

    async def complete_sync(self, *, owner: str, stats: JenkinsSyncStats) -> None:
        record = await self._session.scalar(
            select(JenkinsSyncStateRecord).where(JenkinsSyncStateRecord.id == 1).with_for_update()
        )
        if record is None or record.lease_owner != owner:
            raise RuntimeError("Jenkins sync lease is not owned")
        payload = asdict(stats)
        for key in ("started_at", "completed_at", "cutoff_at"):
            payload[key] = payload[key].isoformat()
        payload["errors"] = list(stats.errors)
        payload["details"] = dict(stats.details)
        record.status = "succeeded" if not stats.errors else "partial"
        record.lease_owner = None
        record.lease_expires_at = None
        record.completed_at = stats.completed_at
        record.cutoff_at = stats.cutoff_at
        record.failure_summary = None
        record.stats = payload
        record.updated_at = stats.completed_at
        await self._session.flush()

    async def fail_sync(self, *, owner: str, now: datetime, summary: str) -> None:
        record = await self._session.scalar(
            select(JenkinsSyncStateRecord).where(JenkinsSyncStateRecord.id == 1).with_for_update()
        )
        if record is None or record.lease_owner != owner:
            return
        record.status = "failed"
        record.lease_owner = None
        record.lease_expires_at = None
        record.completed_at = now
        record.failure_summary = summary[:1000]
        record.updated_at = now
        await self._session.flush()

    async def upsert_jobs(self, jobs: tuple[JenkinsJobSnapshot, ...], *, now: datetime) -> None:
        if not jobs:
            return
        records = {
            item.full_name: item
            for item in (
                await self._session.scalars(
                    select(JenkinsJobRecord).where(JenkinsJobRecord.full_name.in_([job.full_name for job in jobs]))
                )
            ).all()
        }
        for job in jobs:
            record = records.get(job.full_name)
            if record is None:
                record = JenkinsJobRecord(
                    full_name=job.full_name,
                    display_name=job.display_name,
                    url=job.url,
                    job_class=job.job_class,
                    job_type=job.job_type,
                    color=job.color,
                    parent_full_name=job.parent_full_name,
                    first_build_number=job.first_build_number,
                    first_build_at=job.first_build_at,
                    last_build_number=job.last_build_number,
                    last_build_at=job.last_build_at,
                    history_coverage="not_applicable" if job.is_container else "unknown",
                    head_type=job.head_type.value,
                    head_name=job.head_name,
                    source_provider=job.source_provider,
                    repository=job.repository,
                    source_url=job.source_url,
                    discovered_at=now,
                    updated_at=now,
                )
                self._session.add(record)
                records[job.full_name] = record
                continue
            record.display_name = job.display_name
            record.url = job.url
            record.job_class = job.job_class or record.job_class
            record.job_type = job.job_type if job.job_class else record.job_type
            record.color = job.color
            record.parent_full_name = job.parent_full_name
            record.first_build_number = job.first_build_number or record.first_build_number
            record.first_build_at = job.first_build_at or record.first_build_at
            record.last_build_number = job.last_build_number or record.last_build_number
            record.last_build_at = job.last_build_at or record.last_build_at
            if job.head_type.value != "unknown":
                record.head_type = job.head_type.value
            record.head_name = job.head_name or record.head_name
            record.source_provider = job.source_provider or record.source_provider
            record.repository = job.repository or record.repository
            record.source_url = job.source_url or record.source_url
            record.updated_at = now
        await self._session.flush()

    async def watermarks(self, job_names: tuple[str, ...]) -> dict[str, int | None]:
        if not job_names:
            return {}
        rows = (
            await self._session.execute(
                select(JenkinsJobRecord.full_name, JenkinsJobRecord.watermark_build_number).where(
                    JenkinsJobRecord.full_name.in_(job_names)
                )
            )
        ).all()
        return {str(name): number for name, number in rows}

    async def running_build_numbers(self) -> dict[str, tuple[int, ...]]:
        rows = (
            await self._session.execute(
                select(JenkinsBuildRecord.job_full_name, JenkinsBuildRecord.build_number)
                .where(JenkinsBuildRecord.building.is_(True))
                .order_by(JenkinsBuildRecord.job_full_name, JenkinsBuildRecord.build_number)
            )
        ).all()
        grouped: dict[str, list[int]] = defaultdict(list)
        for job_name, build_number in rows:
            grouped[str(job_name)].append(int(build_number))
        return {job_name: tuple(numbers) for job_name, numbers in grouped.items()}

    async def upsert_builds(
        self,
        builds: tuple[JenkinsBuildSnapshot, ...],
        *,
        now: datetime,
    ) -> int:
        if not builds:
            return 0
        grouped: dict[str, list[JenkinsBuildSnapshot]] = defaultdict(list)
        for build in builds:
            grouped[build.job_full_name].append(build)
        new_count = 0
        for job_name, job_builds in grouped.items():
            numbers = [item.number for item in job_builds]
            existing = {
                item.build_number: item
                for item in (
                    await self._session.scalars(
                        select(JenkinsBuildRecord).where(
                            JenkinsBuildRecord.job_full_name == job_name,
                            JenkinsBuildRecord.build_number.in_(numbers),
                        )
                    )
                ).all()
            }
            for build in job_builds:
                record = existing.get(build.number)
                completed_at = None if build.building else build.started_at + timedelta(milliseconds=build.duration_ms)
                if record is None:
                    new_count += 1
                    failure_like = build.result in _FAILURE_RESULTS
                    record = JenkinsBuildRecord(
                        id=uuid.uuid4(),
                        job_full_name=build.job_full_name,
                        build_number=build.number,
                        result=build.result,
                        building=build.building,
                        url=build.url,
                        started_at=build.started_at,
                        completed_at=completed_at,
                        duration_ms=build.duration_ms,
                        logical_run_key=f"{build.job_full_name}#{build.number}",
                        trigger_kind="unknown",
                        source_kind="unresolved",
                        source_status="pending" if failure_like else "not_needed",
                        source_resolution_method="none",
                        source_allow_mr_comments=False,
                        source_provenance=[],
                        failure_classification="unknown",
                        failure_signature="",
                        propagated_failure=False,
                        recovered=False,
                        novelty=JenkinsNovelty.UNCLASSIFIED.value,
                        priority_score=0,
                        priority_reasons=[],
                        evidence={},
                        enrichment_status="pending" if failure_like else "not_needed",
                        ingested_at=now,
                        updated_at=now,
                    )
                    self._session.add(record)
                    existing[build.number] = record
                else:
                    became_failure = record.result not in _FAILURE_RESULTS and build.result in _FAILURE_RESULTS
                    record.result = build.result
                    record.building = build.building
                    record.url = build.url
                    record.started_at = build.started_at
                    record.completed_at = completed_at
                    record.duration_ms = build.duration_ms
                    record.updated_at = now
                    if became_failure:
                        record.enrichment_status = "pending"
                        record.source_status = "pending"
            job = await self._session.get(JenkinsJobRecord, job_name)
            if job is not None:
                newest = max(numbers)
                job.watermark_build_number = max(job.watermark_build_number or 0, newest)
                job.updated_at = now
        await self._session.flush()
        return new_count

    async def set_job_coverage(self, job_name: str, coverage: str, *, now: datetime) -> None:
        if coverage == "not_applicable":
            return
        record = await self._session.get(JenkinsJobRecord, job_name)
        if record is not None:
            record.history_coverage = coverage
            record.updated_at = now
            await self._session.flush()

    async def pending_enrichment(
        self,
        *,
        limit: int,
        log_limit: int,
    ) -> tuple[JenkinsBuildSnapshot, ...]:
        result_rank = case(
            (JenkinsBuildRecord.result == "FAILURE", 0),
            (JenkinsBuildRecord.result == "UNSTABLE", 1),
            else_=2,
        )
        records = (
            await self._session.scalars(
                select(JenkinsBuildRecord)
                .where(JenkinsBuildRecord.enrichment_status == "pending")
                .order_by(result_rank, JenkinsBuildRecord.started_at.desc())
                .limit(limit)
            )
        ).all()
        if not records and log_limit > 0:
            records = (
                await self._session.scalars(
                select(JenkinsBuildRecord)
                .where(JenkinsBuildRecord.enrichment_status == "log_pending")
                .order_by(
                    JenkinsBuildRecord.updated_at.asc(),
                    result_rank,
                    JenkinsBuildRecord.started_at.desc(),
                )
                .limit(log_limit)
            )
        ).all()
        return tuple(_snapshot(record) for record in records)

    async def pending_attribution(self, *, limit: int) -> tuple[JenkinsBuildSnapshot, ...]:
        records = (
            await self._session.scalars(
                select(JenkinsBuildRecord)
                .where(
                    JenkinsBuildRecord.source_status == "pending",
                    JenkinsBuildRecord.result.in_(_FAILURE_RESULTS),
                )
                .order_by(JenkinsBuildRecord.started_at.desc(), JenkinsBuildRecord.id.desc())
                .limit(max(limit * 4, limit))
            )
        ).all()
        selected: list[JenkinsBuildRecord] = []
        logical_runs: set[str] = set()
        for record in records:
            if record.logical_run_key in logical_runs:
                continue
            logical_runs.add(record.logical_run_key)
            selected.append(record)
            if len(selected) >= limit:
                break
        return tuple(_snapshot(record) for record in selected)

    async def save_attribution(
        self,
        attribution: JenkinsBuildAttribution,
        *,
        now: datetime,
    ) -> tuple[str, ...]:
        return await self._persist_attribution(attribution, now=now)

    async def mark_attribution_failed(
        self,
        job_name: str,
        number: int,
        *,
        now: datetime,
        summary: str,
    ) -> None:
        record = await self._session.scalar(
            select(JenkinsBuildRecord).where(
                JenkinsBuildRecord.job_full_name == job_name,
                JenkinsBuildRecord.build_number == number,
            )
        )
        if record is None:
            return
        attempt_count = int(record.evidence.get("source_attribution_attempt_count") or 0) + 1
        record.source_status = "pending" if attempt_count < 3 else "unavailable"
        record.source_reason = summary[:512]
        record.evidence = {
            **record.evidence,
            "source_attribution_attempt_count": attempt_count,
            "source_attribution_error": summary[:500],
        }
        record.updated_at = now
        await self._session.flush()

    async def save_enrichment(
        self,
        enrichment: JenkinsBuildEnrichment,
        *,
        now: datetime,
    ) -> tuple[str, ...]:
        incident_ids = await self._persist_attribution(enrichment.attribution, now=now)
        record = await self._session.scalar(
            select(JenkinsBuildRecord).where(
                JenkinsBuildRecord.job_full_name == enrichment.job_full_name,
                JenkinsBuildRecord.build_number == enrichment.number,
            )
        )
        if record is None:
            raise LookupError(f"Jenkins build {enrichment.job_full_name} #{enrichment.number} is not indexed")
        record.logical_run_key = enrichment.logical_run_key
        record.failed_stage = enrichment.failed_stage
        record.failure_classification = enrichment.failure_classification
        record.failure_signature = enrichment.failure_signature
        record.failure_summary = enrichment.failure_summary
        record.propagated_failure = enrichment.propagated_failure
        record.evidence = {
            **record.evidence,
            "error_lines": list(enrichment.error_lines),
            "stages": [dict(item) for item in enrichment.stage_evidence],
            "causes": [dict(item) for item in enrichment.attribution.cause_evidence],
        }
        record.enrichment_status = "enriched" if enrichment.log_enriched else "log_pending"
        record.updated_at = now
        await self._create_edge(record, now=now)
        await self._session.flush()
        return incident_ids

    async def _persist_attribution(
        self,
        attribution: JenkinsBuildAttribution,
        *,
        now: datetime,
    ) -> tuple[str, ...]:
        record = await self._session.scalar(
            select(JenkinsBuildRecord).where(
                JenkinsBuildRecord.job_full_name == attribution.job_full_name,
                JenkinsBuildRecord.build_number == attribution.number,
            )
        )
        if record is None:
            raise LookupError(f"Jenkins build {attribution.job_full_name} #{attribution.number} is not indexed")
        previous_logical_run = record.logical_run_key
        record.upstream_job_full_name = attribution.upstream_job_full_name
        record.upstream_build_number = attribution.upstream_build_number
        record.root_job_full_name = attribution.root_job_full_name
        record.root_build_number = attribution.root_build_number
        record.logical_run_key = attribution.logical_run_key
        record.trigger_kind = attribution.trigger_kind
        record.head_name = attribution.head_name
        logical_keys = {previous_logical_run, attribution.logical_run_key}
        targets = list(
            (
                await self._session.scalars(
                    select(JenkinsBuildRecord).where(
                        or_(
                            JenkinsBuildRecord.id == record.id,
                            JenkinsBuildRecord.logical_run_key.in_(logical_keys),
                        )
                    )
                )
            ).all()
        )
        incident_ids: set[str] = set()
        for target in targets:
            _apply_source(target, attribution.source, now=now)
            if target.incident_id is not None:
                incident_ids.add(str(target.incident_id))
        await self._session.flush()
        return tuple(sorted(incident_ids))

    async def mark_enrichment_failed(self, job_name: str, number: int, *, now: datetime, summary: str) -> None:
        record = await self._session.scalar(
            select(JenkinsBuildRecord).where(
                JenkinsBuildRecord.job_full_name == job_name,
                JenkinsBuildRecord.build_number == number,
            )
        )
        if record is None:
            return
        attempt_count = int(record.evidence.get("enrichment_attempt_count") or 0) + 1
        record.enrichment_status = "pending" if attempt_count < 3 else "failed"
        record.evidence = {
            **record.evidence,
            "enrichment_attempt_count": attempt_count,
            "enrichment_error": summary[:500],
        }
        record.updated_at = now
        await self._session.flush()

    async def refresh_classifications(self, *, now: datetime) -> None:
        since = now - timedelta(days=30)
        records = list(
            (
                await self._session.scalars(
                    select(JenkinsBuildRecord)
                    .where(JenkinsBuildRecord.started_at >= since)
                    .order_by(JenkinsBuildRecord.started_at, JenkinsBuildRecord.id)
                )
            ).all()
        )
        by_job: dict[str, list[JenkinsBuildRecord]] = defaultdict(list)
        signature_counts: Counter[str] = Counter()
        logical_counts: Counter[str] = Counter()
        durations: dict[str, list[int]] = defaultdict(list)
        for record in records:
            by_job[record.job_full_name].append(record)
            if record.result in _FAILURE_RESULTS:
                logical_counts[record.logical_run_key] += 1
                if record.failure_signature and not record.propagated_failure:
                    signature_counts[record.failure_signature] += 1
            elif record.result == "SUCCESS" and record.duration_ms > 0:
                durations[record.job_full_name].append(record.duration_ms)

        seen_signatures: Counter[str] = Counter()
        histories: dict[str, list[str]] = defaultdict(list)
        for record in records:
            if record.result not in _FAILURE_RESULTS:
                histories[record.job_full_name].append(record.result)
                continue
            prior = histories[record.job_full_name][-5:]
            if record.propagated_failure:
                novelty = JenkinsNovelty.PROPAGATED
            elif record.failure_signature and seen_signatures[record.failure_signature] == 0:
                novelty = JenkinsNovelty.NEW_REGRESSION if prior and prior[-1] == "SUCCESS" else JenkinsNovelty.NEW_FAILURE
            elif not record.failure_signature and not any(item in _FAILURE_RESULTS for item in prior):
                novelty = JenkinsNovelty.NEW_REGRESSION if prior and prior[-1] == "SUCCESS" else JenkinsNovelty.NEW_FAILURE
            elif "SUCCESS" in prior and any(item in _FAILURE_RESULTS for item in prior):
                novelty = JenkinsNovelty.FLAKY
            else:
                novelty = JenkinsNovelty.RECURRING
            record.novelty = novelty.value
            reasons: list[str] = []
            score = 0
            later_success = any(
                item.result == "SUCCESS" and item.started_at > record.started_at
                for item in by_job[record.job_full_name]
            )
            record.recovered = later_success
            if not later_success and record.started_at >= now - timedelta(hours=24):
                score += 30
                reasons.append("current blockage +30")
            recurrence = min(20, max(0, signature_counts[record.failure_signature] - 1) * 4)
            if recurrence:
                score += recurrence
                reasons.append(f"recurrence +{recurrence}")
            cost = min(20, max(1, math.ceil(record.duration_ms / 3_600_000 * (1 if record.result != "UNSTABLE" else 0.5))))
            score += cost
            reasons.append(f"failed wall time +{cost}")
            blast = min(15, max(0, logical_counts[record.logical_run_key] - 1) * 3)
            if blast:
                score += blast
                reasons.append(f"execution fanout +{blast}")
            baseline = _percentile(durations.get(record.job_full_name, []), 0.95)
            if baseline and record.duration_ms > baseline * 1.5:
                score += 10
                reasons.append("duration anomaly +10")
            if record.change_number and record.source_provider:
                score += 5
                reasons.append("change request blocked +5")
            record.priority_score = min(100, score)
            record.priority_reasons = reasons
            if record.failure_signature and not record.propagated_failure:
                seen_signatures[record.failure_signature] += 1
            histories[record.job_full_name].append(record.result)
        await self._session.flush()

    async def sync_status(self) -> dict[str, Any]:
        record = await self._session.get(JenkinsSyncStateRecord, 1)
        if record is None:
            return {"status": "never_run"}
        return {
            "status": record.status,
            "started_at": record.started_at,
            "completed_at": record.completed_at,
            "cutoff_at": record.cutoff_at,
            "failure_summary": record.failure_summary,
            "stats": record.stats,
            "updated_at": record.updated_at,
        }

    async def jenkins_summary(self, *, since: datetime) -> dict[str, Any]:
        jobs = list((await self._session.scalars(select(JenkinsJobRecord))).all())
        builds = list(
            (
                await self._session.scalars(
                    select(JenkinsBuildRecord).where(JenkinsBuildRecord.started_at >= since)
                )
            ).all()
        )
        results = Counter(item.result for item in builds)
        failures = [item for item in builds if item.result in _FAILURE_RESULTS]
        return {
            "window_start": since,
            "job_count": len(jobs),
            "active_job_count": sum(bool(item.last_build_at and item.last_build_at >= since) for item in jobs),
            "build_count": len(builds),
            "result_counts": dict(results),
            "failure_build_count": len(failures),
            "enriched_failure_count": sum(item.enrichment_status == "enriched" for item in failures),
            "pending_failure_analysis_count": sum(
                item.enrichment_status in {"pending", "log_pending"} for item in failures
            ),
            "new_failure_count": sum(
                item.novelty in {JenkinsNovelty.NEW_FAILURE.value, JenkinsNovelty.NEW_REGRESSION.value}
                for item in failures
            ),
            "source_kind_counts": dict(Counter(item.source_kind for item in failures)),
            "verified_source_count": sum(item.source_status == "verified" for item in failures),
            "unresolved_source_count": sum(
                item.source_status in {"pending", "conflict", "unavailable", "unresolved"}
                for item in failures
            ),
            "running_build_count": sum(item.building for item in builds),
            "cumulative_wall_hours": round(sum(item.duration_ms for item in builds if not item.building) / 3_600_000, 1),
            "exact_job_count": sum(item.history_coverage in {"exact", "job_started_in_window"} for item in jobs),
            "retention_limited_job_count": sum(item.history_coverage == "retention_limited" for item in jobs),
            "multibranch_parent_count": sum(item.job_type == "multibranch" for item in jobs),
            "sync": await self.sync_status(),
        }

    async def failure_builds(
        self,
        *,
        since: datetime,
        limit: int,
        cursor: str | None = None,
        novelty: frozenset[str] | None = None,
        job: str | None = None,
        result: str | None = None,
    ) -> CursorPage:
        conditions = [
            JenkinsBuildRecord.started_at >= since,
            JenkinsBuildRecord.result.in_(_FAILURE_RESULTS),
        ]
        if novelty:
            conditions.append(JenkinsBuildRecord.novelty.in_(novelty))
        if job:
            conditions.append(JenkinsBuildRecord.job_full_name.ilike(f"%{job}%"))
        if result:
            conditions.append(JenkinsBuildRecord.result == result)
        total_count = int(
            await self._session.scalar(
                select(func.count(JenkinsBuildRecord.id)).where(*conditions)
            )
            or 0
        )
        statement = (
            select(JenkinsBuildRecord, JenkinsJobRecord)
            .join(JenkinsJobRecord, JenkinsJobRecord.full_name == JenkinsBuildRecord.job_full_name)
            .where(*conditions)
        )
        if cursor:
            started_at, item_id = decode_cursor(cursor)
            build_id = uuid.UUID(item_id)
            statement = statement.where(
                or_(
                    JenkinsBuildRecord.started_at < started_at,
                    and_(
                        JenkinsBuildRecord.started_at == started_at,
                        JenkinsBuildRecord.id < build_id,
                    ),
                )
            )
        rows = (
            await self._session.execute(
                statement.order_by(
                    JenkinsBuildRecord.started_at.desc(),
                    JenkinsBuildRecord.id.desc(),
                ).limit(limit + 1)
            )
        ).all()
        page = rows[:limit]
        next_cursor = None
        if len(rows) > limit:
            last = page[-1][0]
            next_cursor = encode_cursor(last.started_at, str(last.id))
        return CursorPage(
            tuple(_build_dict(build, job_record) for build, job_record in page),
            next_cursor,
            total_count,
        )

    async def logical_executions(self, *, since: datetime, limit: int) -> tuple[dict[str, Any], ...]:
        records = list(
            (
                await self._session.scalars(
                    select(JenkinsBuildRecord)
                    .where(
                        JenkinsBuildRecord.started_at >= since,
                        JenkinsBuildRecord.result.in_(_FAILURE_RESULTS),
                    )
                    .order_by(JenkinsBuildRecord.started_at.desc())
                    .limit(10_000)
                )
            ).all()
        )
        grouped: dict[str, list[JenkinsBuildRecord]] = defaultdict(list)
        for record in records:
            grouped[record.logical_run_key].append(record)
        items = []
        for logical_key, group in grouped.items():
            primary = max(
                group,
                key=lambda item: (
                    not item.propagated_failure,
                    item.priority_score,
                    item.started_at,
                ),
            )
            items.append(
                {
                    "logical_run_key": logical_key,
                    "title": primary.failure_summary or f"{primary.job_full_name} #{primary.build_number} failed",
                    "classification": primary.failure_classification,
                    "priority_score": max(item.priority_score for item in group),
                    "priority_reasons": primary.priority_reasons,
                    "first_seen_at": min(item.started_at for item in group),
                    "last_seen_at": max(item.started_at for item in group),
                    "root_job": primary.root_job_full_name or primary.job_full_name,
                    "root_build_number": primary.root_build_number or primary.build_number,
                    "source_provider": primary.source_provider,
                    "repository": primary.repository,
                    "change_number": primary.change_number,
                    "change_url": primary.change_url,
                    "source_kind": primary.source_kind,
                    "source_status": primary.source_status,
                    "source_profile_id": primary.source_profile_id,
                    "source_profile_registered": primary.source_profile_id is not None,
                    "source_branch": primary.source_branch,
                    "source_commit_sha": primary.source_commit_sha,
                    "source_url": primary.source_url,
                    "source_title": primary.source_title,
                    "source_state": primary.source_state,
                    "source_resolution_method": primary.source_resolution_method,
                    "source_reason": primary.source_reason,
                    "source_verified_at": primary.source_verified_at,
                    "affected_build_count": len(group),
                    "propagated_build_count": sum(item.propagated_failure for item in group),
                    "builds": [_build_brief(item) for item in sorted(group, key=lambda value: value.started_at)],
                    "primary_build_id": str(primary.id),
                }
            )
        items.sort(key=lambda item: (item["priority_score"], item["last_seen_at"]), reverse=True)
        return tuple(items[:limit])

    async def recurring_patterns(self, *, since: datetime, limit: int) -> tuple[dict[str, Any], ...]:
        records = list(
            (
                await self._session.scalars(
                    select(JenkinsBuildRecord).where(
                        JenkinsBuildRecord.started_at >= since,
                        JenkinsBuildRecord.result.in_(_FAILURE_RESULTS),
                        JenkinsBuildRecord.failure_signature != "",
                        JenkinsBuildRecord.enrichment_status == "enriched",
                        JenkinsBuildRecord.propagated_failure.is_(False),
                    )
                )
            ).all()
        )
        grouped: dict[str, list[JenkinsBuildRecord]] = defaultdict(list)
        for record in records:
            grouped[record.failure_signature].append(record)
        items = []
        for signature, group in grouped.items():
            if len(group) < 2:
                continue
            latest = max(group, key=lambda item: item.started_at)
            items.append(
                {
                    "signature": signature,
                    "title": latest.failure_summary or latest.failure_classification.replace("_", " "),
                    "classification": latest.failure_classification,
                    "occurrence_count": len(group),
                    "affected_jobs": sorted({item.job_full_name for item in group}),
                    "first_seen_at": min(item.started_at for item in group),
                    "last_seen_at": latest.started_at,
                    "failed_wall_hours": round(sum(item.duration_ms for item in group) / 3_600_000, 1),
                    "priority_score": max(item.priority_score for item in group),
                    "latest_build_id": str(latest.id),
                }
            )
        items.sort(
            key=lambda item: (item["priority_score"], item["occurrence_count"], item["last_seen_at"]),
            reverse=True,
        )
        return tuple(items[:limit])

    async def job_families(self, *, since: datetime, limit: int) -> tuple[dict[str, Any], ...]:
        rows = (
            await self._session.execute(
                select(JenkinsBuildRecord, JenkinsJobRecord)
                .join(JenkinsJobRecord, JenkinsJobRecord.full_name == JenkinsBuildRecord.job_full_name)
                .where(JenkinsBuildRecord.started_at >= since)
            )
        ).all()
        grouped: dict[str, tuple[JenkinsJobRecord, list[JenkinsBuildRecord]]] = {}
        for build, job in rows:
            grouped.setdefault(job.full_name, (job, []))[1].append(build)
        items = []
        for job_name, (job, builds) in grouped.items():
            counts = Counter(item.result for item in builds)
            completed = [item.duration_ms for item in builds if not item.building and item.duration_ms > 0]
            latest = max(builds, key=lambda item: item.started_at)
            items.append(
                {
                    "job_name": job_name,
                    "job_type": job.job_type,
                    "parent": job.parent_full_name,
                    "head_type": job.head_type,
                    "head_name": job.head_name,
                    "source_provider": latest.source_provider or job.source_provider,
                    "repository": latest.repository or job.repository,
                    "source_kind": latest.source_kind,
                    "source_status": latest.source_status,
                    "source_profile_id": latest.source_profile_id,
                    "source_profile_registered": latest.source_profile_id is not None,
                    "change_number": latest.change_number,
                    "source_branch": latest.source_branch,
                    "source_commit_sha": latest.source_commit_sha,
                    "source_url": latest.source_url,
                    "source_reason": latest.source_reason,
                    "url": job.url,
                    "coverage": job.history_coverage,
                    "run_count": len(builds),
                    "result_counts": dict(counts),
                    "failure_rate": round(
                        sum(counts[item] for item in _FAILURE_RESULTS) / len(builds) * 100,
                        1,
                    ),
                    "wall_hours": round(sum(completed) / 3_600_000, 1),
                    "median_duration_minutes": round(_percentile(completed, 0.5) / 60_000, 1),
                    "p95_duration_minutes": round(_percentile(completed, 0.95) / 60_000, 1),
                    "latest_result": latest.result,
                    "last_build_at": latest.started_at,
                }
            )
        items.sort(key=lambda item: (item["run_count"], item["wall_hours"]), reverse=True)
        return tuple(items[:limit])

    async def multibranch_families(self, *, since: datetime, limit: int) -> tuple[dict[str, Any], ...]:
        parents = list(
            (
                await self._session.scalars(
                    select(JenkinsJobRecord).where(JenkinsJobRecord.job_type == "multibranch")
                )
            ).all()
        )
        children = list(
            (
                await self._session.scalars(
                    select(JenkinsJobRecord).where(
                        JenkinsJobRecord.parent_full_name.in_([item.full_name for item in parents])
                    )
                )
            ).all()
        ) if parents else []
        child_names = [item.full_name for item in children]
        builds = list(
            (
                await self._session.scalars(
                    select(JenkinsBuildRecord).where(
                        JenkinsBuildRecord.job_full_name.in_(child_names),
                        JenkinsBuildRecord.started_at >= since,
                    )
                )
            ).all()
        ) if child_names else []
        builds_by_job: dict[str, list[JenkinsBuildRecord]] = defaultdict(list)
        for build in builds:
            builds_by_job[build.job_full_name].append(build)
        children_by_parent: dict[str, list[JenkinsJobRecord]] = defaultdict(list)
        for child in children:
            if child.parent_full_name:
                children_by_parent[child.parent_full_name].append(child)
        items = []
        for parent in parents:
            child_rows = []
            total_counts: Counter[str] = Counter()
            for child in children_by_parent[parent.full_name]:
                child_builds = builds_by_job[child.full_name]
                counts = Counter(item.result for item in child_builds)
                total_counts.update(counts)
                if child_builds:
                    child_rows.append(
                        {
                            "job_name": child.full_name,
                            "head_type": child.head_type,
                            "head_name": child.head_name or child.display_name,
                            "source_provider": child.source_provider,
                            "repository": child.repository,
                            "source_url": child.source_url,
                            "run_count": len(child_builds),
                            "result_counts": dict(counts),
                            "last_build_at": max(item.started_at for item in child_builds),
                        }
                    )
            child_rows.sort(key=lambda item: (item["run_count"], item["last_build_at"]), reverse=True)
            items.append(
                {
                    "parent": parent.full_name,
                    "url": parent.url,
                    "child_count": len(children_by_parent[parent.full_name]),
                    "active_child_count": len(child_rows),
                    "run_count": sum(total_counts.values()),
                    "result_counts": dict(total_counts),
                    "head_counts": dict(Counter(item["head_type"] for item in child_rows)),
                    "children": child_rows,
                }
            )
        items.sort(key=lambda item: (item["run_count"], item["active_child_count"]), reverse=True)
        return tuple(items[:limit])

    async def build_detail(self, build_id: str) -> dict[str, Any] | None:
        try:
            item_id = uuid.UUID(build_id)
        except ValueError:
            return None
        row = (
            await self._session.execute(
                select(JenkinsBuildRecord, JenkinsJobRecord)
                .join(JenkinsJobRecord, JenkinsJobRecord.full_name == JenkinsBuildRecord.job_full_name)
                .where(JenkinsBuildRecord.id == item_id)
            )
        ).one_or_none()
        if row is None:
            return None
        build, job = row
        upstream = (
            await self._session.scalars(
                select(JenkinsBuildRecord)
                .join(JenkinsBuildEdgeRecord, JenkinsBuildEdgeRecord.upstream_build_id == JenkinsBuildRecord.id)
                .where(JenkinsBuildEdgeRecord.downstream_build_id == build.id)
            )
        ).all()
        downstream = (
            await self._session.scalars(
                select(JenkinsBuildRecord)
                .join(JenkinsBuildEdgeRecord, JenkinsBuildEdgeRecord.downstream_build_id == JenkinsBuildRecord.id)
                .where(JenkinsBuildEdgeRecord.upstream_build_id == build.id)
            )
        ).all()
        return {
            **_build_dict(build, job),
            "evidence": build.evidence,
            "upstream_builds": [_build_brief(item) for item in upstream],
            "downstream_builds": [_build_brief(item) for item in downstream],
        }

    async def build_by_job_number(self, job_name: str, build_number: int) -> dict[str, Any] | None:
        row = (await self._session.execute(
            select(JenkinsBuildRecord, JenkinsJobRecord)
            .join(JenkinsJobRecord, JenkinsJobRecord.full_name == JenkinsBuildRecord.job_full_name)
            .where(JenkinsBuildRecord.job_full_name == job_name, JenkinsBuildRecord.build_number == build_number)
        )).one_or_none()
        if row is None:
            return None
        return _build_dict(*row)

    async def builds_for_incident(self, incident_id: str) -> tuple[dict[str, Any], ...]:
        try:
            item_id = uuid.UUID(incident_id)
        except ValueError:
            return ()
        rows = (
            await self._session.execute(
                select(JenkinsBuildRecord, JenkinsJobRecord)
                .join(JenkinsJobRecord, JenkinsJobRecord.full_name == JenkinsBuildRecord.job_full_name)
                .where(JenkinsBuildRecord.incident_id == item_id)
                .order_by(JenkinsBuildRecord.started_at.desc(), JenkinsBuildRecord.id.desc())
            )
        ).all()
        return tuple(_build_dict(build, job) | {"evidence": build.evidence} for build, job in rows)

    async def link_incident(self, build_id: str, incident_id: str) -> None:
        build = await self._session.get(JenkinsBuildRecord, uuid.UUID(build_id))
        if build is None:
            raise LookupError(f"Jenkins build {build_id} does not exist")
        if await self._session.get(IncidentRecord, uuid.UUID(incident_id)) is None:
            raise LookupError(f"incident {incident_id} does not exist")
        if build.incident_id is not None and build.incident_id != uuid.UUID(incident_id):
            raise ValueError(f"Jenkins build {build_id} is already linked to another incident")
        build.incident_id = uuid.UUID(incident_id)
        await self._session.flush()

    async def analysis_candidates(self, *, min_priority: int, limit: int) -> tuple[dict[str, Any], ...]:
        rows = (
            await self._session.execute(
                select(JenkinsBuildRecord, JenkinsJobRecord)
                .join(JenkinsJobRecord, JenkinsJobRecord.full_name == JenkinsBuildRecord.job_full_name)
                .where(
                    JenkinsBuildRecord.incident_id.is_(None),
                    JenkinsBuildRecord.building.is_(False),
                    JenkinsBuildRecord.result.in_(_FAILURE_RESULTS),
                    JenkinsBuildRecord.enrichment_status.in_({"enriched", "log_pending", "failed"}),
                    JenkinsBuildRecord.priority_score >= min_priority,
                )
                .order_by(
                    JenkinsBuildRecord.propagated_failure.asc(),
                    JenkinsBuildRecord.recovered.asc(),
                    JenkinsBuildRecord.priority_score.desc(),
                    JenkinsBuildRecord.started_at,
                )
                .limit(limit)
            )
        ).all()
        return tuple(_build_dict(build, job) | {"evidence": build.evidence} for build, job in rows)

    async def _create_edge(self, record: JenkinsBuildRecord, *, now: datetime) -> None:
        if not record.upstream_job_full_name or record.upstream_build_number is None:
            return
        upstream = await self._session.scalar(
            select(JenkinsBuildRecord).where(
                JenkinsBuildRecord.job_full_name == record.upstream_job_full_name,
                JenkinsBuildRecord.build_number == record.upstream_build_number,
            )
        )
        if upstream is None:
            return
        existing = await self._session.scalar(
            select(JenkinsBuildEdgeRecord).where(
                JenkinsBuildEdgeRecord.upstream_build_id == upstream.id,
                JenkinsBuildEdgeRecord.downstream_build_id == record.id,
            )
        )
        if existing is None:
            self._session.add(
                JenkinsBuildEdgeRecord(
                    id=uuid.uuid4(),
                    upstream_build_id=upstream.id,
                    downstream_build_id=record.id,
                    relationship_type="triggered",
                    created_at=now,
                )
            )


def _snapshot(record: JenkinsBuildRecord) -> JenkinsBuildSnapshot:
    return JenkinsBuildSnapshot(
        job_full_name=record.job_full_name,
        number=record.build_number,
        result=record.result,
        url=record.url,
        started_at=record.started_at,
        duration_ms=record.duration_ms,
        building=record.building,
        enrichment_status=record.enrichment_status,
        source_status=record.source_status,
        logical_run_key=record.logical_run_key,
    )


def _apply_source(record: JenkinsBuildRecord, source: SourceAttribution, *, now: datetime) -> None:
    record.source_kind = source.kind.value
    record.source_status = source.status.value
    record.source_provider = source.provider
    record.repository = source.repository
    record.change_number = source.change_number if source.kind is SourceKind.CHANGE_REQUEST else None
    record.change_url = source.url if source.kind is SourceKind.CHANGE_REQUEST else None
    record.source_profile_id = source.profile_id
    record.source_branch = source.branch
    record.source_commit_sha = source.commit_sha
    record.source_url = source.url
    record.source_title = source.title
    record.source_state = source.state
    record.source_resolution_method = source.resolution_method
    record.source_reason = source.reason
    record.source_allow_mr_comments = source.allow_mr_comments
    record.source_provenance = [dict(item) for item in source.provenance]
    record.source_verified_at = source.verified_at
    record.source_attributed_at = now
    record.evidence = {**record.evidence, "source_attribution": source.evidence()}
    record.updated_at = now


def _build_brief(record: JenkinsBuildRecord) -> dict[str, Any]:
    return {
        "id": str(record.id),
        "job_name": record.job_full_name,
        "build_number": record.build_number,
        "result": record.result,
        "url": record.url,
        "started_at": record.started_at,
        "duration_ms": record.duration_ms,
        "propagated_failure": record.propagated_failure,
        "recovered": record.recovered,
        "failed_stage": record.failed_stage,
        "failure_summary": record.failure_summary,
        "incident_id": str(record.incident_id) if record.incident_id else None,
    }


def _build_dict(record: JenkinsBuildRecord, job: JenkinsJobRecord) -> dict[str, Any]:
    return {
        **_build_brief(record),
        "building": record.building,
        "completed_at": record.completed_at,
        "job_type": job.job_type,
        "parent": job.parent_full_name,
        "head_type": job.head_type,
        "head_name": record.head_name or job.head_name,
        "source_provider": record.source_provider or job.source_provider,
        "repository": record.repository or job.repository,
        "change_number": record.change_number,
        "change_url": record.change_url,
        "source_kind": record.source_kind,
        "source_status": record.source_status,
        "source_profile_id": record.source_profile_id,
        "source_profile_registered": record.source_profile_id is not None,
        "source_branch": record.source_branch,
        "source_commit_sha": record.source_commit_sha,
        "source_url": record.source_url,
        "source_title": record.source_title,
        "source_state": record.source_state,
        "source_resolution_method": record.source_resolution_method,
        "source_reason": record.source_reason,
        "source_allow_mr_comments": record.source_allow_mr_comments,
        "source_verified_at": record.source_verified_at,
        "trigger_kind": record.trigger_kind,
        "root_job": record.root_job_full_name or record.job_full_name,
        "root_build_number": record.root_build_number or record.build_number,
        "logical_run_key": record.logical_run_key,
        "failure_classification": record.failure_classification,
        "failure_signature": record.failure_signature,
        "novelty": record.novelty,
        "priority_score": record.priority_score,
        "priority_reasons": record.priority_reasons,
        "coverage": job.history_coverage,
        "enrichment_status": record.enrichment_status,
    }


def _percentile(values: list[int], quantile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * quantile) - 1))
    return ordered[index]
