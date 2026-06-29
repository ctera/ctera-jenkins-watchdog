"""API router — scan trigger (SSE stream) and findings retrieval."""

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse

from jenkins_watchdog.api.models import (
    FindingResponse,
    FindingsResponse,
    Investigation,
    JiraIssueRef,
    ScanRequest,
)
from jenkins_watchdog.checks.agent_utils import group_agent_findings
from jenkins_watchdog.checks.base import Finding
from jenkins_watchdog.checks.registry import run_all_checks
from jenkins_watchdog.clients.valkey import get_valkey_client
from jenkins_watchdog.reasoning.context import gather_cluster_context
from jenkins_watchdog.reasoning.engine import investigate_finding
from jenkins_watchdog.reasoning.gate import should_investigate
from jenkins_watchdog.reasoning.triage import triage_findings
from jenkins_watchdog.scan_options import ScanOptions, activate_scan_options, reset_scan_options
from jenkins_watchdog.state import (
    INVESTIGATIONS_KEY,
    acquire_lock,
    clear_scan_cancel,
    compute_diff,
    get_last_run_info,
    get_previous_findings,
    get_scan_history,
    get_stored_investigations,
    is_scan_cancelled,
    refresh_lock,
    release_lock,
    request_scan_cancel,
    store_investigations,
    store_run_result,
)

logger = logging.getLogger(__name__)

router = APIRouter()

SSE_PING_INTERVAL = 15
SSE_HEARTBEAT_INTERVAL = 15


def _scan_sse(content):
    """SSE response with periodic pings to keep proxies (Traefik) from timing out."""
    return EventSourceResponse(content, media_type="text/event-stream", ping=SSE_PING_INTERVAL)


CATEGORY_WEIGHT = {
    "jenkins_controller": 100,
    "jenkins_agent": 80,
    "jenkins_queue": 70,
    "jenkins_pipeline_pattern": 65,
    "jenkins_failed_build": 60,
    "jenkins_build": 55,
    "k8s_workload": 50,
    "k8s_event": 45,
    "k8s_node": 40,
}
SEVERITY_WEIGHT = {"critical": 50, "warning": 20, "low": 5}

_STATEFULSET_SUFFIX = re.compile(r"-\d+$")
_DEPLOYMENT_SUFFIX = re.compile(r"-[a-z0-9]{5,10}-[a-z0-9]{4,5}$")


def priority_score(finding: Finding) -> int:
    return CATEGORY_WEIGHT.get(finding.category, 30) + SEVERITY_WEIGHT.get(finding.severity, 0)


def _extract_workload_key(resource: str) -> str:
    """Extract workload grouping key from a resource string like 'ns/pod-name'."""
    parts = resource.split("/")
    if len(parts) < 2:
        return resource
    ns, name = parts[0], parts[1]
    base = _STATEFULSET_SUFFIX.sub("", name)
    base = _DEPLOYMENT_SUFFIX.sub("", base)
    return f"{ns}/{base}"


_REDUNDANT_SYMPTOMS = {
    "CrashLoopBackOff": {"OOMKilled"},
    "ImagePullBackOff": set(),
}


def deduplicate_findings(findings: list[Finding]) -> list[Finding]:
    """Keep only the highest-severity finding per resource, merging symptoms."""
    by_resource: dict[str, Finding] = {}
    for f in findings:
        if f.resource not in by_resource:
            by_resource[f.resource] = f
        else:
            existing = by_resource[f.resource]
            if SEVERITY_WEIGHT.get(f.severity, 0) > SEVERITY_WEIGHT.get(existing.severity, 0):
                f.context["also_seen"] = existing.symptom
                by_resource[f.resource] = f
            else:
                prev = existing.context.get("also_seen", "")
                existing.context["also_seen"] = f"{prev}; {f.symptom}" if prev else f.symptom

    workload_symptoms: dict[str, set[str]] = {}
    for f in by_resource.values():
        wk = _extract_workload_key(f.resource)
        workload_symptoms.setdefault(wk, set())
        for keyword in ("OOMKilled", "CrashLoopBackOff", "ImagePullBackOff"):
            if keyword in f.symptom:
                workload_symptoms[wk].add(keyword)

    result = []
    for f in by_resource.values():
        wk = _extract_workload_key(f.resource)
        dominated = False
        for symptom_key, suppressed_by in _REDUNDANT_SYMPTOMS.items():
            if symptom_key in f.symptom and suppressed_by & workload_symptoms.get(wk, set()):
                dominated = True
                break
        if not dominated:
            result.append(f)
    return result


