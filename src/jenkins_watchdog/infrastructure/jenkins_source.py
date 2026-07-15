"""Read-only Jenkins adapter for durable catalog and build ingestion."""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote, urlparse

import httpx

from jenkins_watchdog.clients.jenkins import JenkinsClient, job_to_api_path
from jenkins_watchdog.clients.log_analysis import classify_failure, error_signature, extract_error_lines
from jenkins_watchdog.domain.jenkins import (
    JenkinsBuildAttribution,
    JenkinsBuildEnrichment,
    JenkinsBuildHistoryPage,
    JenkinsBuildSnapshot,
    JenkinsCoverage,
    JenkinsHeadType,
    JenkinsJobSnapshot,
)
from jenkins_watchdog.domain.source import SourceProfileRegistry
from jenkins_watchdog.infrastructure.source_attribution import JenkinsSourceAttributor

_PAGE_SIZE = 100
_FAILURE_RESULTS = frozenset({"FAILURE", "UNSTABLE", "ABORTED"})
_PROPAGATED = re.compile(
    r"(?:Build\s+)?(?P<job>[^#\n]+?)\s+#(?P<number>\d+)\s+(?:completed|finished).*?(?:FAILURE|FAILED)",
    re.IGNORECASE,
)
_LOG_TIMESTAMP = re.compile(r"^(?:\[\d{4}-\d{2}-\d{2}T[^\]]+\]\s*)+")
_STACK_FRAME = re.compile(r"^at\s+[\w.$]+\([^)]*\)$")