def correlate_findings(findings: list[Finding]) -> list[Finding]:
    """Group related findings: agents, pipeline failures, infrastructure."""
    findings = group_agent_findings(findings)

    # Group jenkins-job findings with same error signature
    sig_groups: dict[str, list[Finding]] = {}
    no_sig: list[Finding] = []
    for f in findings:
        sig = f.context.get("error_signature") or ""
        if sig and f.category in ("jenkins_failed_build", "jenkins_pipeline_pattern"):
            sig_groups.setdefault(sig, []).append(f)
        else:
            no_sig.append(f)

    merged: list[Finding] = list(no_sig)
    for sig, group in sig_groups.items():
        if len(group) == 1:
            merged.append(group[0])
            continue
        primary = max(group, key=priority_score)
        related = [g for g in group if g is not primary]
        primary.context["correlated_jobs"] = [g.context.get("job_name", g.resource) for g in related]
        primary.context["correlated_findings"] = primary.context.get("correlated_findings", []) + [
            f"{g.resource}: {g.symptom}" for g in related
        ]
        primary.context["correlation_group_size"] = 1 + len(related)
        primary.symptom = f"{primary.symptom} (+{len(related)} jobs with same error signature)"
        merged.append(primary)

    # Link build failures to K8s events on same node/pod
    build_findings = [f for f in merged if f.category.startswith("jenkins_")]
    k8s_findings = [f for f in merged if f.category.startswith("k8s_")]
    linked_k8s: set[str] = set()

    for bf in build_findings:
        node = bf.context.get("built_on") or bf.context.get("node", "")
        for kf in k8s_findings:
            if kf.fingerprint in linked_k8s:
                continue
            host = kf.context.get("source", {}).get("host", "")
            obj_name = kf.context.get("involved_object", {}).get("name", "")
            if node and (node in host or node in obj_name or node in kf.resource):
                bf.context.setdefault("correlated_findings", []).append(
                    f"{kf.resource}: {kf.symptom}"
                )
                linked_k8s.add(kf.fingerprint)

    # Group multiple failures on same K8s node
    node_groups: dict[str, list[Finding]] = {}
    ungrouped: list[Finding] = []
    for f in merged:
        node = ""
        if f.context.get("source", {}).get("host"):
            node = f.context["source"]["host"]
        elif f.category == "k8s_node":
            node = f.resource.split("/")[-1]
        if node:
            node_groups.setdefault(node, []).append(f)
        else:
            ungrouped.append(f)

    for node, group in node_groups.items():
        if len(group) == 1:
            ungrouped.append(group[0])
            continue
        jenkins_related = [g for g in group if g.category.startswith("jenkins_")]
        if len(jenkins_related) >= 2 or (jenkins_related and len(group) >= 2):
            primary = max(group, key=priority_score)
            related = [g for g in group if g is not primary]
            primary.context.setdefault("correlated_findings", []).extend(
                f"{g.resource}: {g.symptom}" for g in related
            )
            primary.context["node_correlation"] = node
            primary.context["correlation_group_size"] = 1 + len(related)
            primary.symptom = f"{primary.symptom} (node {node}: {len(group)} related issues)"
            ungrouped.append(primary)
        else:
            ungrouped.extend(group)

    return ungrouped


_active_scan: asyncio.Task | None = None
_scan_events: asyncio.Queue | None = None
_scan_cancel_event: asyncio.Event | None = None
_current_investigation: asyncio.Task | None = None


@router.post("/scan")
async def trigger_scan(request: ScanRequest | None = None):
    """Run scan as a background task; stream progress via SSE."""
    global _active_scan, _scan_events

    if _active_scan and not _active_scan.done():
        return _scan_sse(_follow_active_scan())

    if not await acquire_lock():
        async def _error_stream():
            yield {"data": json.dumps({"type": "error", "message": "Another scan is already running. Please wait."})}
        return _scan_sse(_error_stream())

    _scan_events = asyncio.Queue()
    _scan_cancel_event = asyncio.Event()
    await clear_scan_cancel()
    _active_scan = asyncio.create_task(_run_scan_background(request or ScanRequest(), _scan_events))

    return _scan_sse(_follow_active_scan())


@router.post("/scan/stop")
async def stop_scan():
    """Request cancellation of the currently running scan."""
    global _active_scan, _scan_cancel_event, _current_investigation

    if _active_scan is None or _active_scan.done():
        return {"status": "not_running"}

    await request_scan_cancel()
    if _scan_cancel_event:
        _scan_cancel_event.set()

    if _current_investigation and not _current_investigation.done():
        _current_investigation.cancel()

    _active_scan.cancel()
    return {"status": "stopping"}


async def _follow_active_scan():
    """SSE generator that reads events from the background scan task."""
    global _scan_events
    if _scan_events is None:
        yield {"data": json.dumps({"type": "error", "message": "No active scan to follow."})}
        return

    queue = _scan_events
    while True:
        try:
            event = await asyncio.wait_for(queue.get(), timeout=SSE_HEARTBEAT_INTERVAL)
        except asyncio.TimeoutError:
            yield {"data": json.dumps({"type": "heartbeat"})}
            continue

        if event is None:
            break
        yield {"data": json.dumps(event)}