class JenkinsSourceAdapter:
    def __init__(
        self,
        client: JenkinsClient,
        *,
        hierarchy_depth: int = 8,
        attributor: JenkinsSourceAttributor | None = None,
    ) -> None:
        self._client = client
        self._hierarchy_depth = hierarchy_depth
        self._attributor = attributor or JenkinsSourceAttributor(SourceProfileRegistry(1, ()))
        self._job_sources: dict[str, JenkinsJobSnapshot] = {}
        self._job_source_tasks: dict[str, asyncio.Task[JenkinsJobSnapshot]] = {}
        self._build_detail_tasks: dict[tuple[str, int], asyncio.Task[dict[str, Any]]] = {}

    async def discover_jobs(self) -> tuple[JenkinsJobSnapshot, ...]:
        fields = "name,fullName,url,color,_class,firstBuild[number,timestamp],lastBuild[number,timestamp]"
        nested = fields
        for _ in range(self._hierarchy_depth):
            nested = f"{fields},jobs[{nested}]"
        payload = await self._client.get_json("/api/json", params={"tree": f"jobs[{nested}]"})
        jobs: list[JenkinsJobSnapshot] = []

        def visit(items: list[dict[str, Any]], parent: str | None = None) -> None:
            for item in items:
                name = str(item.get("name") or "").strip()
                full_name = str(item.get("fullName") or (f"{parent}/{name}" if parent else name)).strip("/")
                if not full_name:
                    continue
                first = item.get("firstBuild") or {}
                last = item.get("lastBuild") or {}
                jobs.append(
                    JenkinsJobSnapshot(
                        full_name=full_name,
                        display_name=name or full_name.rsplit("/", 1)[-1],
                        url=str(item.get("url") or ""),
                        job_class=str(item.get("_class") or ""),
                        color=str(item["color"]) if item.get("color") is not None else None,
                        parent_full_name=parent,
                        first_build_number=_integer(first.get("number")),
                        first_build_at=_timestamp(first.get("timestamp")),
                        last_build_number=_integer(last.get("number")),
                        last_build_at=_timestamp(last.get("timestamp")),
                    )
                )
                visit(item.get("jobs") or [], full_name)

        visit(payload.get("jobs") or [])
        return tuple(jobs)

    async def enrich_job_source(self, job: JenkinsJobSnapshot) -> JenkinsJobSnapshot:
        cached = self._job_sources.get(job.full_name)
        if cached is not None:
            return replace(
                job,
                head_type=cached.head_type,
                head_name=cached.head_name,
                source_provider=cached.source_provider,
                repository=cached.repository,
                source_url=cached.source_url,
            )
        task = self._job_source_tasks.get(job.full_name)
        if task is None:
            task = asyncio.create_task(self._load_job_source(job))
            self._job_source_tasks[job.full_name] = task
        try:
            return await task
        except Exception:
            self._job_source_tasks.pop(job.full_name, None)
            raise

    async def _load_job_source(self, job: JenkinsJobSnapshot) -> JenkinsJobSnapshot:
        try:
            payload = await self._client.get_json(
                f"{job_to_api_path(job.full_name)}/api/json",
                params={
                    "tree": (
                        "property[_class,branch[head[_class,name]]],"
                        "actions[_class,objectUrl]"
                    )
                },
            )
        except Exception:
            self._job_sources[job.full_name] = job
            return job
        branch_property = next(
            (
                item
                for item in payload.get("property") or []
                if "BranchJobProperty" in str(item.get("_class") or "")
            ),
            {},
        )
        branch = branch_property.get("branch") or {}
        head = branch.get("head") or {}
        head_class = str(head.get("_class") or "")
        head_type = _head_type(head_class)
        object_action = next(
            (
                item
                for item in payload.get("actions") or []
                if "ObjectMetadataAction" in str(item.get("_class") or "")
            ),
            {},
        )
        source_url = str(object_action.get("objectUrl") or "") or None
        provider, repository, _ = _source_from_url(source_url)
        enriched = replace(
            job,
            head_type=head_type,
            head_name=str(head.get("name") or job.display_name) if head else None,
            source_provider=provider,
            repository=repository,
            source_url=source_url,
        )
        self._job_sources[job.full_name] = enriched
        return enriched

    async def build_history(
        self,
        job: JenkinsJobSnapshot,
        *,
        cutoff: datetime,
        after_number: int | None,
    ) -> JenkinsBuildHistoryPage:
        fields = "number,result,building,timestamp,duration,url"
        builds: list[JenkinsBuildSnapshot] = []
        start = 0
        reached_boundary = False
        exhausted = False
        cutoff_ms = int(cutoff.timestamp() * 1000)
        while start < 10_000:
            payload = await self._client.get_json(
                f"{job_to_api_path(job.full_name)}/api/json",
                params={"tree": f"allBuilds[{fields}]{{{start},{start + _PAGE_SIZE}}}"},
            )
            chunk = payload.get("allBuilds") or []
            if len(chunk) < _PAGE_SIZE:
                exhausted = True
            for item in chunk:
                number = _integer(item.get("number"))
                timestamp_ms = _integer(item.get("timestamp"))
                if number is None or timestamp_ms is None:
                    continue
                if after_number is not None and number <= after_number:
                    reached_boundary = True
                    continue
                if after_number is None and timestamp_ms < cutoff_ms:
                    reached_boundary = True
                    continue
                result = str(item.get("result") or "RUNNING")
                builds.append(
                    JenkinsBuildSnapshot(
                        job_full_name=job.full_name,
                        number=number,
                        result=result,
                        url=str(item.get("url") or f"{job.url}{number}/"),
                        started_at=datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc),
                        duration_ms=max(0, _integer(item.get("duration")) or 0),
                        building=bool(item.get("building") or item.get("result") is None),
                    )
                )
            if reached_boundary or exhausted or not chunk:
                break
            start += _PAGE_SIZE

        if after_number is not None:
            coverage = JenkinsCoverage.NOT_APPLICABLE
        elif reached_boundary:
            coverage = JenkinsCoverage.EXACT
        elif exhausted and job.first_build_number == 1:
            coverage = JenkinsCoverage.JOB_STARTED_IN_WINDOW
        elif exhausted:
            coverage = JenkinsCoverage.RETENTION_LIMITED
        else:
            coverage = JenkinsCoverage.UNKNOWN
        builds.sort(key=lambda item: item.number)
        return JenkinsBuildHistoryPage(tuple(builds), coverage)

    async def attribute_build(self, build: JenkinsBuildSnapshot) -> JenkinsBuildAttribution:
        trace = await self._trace_build(build)
        return await self._attribution_from_trace(build, trace)

    async def enrich_build(self, build: JenkinsBuildSnapshot, *, include_log: bool) -> JenkinsBuildEnrichment:
        trace = await self._trace_build(build)
        attribution = await self._attribution_from_trace(build, trace)

        stages = await self._workflow_stages(build.job_full_name, build.number)
        failed_stage = next(
            (str(stage.get("name")) for stage in stages if stage.get("status") in {"FAILED", "UNSTABLE"}),
            None,
        )
        error_lines: list[str] = []
        log_enriched = False
        if include_log and build.result in _FAILURE_RESULTS:
            try:
                console = await self._client.get_build_console_tail(build.job_full_name, build.number)
                error_lines = extract_error_lines(console, max_lines=20, tail_chars=160_000)
                log_enriched = True
            except httpx.HTTPStatusError as exc:
                log_enriched = exc.response.status_code == 404
                error_lines = []
            except Exception:
                error_lines = []

        cause_classes = {_cause_class(item) for item in trace.all_causes}
        propagated = any("DownstreamFailureCause" in item for item in cause_classes)
        propagated = propagated or any(_PROPAGATED.search(line) for line in error_lines)
        classification = classify_failure(error_lines)
        if build.result == "ABORTED" and classification == "unknown":
            classification = "cancelled"
        if propagated:
            classification = "propagated"
        elif classification == "unknown" and failed_stage:
            lowered = failed_stage.lower()
            if "test" in lowered or "automation" in lowered or "regression" in lowered:
                classification = "test_failure"

        summary = _failure_summary(error_lines, failed_stage, build.result, propagated)
        if not trace.details_available and not error_lines:
            summary = "Build details are no longer retained by Jenkins"
        generic_summary = summary in {
            f"Build finished with {build.result}",
            f"{failed_stage} finished with {build.result}",
        }
        signature = error_signature([summary]) if error_lines and trace.details_available and not generic_summary else ""
        if not signature:
            signature_input = f"{classification}|{failed_stage or '-'}|{build.result}|{build.job_full_name}"
            signature = hashlib.sha256(signature_input.encode()).hexdigest()[:12]
        return JenkinsBuildEnrichment(
            job_full_name=build.job_full_name,
            number=build.number,
            attribution=attribution,
            failed_stage=failed_stage,
            failure_classification=classification,
            failure_signature=signature,
            failure_summary=summary,
            propagated_failure=propagated,
            error_lines=tuple(error_lines[-12:]),
            stage_evidence=tuple(_stage_evidence(stage) for stage in stages),
            log_enriched=log_enriched,
        )

    async def _trace_build(self, build: JenkinsBuildSnapshot) -> "_BuildTrace":
        current_job = build.job_full_name
        current_number = build.number
        direct_upstream_job: str | None = None
        direct_upstream_number: int | None = None
        root_job = current_job
        root_number = current_number
        all_causes: list[dict[str, Any]] = []
        root_causes: list[dict[str, Any]] = []
        root_payload: dict[str, Any] = {}
        seen: set[tuple[str, int]] = set()
        details_available = True
        for depth in range(10):
            if (current_job, current_number) in seen:
                break
            seen.add((current_job, current_number))
            try:
                payload = await self._build_detail(current_job, current_number)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404 and depth == 0:
                    details_available = False
                    payload = {}
                elif depth > 0:
                    root_job, root_number = current_job, current_number
                    details_available = False
                    break
                else:
                    raise
            except Exception:
                if depth == 0:
                    raise
                root_job, root_number = current_job, current_number
                details_available = False
                break
            causes = _causes(payload)
            all_causes.extend(causes)
            upstream = next(
                (
                    cause
                    for cause in causes
                    if cause.get("upstreamProject") and _integer(cause.get("upstreamBuild")) is not None
                ),
                None,
            )
            if upstream is None:
                root_job, root_number, root_causes, root_payload = current_job, current_number, causes, payload
                break
            upstream_job = str(upstream["upstreamProject"])
            upstream_number = int(upstream["upstreamBuild"])
            if depth == 0:
                direct_upstream_job = upstream_job
                direct_upstream_number = upstream_number
            current_job, current_number = upstream_job, upstream_number

        return _BuildTrace(
            direct_upstream_job=direct_upstream_job,
            direct_upstream_number=direct_upstream_number,
            root_job=root_job,
            root_number=root_number,
            root_payload=root_payload,
            root_causes=tuple(root_causes),
            all_causes=tuple(all_causes),
            details_available=details_available,
        )

    async def _attribution_from_trace(
        self,
        build: JenkinsBuildSnapshot,
        trace: "_BuildTrace",
    ) -> JenkinsBuildAttribution:

        source_job = JenkinsJobSnapshot(
            full_name=build.job_full_name,
            display_name=build.job_full_name.rsplit("/", 1)[-1],
            url=build.url.rsplit(f"/{build.number}", 1)[0] + "/",
            job_class="",
            color=None,
            parent_full_name=build.job_full_name.rsplit("/", 1)[0] if "/" in build.job_full_name else None,
        )
        source_job = await self.enrich_job_source(source_job)
        trigger_kind = _trigger_kind(list(trace.root_causes))
        source = await self._attributor.resolve(
            root_job=trace.root_job,
            root_build_number=trace.root_number,
            trigger_kind=trigger_kind,
            root_payload=trace.root_payload,
            job_source=source_job,
            root_url=_root_build_url(build.url, trace.root_job, trace.root_number),
            details_available=trace.details_available,
        )
        return JenkinsBuildAttribution(
            job_full_name=build.job_full_name,
            number=build.number,
            upstream_job_full_name=trace.direct_upstream_job,
            upstream_build_number=trace.direct_upstream_number,
            root_job_full_name=trace.root_job,
            root_build_number=trace.root_number,
            trigger_kind=trigger_kind,
            source=source,
            head_name=source.branch or source_job.head_name,
            cause_evidence=tuple(_cause_evidence(cause) for cause in trace.all_causes),
        )

    async def _workflow_stages(self, job_name: str, number: int) -> list[dict[str, Any]]:
        try:
            payload = await self._client.get_json(f"{job_to_api_path(job_name)}/{number}/wfapi/describe")
            return [item for item in payload.get("stages") or [] if isinstance(item, dict)]
        except Exception:
            return []

    async def _build_detail(self, job_name: str, number: int) -> dict[str, Any]:
        key = (job_name, number)
        task = self._build_detail_tasks.get(key)
        if task is None:
            task = asyncio.create_task(
                self._client.get_json(
                    f"{job_to_api_path(job_name)}/{number}/api/json",
                    params={
                        "tree": (
                            "actions[_class,causes[*],remoteUrls[*],"
                            "lastBuiltRevision[SHA1,branch[name,SHA1]],parameters[name,value]]"
                        )
                    },
                )
            )
            self._build_detail_tasks[key] = task
            if len(self._build_detail_tasks) > 5_000:
                oldest = next(iter(self._build_detail_tasks))
                if self._build_detail_tasks[oldest].done():
                    self._build_detail_tasks.pop(oldest, None)
        try:
            return await task
        except Exception:
            self._build_detail_tasks.pop(key, None)
            raise