async def _run_scan_background(request: ScanRequest, event_queue: asyncio.Queue):
    """Background scan task — runs to completion regardless of SSE client state."""
    global _active_scan, _scan_events, _scan_cancel_event, _current_investigation
    scan_id = str(uuid.uuid4())[:8]
    started_at = datetime.now(timezone.utc)
    total_prompt_tokens = 0
    total_completion_tokens = 0
    token = None

    try:
        scan_opts = ScanOptions.deep_scan() if request.deep else ScanOptions.from_settings()
        token = activate_scan_options(scan_opts)

        await event_queue.put({
            "type": "scan_started",
            "scan_id": scan_id,
            "deep": request.deep,
        })

        findings = await run_all_checks(_scan_cancel_event)
        if _scan_cancel_event and _scan_cancel_event.is_set():
            raise asyncio.CancelledError()
        await event_queue.put({"type": "detection_complete", "total_findings": len(findings)})

        # Dedup and correlate ALL findings before diff/investigation
        findings = deduplicate_findings(findings)
        findings = correlate_findings(findings)

        cluster_context = await gather_cluster_context()

        # LLM triage — classify noise vs investigate-worthy findings
        if findings and not request.investigate_all:
            await event_queue.put({"type": "triage_start", "count": len(findings)})
            triage_result = await triage_findings(findings, cluster_context=cluster_context)
            total_prompt_tokens += triage_result.prompt_tokens
            total_completion_tokens += triage_result.completion_tokens

            dismissed_fps = {d.finding.fingerprint for d in triage_result.dismissed}
            findings = [f for f in findings if f.fingerprint not in dismissed_fps]

            await event_queue.put({
                "type": "triage_complete",
                "total_findings": len(findings),
                "dismissed_count": len(triage_result.dismissed),
                "correlation_groups": len(triage_result.correlations),
            })

        previous = await get_previous_findings()
        diff = compute_diff(previous, findings)
        existing_investigations = await get_stored_investigations()

        to_investigate = []
        if request.investigate_all:
            to_investigate = list(findings)
        elif request.deep:
            to_investigate = [f for f in findings if f.severity in ("critical", "warning")]
        else:
            to_investigate = [f for f in diff.new if f.severity in ("critical", "warning")]
            to_investigate += [f for f in diff.ongoing if f.severity == "critical"]
            # Always investigate pipeline patterns and shared failure signatures
            to_investigate += [
                f for f in findings
                if f.category == "jenkins_pipeline_pattern"
                and f.context.get("pattern") in ("consecutive_failures", "shared_failure_signature", "regression")
                and f not in to_investigate
            ]

        to_investigate.sort(key=priority_score, reverse=True)
        to_investigate = to_investigate[: scan_opts.max_investigations_per_scan]

        if not request.investigate_all:
            to_investigate = [
                f for f in to_investigate
                if should_investigate(f, diff, existing_investigations, deep=request.deep)
            ]

        await event_queue.put({"type": "investigation_plan", "count": len(to_investigate), "deep": request.deep})

        investigations: dict[str, Investigation] = {}
        for idx, finding in enumerate(to_investigate):
            if _scan_cancel_event and _scan_cancel_event.is_set():
                raise asyncio.CancelledError()
            if await is_scan_cancelled():
                raise asyncio.CancelledError()

            await refresh_lock()
            await event_queue.put({
                "type": "investigation_start",
                "index": idx + 1,
                "total": len(to_investigate),
                "resource": finding.resource,
                "symptom": finding.symptom,
            })

            try:
                def make_progress_emitter(resource: str, q: asyncio.Queue):
                    def on_progress(event: dict):
                        event["resource"] = resource
                        q.put_nowait(event)
                    return on_progress

                on_progress = make_progress_emitter(finding.resource, event_queue)
                _current_investigation = asyncio.create_task(
                    investigate_finding(
                        finding,
                        on_progress=on_progress,
                        cluster_context=cluster_context,
                        all_findings=findings,
                    )
                )
                try:
                    result = await _current_investigation
                finally:
                    _current_investigation = None

                if result:
                    investigations[finding.fingerprint] = result
                    total_prompt_tokens += result.prompt_tokens
                    total_completion_tokens += result.completion_tokens
                    await refresh_lock()
                    await event_queue.put({
                        "type": "investigation_complete",
                        "resource": finding.resource,
                        "root_cause": result.root_cause[:300],
                        "confidence": result.confidence,
                        "tools_used": result.tools_used,
                        "prompt_tokens": result.prompt_tokens,
                        "completion_tokens": result.completion_tokens,
                        "estimated_cost_usd": result.estimated_cost_usd,
                    })
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("[scan:%s] Investigation failed for %s", scan_id, finding.resource)
                await event_queue.put({"type": "investigation_error", "resource": finding.resource, "error": str(e)[:200]})

        completed_at = datetime.now(timezone.utc)
        duration_s = (completed_at - started_at).total_seconds()

        total_cost = sum(inv.estimated_cost_usd for inv in investigations.values())
        token_usage = {
            "prompt_tokens": total_prompt_tokens,
            "completion_tokens": total_completion_tokens,
            "estimated_cost_usd": round(total_cost, 4),
        }

        await store_run_result(findings, scan_id, duration_s, token_usage, diff=diff)
        await store_investigations(investigations)

        await event_queue.put({
            "type": "scan_complete",
            "scan_id": scan_id,
            "deep": request.deep,
            "total_findings": len(findings),
            "new_findings": diff.new_count,
            "critical_findings": len([f for f in findings if f.severity == "critical"]),
            "investigations_performed": len(investigations),
            "duration_s": round(duration_s, 1),
            **token_usage,
        })
    except asyncio.CancelledError:
        duration_s = (datetime.now(timezone.utc) - started_at).total_seconds()
        logger.info("[scan:%s] Scan cancelled by user after %.1fs", scan_id, duration_s)
        await event_queue.put({
            "type": "scan_stopped",
            "scan_id": scan_id,
            "deep": request.deep,
            "duration_s": round(duration_s, 1),
        })
    except Exception as e:
        logger.exception("[scan:%s] Scan failed", scan_id)
        await event_queue.put({"type": "error", "message": f"Scan failed: {str(e)[:200]}"})
    finally:
        if _current_investigation and not _current_investigation.done():
            _current_investigation.cancel()
            _current_investigation = None
        try:
            reset_scan_options(token)
        except Exception:
            pass
        await release_lock()
        await clear_scan_cancel()
        await event_queue.put(None)
        _active_scan = None
        _scan_events = None
        _scan_cancel_event = None