@dataclass(frozen=True, slots=True)
class _BuildTrace:
    direct_upstream_job: str | None
    direct_upstream_number: int | None
    root_job: str
    root_number: int
    root_payload: Mapping[str, Any]
    root_causes: tuple[dict[str, Any], ...]
    all_causes: tuple[dict[str, Any], ...]
    details_available: bool


def _head_type(class_name: str) -> JenkinsHeadType:
    if "PullRequestSCMHead" in class_name or "MergeRequestSCMHead" in class_name:
        return JenkinsHeadType.CHANGE_REQUEST
    if "TagSCMHead" in class_name:
        return JenkinsHeadType.TAG
    if "BranchSCMHead" in class_name:
        return JenkinsHeadType.BRANCH
    return JenkinsHeadType.UNKNOWN


def _source_from_url(raw: str | None) -> tuple[str | None, str | None, str | None]:
    if not raw:
        return None, None, None
    parsed = urlparse(raw)
    host = parsed.netloc.lower()
    provider = "github" if "github" in host else "gitlab" if "gitlab" in host else None
    parts = [unquote(item) for item in parsed.path.split("/") if item]
    repository = "/".join(parts[:2]) if len(parts) >= 2 else None
    return provider, repository, _change_number_from_url(raw)


def _change_number_from_url(raw: str | None) -> str | None:
    if not raw:
        return None
    match = re.search(r"/(?:pull|merge_requests)/(\d+)(?:/|$)", raw)
    return match.group(1) if match else None


def _root_build_url(build_url: str, root_job: str, root_number: int) -> str:
    parsed = urlparse(build_url)
    if not parsed.scheme or not parsed.netloc:
        return build_url
    return f"{parsed.scheme}://{parsed.netloc}{job_to_api_path(root_job)}/{root_number}/"


def _timestamp(value: Any) -> datetime | None:
    number = _integer(value)
    return datetime.fromtimestamp(number / 1000, tz=timezone.utc) if number else None


def _integer(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _causes(payload: dict[str, Any]) -> list[dict[str, Any]]:
    causes: list[dict[str, Any]] = []
    for action in payload.get("actions") or []:
        if not isinstance(action, dict):
            continue
        causes.extend(item for item in action.get("causes") or [] if isinstance(item, dict))
    return causes


def _cause_class(cause: dict[str, Any]) -> str:
    return str(cause.get("_class") or "").rsplit(".", 1)[-1]


def _cause_evidence(cause: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": _cause_class(cause),
        "upstream_job": cause.get("upstreamProject"),
        "upstream_build": _integer(cause.get("upstreamBuild")),
    }


def _stage_evidence(stage: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": str(stage.get("name") or "Unknown stage"),
        "status": str(stage.get("status") or "UNKNOWN"),
        "duration_ms": _integer(stage.get("durationMillis")) or 0,
    }


def _trigger_kind(causes: list[dict[str, Any]]) -> str:
    classes = {_cause_class(item) for item in causes}
    if any("GitLabWebHookCause" in item for item in classes):
        return "gitlab_webhook"
    if any("GitHub" in item for item in classes):
        return "github_webhook"
    if any("TimerTrigger" in item for item in classes):
        return "scheduled"
    if any("SCMTrigger" in item for item in classes):
        return "scm_poll"
    if any("UserIdCause" in item for item in classes):
        return "manual"
    if any("UpstreamCause" in item or "BuildUpstreamCause" in item for item in classes):
        return "upstream"
    return "unknown"


def _first_scalar(value: Any, keys: tuple[str, ...]) -> str | None:
    wanted = {key.lower() for key in keys}
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in wanted and isinstance(item, (str, int)) and str(item):
                return str(item)
        for item in value.values():
            found = _first_scalar(item, keys)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _first_scalar(item, keys)
            if found is not None:
                return found
    return None


def _failure_summary(lines: list[str], stage: str | None, result: str, propagated: bool) -> str:
    if propagated:
        match = next(
            (
                match
                for line in reversed(lines)
                if (match := _PROPAGATED.search(_LOG_TIMESTAMP.sub("", line).strip()))
            ),
            None,
        )
        if match:
            return f"Downstream failure: {match.group('job').strip()} #{match.group('number')}"
        return "Downstream build failure propagated to this build"
    skip = (
        "erroraction$errorid",
        "script returned exit code",
        "[pipeline]",
        "sending email",
        "email was triggered",
        "timeout set to expire",
        "skipped due to earlier failure",
        "build returned error, collecting",
        "no test report files were found",
        "build step 'execute shell' marked build as failure",
        "finished: failure",
    )
    scored: list[tuple[int, int, str]] = []
    for index, line in enumerate(lines):
        cleaned = _LOG_TIMESTAMP.sub("", line).strip()
        lowered = cleaned.lower()
        if _STACK_FRAME.fullmatch(cleaned):
            continue
        if any(item in lowered for item in skip):
            continue
        score = _summary_score(lowered)
        if score:
            scored.append((score, index, cleaned))
    if scored:
        return max(scored)[2][:500]
    for line in reversed(lines):
        cleaned = _LOG_TIMESTAMP.sub("", line).strip()
        lowered = cleaned.lower()
        if not _STACK_FRAME.fullmatch(cleaned) and not any(item in lowered for item in skip):
            return cleaned[:500]
    if stage:
        return f"{stage} finished with {result}"
    return f"Build finished with {result}"


def _summary_score(line: str) -> int:
    if any(item in line for item in ("fatal error", "traceback", "exception", "assertion")):
        return 120
    if re.search(r"(?:^|\s)(?:error|fatal):\s", line):
        return 110
    if any(item in line for item in ("compilation failure", "make: ***", "rpmbuild returned error")):
        return 105
    if any(
        item in line
        for item in (
            "tests failed",
            "permission denied",
            "no such file",
            "cannot access",
            "connection refused",
            "timed out",
            "oomkilled",
            "outofmemory",
        )
    ):
        return 100
    if "timeout" in line:
        return 80
    if any(item in line for item in ("failed", "failure", "error")):
        return 60
    return 0