@router.get("/findings", response_model=FindingsResponse)
async def get_findings():
    """Retrieve the latest scan findings with any investigations."""
    info = await get_last_run_info()
    previous = await get_previous_findings()
    investigations = await get_stored_investigations()

    finding_responses = []
    for f_dict in previous:
        fp = f_dict.get("fingerprint", "")
        inv = investigations.get(fp)
        jira_ref = None
        jira_data = f_dict.get("jira_issue")
        if jira_data and isinstance(jira_data, dict):
            jira_ref = JiraIssueRef(key=jira_data["key"], url=jira_data["url"])
        finding_responses.append(
            FindingResponse(
                severity=f_dict.get("severity", "low"),
                category=f_dict.get("category", ""),
                resource=f_dict.get("resource", ""),
                symptom=f_dict.get("symptom", ""),
                context=f_dict.get("context", {}),
                fingerprint=fp,
                status=f_dict.get("status", "ongoing"),
                first_seen=f_dict.get("first_seen"),
                last_seen=f_dict.get("last_seen"),
                investigation=Investigation(**inv) if inv else None,
                jira_issue=jira_ref,
            )
        )

    last_scan = None
    if info.get("last_run"):
        last_scan = datetime.fromisoformat(info["last_run"])

    return FindingsResponse(
        last_scan=last_scan,
        total_findings=len(finding_responses),
        findings=finding_responses,
    )


@router.get("/history")
async def get_history(limit: int = 20):
    """Return recent scan history for trend analysis."""
    history = await get_scan_history(min(limit, 50))
    return {"scans": history, "count": len(history)}


@router.delete("/findings/{fingerprint}")
async def dismiss_finding(fingerprint: str):
    """Remove a specific finding and its investigation from state."""
    from jenkins_watchdog.state import FINDINGS_KEY

    client = await get_valkey_client()
    raw = await client.get(FINDINGS_KEY)
    if not raw:
        return {"status": "not_found", "remaining": 0}

    findings = json.loads(raw)
    original_count = len(findings)
    findings = [f for f in findings if f.get("fingerprint") != fingerprint]

    if len(findings) == original_count:
        return {"status": "not_found", "remaining": len(findings)}

    await client.set(FINDINGS_KEY, json.dumps(findings, default=str), ex=604800)

    inv_raw = await client.get(INVESTIGATIONS_KEY)
    if inv_raw:
        investigations = json.loads(inv_raw)
        investigations.pop(fingerprint, None)
        await client.set(INVESTIGATIONS_KEY, json.dumps(investigations, default=str), ex=604800)

    return {"status": "dismissed", "remaining": len(findings)}


@router.delete("/reset")
async def reset_state():
    """Clear all stored findings, investigations, and history."""
    from jenkins_watchdog.state import FINDINGS_KEY, HISTORY_KEY, LAST_RUN_KEY

    client = await get_valkey_client()
    await client.delete(INVESTIGATIONS_KEY, FINDINGS_KEY, HISTORY_KEY, LAST_RUN_KEY)
    return {"status": "reset", "message": "All state cleared. Next scan will treat all findings as new."}
