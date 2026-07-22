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
    LOCK_KEY,
    acquire_lock,
    clear_scan_cancel,
    compute_diff,
    dismiss_fingerprint_with_details,
    get_dismissed_details,
    get_dismissed_fingerprints,
    get_last_run,
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
    undismiss_fingerprint,
)

SCAN_PROGRESS_KEY = "watchdog:scan:progress"

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


@router.get("/scan/status")
async def scan_status():
    """Return current scan status — used by UI to show background scan progress."""
    lock_held = False
    progress = None
    try:
        client = await get_valkey_client()
        lock_held = bool(await client.get(LOCK_KEY))
        raw_progress = await client.get(SCAN_PROGRESS_KEY)
        if raw_progress:
            progress = json.loads(raw_progress)
    except Exception:
        pass

    is_ui_scan = _active_scan is not None and not _active_scan.done()

    last_run = await get_last_run()

    return {
        "scanning": lock_held or is_ui_scan,
        "source": "ui" if is_ui_scan else ("scheduler" if lock_held else None),
        "last_run": last_run,
        "progress": progress,
    }


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

        dismissed = await get_dismissed_fingerprints()
        findings = [f for f in findings if f.fingerprint not in dismissed]

        await event_queue.put({
            "type": "detection_complete",
            "total_findings": len(findings),
            "deep": request.deep,
            "window_hours": scan_opts.jenkins_failed_build_window_hours,
        })

        # Dedup and correlate ALL findings before diff/investigation
        findings = deduplicate_findings(findings)
        findings = correlate_findings(findings)

        cluster_context = await gather_cluster_context()

        # LLM triage — skip in deep scan to keep more findings for investigation
        if findings and not request.investigate_all and not request.deep:
            await event_queue.put({"type": "triage_start", "count": len(findings)})
            triage_result = await triage_findings(findings, cluster_context=cluster_context)
            total_prompt_tokens += triage_result.prompt_tokens
            total_completion_tokens += triage_result.completion_tokens

            dismissed_fps = {d.finding.fingerprint for d in triage_result.dismissed}
            findings = [f for f in findings if f.fingerprint not in dismissed_fps]

            for d in triage_result.dismissed:
                await dismiss_fingerprint_with_details(
                    d.finding.fingerprint,
                    reason=d.reason,
                    auto=True,
                    symptom=d.finding.symptom,
                )

            await event_queue.put({
                "type": "triage_complete",
                "total_findings": len(findings),
                "dismissed_count": len(triage_result.dismissed),
                "correlation_groups": len(triage_result.correlations),
            })

        elif findings and request.deep:
            await event_queue.put({
                "type": "triage_skipped",
                "count": len(findings),
                "deep": True,
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

        if not request.investigate_all:
            to_investigate = [
                f for f in to_investigate
                if should_investigate(f, diff, existing_investigations, deep=request.deep)
            ]

        to_investigate.sort(key=priority_score, reverse=True)
        to_investigate = to_investigate[: scan_opts.max_investigations_per_scan]

        logger.info(
            "[scan:%s] Investigation plan: deep=%s count=%d max=%d",
            scan_id,
            request.deep,
            len(to_investigate),
            scan_opts.max_investigations_per_scan,
        )

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

        await store_run_result(findings, scan_id, duration_s, token_usage, diff=diff, deep=request.deep)
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
        last_scan_deep=info.get("deep"),
        total_findings=len(finding_responses),
        findings=finding_responses,
    )


@router.get("/history")
async def get_history(limit: int = 20):
    """Return recent scan history for trend analysis."""
    history = await get_scan_history(min(limit, 50))
    return {"scans": history, "count": len(history)}


@router.get("/findings/dismissed")
async def get_dismissed_findings():
    dismissed = await get_dismissed_details()
    return {"dismissed": dismissed, "count": len(dismissed)}


@router.delete("/findings/{fingerprint}")
async def dismiss_finding(fingerprint: str, reason: str = "Manually dismissed"):
    from jenkins_watchdog.state import FINDINGS_KEY

    client = await get_valkey_client()
    raw = await client.get(FINDINGS_KEY)
    if not raw:
        return {"status": "not_found"}

    findings = json.loads(raw)
    found = False
    for f in findings:
        if f.get("fingerprint") == fingerprint:
            f["status"] = "dismissed"
            found = True
            break

    if not found:
        return {"status": "not_found"}

    await client.set(FINDINGS_KEY, json.dumps(findings, default=str), ex=604800)
    await dismiss_fingerprint_with_details(fingerprint, reason=reason)

    return {"status": "dismissed", "reason": reason}


@router.post("/findings/{fingerprint}/undismiss")
async def undismiss_finding(fingerprint: str):
    from jenkins_watchdog.state import FINDINGS_KEY

    client = await get_valkey_client()
    raw = await client.get(FINDINGS_KEY)
    if not raw:
        return {"status": "not_found"}

    findings = json.loads(raw)
    found = False
    for f in findings:
        if f.get("fingerprint") == fingerprint:
            f["status"] = "ongoing"
            found = True
            break

    if not found:
        return {"status": "not_found"}

    await client.set(FINDINGS_KEY, json.dumps(findings, default=str), ex=604800)
    await undismiss_fingerprint(fingerprint)

    return {"status": "restored"}


@router.delete("/reset")
async def reset_state():
    """Clear all stored findings, investigations, and history."""
    from jenkins_watchdog.state import FINDINGS_KEY, HISTORY_KEY, LAST_RUN_KEY

    client = await get_valkey_client()
    await client.delete(INVESTIGATIONS_KEY, FINDINGS_KEY, HISTORY_KEY, LAST_RUN_KEY)
    return {"status": "reset", "message": "All state cleared. Next scan will treat all findings as new."}


import litellm
from pydantic import BaseModel

from jenkins_watchdog.config import settings
from jenkins_watchdog.reasoning.engine import _parse_investigation
from jenkins_watchdog.tools import ALL_TOOL_DEFINITIONS, execute_tool

_FINDING_CHAT_TTL = 604800
_FINDING_CHAT_KEY_PREFIX = "watchdog:chat:finding:"

_FINDING_CHAT_SYSTEM = """You are an expert Jenkins CI/CD and Kubernetes platform engineer investigating a production issue.
You have access to tools that query real-time state: Kubernetes API, Jenkins API, and Prometheus metrics.

When the user asks about this issue:
1. Use the available tools to gather real evidence
2. Correlate findings across multiple data sources
3. Provide specific, actionable answers with evidence

## Finding you are discussing:
Resource: {resource}
Symptom: {symptom}
Severity: {severity}
Category: {category}
Context: {context}

## Your previous investigation:
Root Cause: {root_cause}
Evidence: {evidence}
Impact: {impact}
Suggested Fix: {suggested_fix}
{fix_location}
Confidence: {confidence}

## Raw reasoning from your investigation:
{raw_reasoning}

The user may challenge your conclusions, provide corrections, or ask you to dig deeper.
Use your tools to verify claims and re-examine evidence when asked."""

_CORRECTION_PROMPT = """Based on the conversation above, produce a CORRECTED investigation.
Extract the corrected findings into this exact JSON format:

{"root_cause":"...","evidence":["..."],"impact":"...","suggested_fix":"...","fix_location":"...","confidence":"high|medium|low"}

Incorporate corrections from the conversation. Keep validated evidence, remove debunked evidence.
Return ONLY the JSON object, no other text."""


class FindingChatRequest(BaseModel):
    message: str


def _finding_chat_key(fingerprint: str) -> str:
    return f"{_FINDING_CHAT_KEY_PREFIX}{fingerprint}"


def _finding_chat_model_chain() -> list[str]:
    models = [settings.llm_model]
    if settings.llm_fallback_models:
        models.extend(m.strip() for m in settings.llm_fallback_models.split(",") if m.strip())
    return models


async def _load_finding_chat_messages(fingerprint: str) -> list[dict] | None:
    client = await get_valkey_client()
    data = await client.get(_finding_chat_key(fingerprint))
    if data:
        return json.loads(data)
    return None


async def _save_finding_chat_messages(fingerprint: str, messages: list[dict]) -> None:
    client = await get_valkey_client()
    await client.set(
        _finding_chat_key(fingerprint),
        json.dumps(messages, default=str),
        ex=_FINDING_CHAT_TTL,
    )


async def _get_finding_dict(fingerprint: str) -> dict | None:
    from jenkins_watchdog.state import FINDINGS_KEY

    client = await get_valkey_client()
    raw = await client.get(FINDINGS_KEY)
    if not raw:
        return None
    for finding in json.loads(raw):
        if finding.get("fingerprint") == fingerprint:
            return finding
    return None


async def _get_investigation_dict(fingerprint: str) -> dict | None:
    investigations = await get_stored_investigations()
    return investigations.get(fingerprint)


def _format_finding_chat_system_prompt(finding: dict, investigation: dict | None) -> str:
    inv = investigation or {}
    evidence = inv.get("evidence") or []
    evidence_text = "\n".join(f"- {item}" for item in evidence) if evidence else "None"
    fix_location = ""
    if inv.get("fix_location"):
        fix_location = f"Fix Location: {inv['fix_location']}"
    context = json.dumps(finding.get("context") or {}, indent=2, default=str)
    return _FINDING_CHAT_SYSTEM.format(
        resource=finding.get("resource", ""),
        symptom=finding.get("symptom", ""),
        severity=finding.get("severity", ""),
        category=finding.get("category", ""),
        context=context,
        root_cause=inv.get("root_cause") or "Not yet investigated",
        evidence=evidence_text,
        impact=inv.get("impact") or "Unknown",
        suggested_fix=inv.get("suggested_fix") or "None",
        fix_location=fix_location,
        confidence=inv.get("confidence") or "unknown",
        raw_reasoning=inv.get("raw_reasoning") or "None",
    )


def _visible_chat_messages(messages: list[dict]) -> list[dict]:
    visible = []
    for message in messages:
        role = message.get("role")
        if role not in ("user", "assistant"):
            continue
        content = message.get("content")
        if not content:
            continue
        visible.append({"role": role, "content": content})
    return visible


async def _init_finding_chat_messages(fingerprint: str) -> list[dict] | None:
    finding = await _get_finding_dict(fingerprint)
    if not finding:
        return None
    investigation = await _get_investigation_dict(fingerprint)
    return [{"role": "system", "content": _format_finding_chat_system_prompt(finding, investigation)}]


async def _call_finding_chat_llm(
    model_chain: list[str],
    messages: list[dict],
    *,
    tools: list[dict] | None = ALL_TOOL_DEFINITIONS,
):
    last_error = None
    for model in model_chain:
        try:
            kwargs = {
                "model": model,
                "messages": messages,
                "temperature": settings.llm_temperature,
                "max_tokens": settings.llm_max_tokens,
                "api_key": settings.anthropic_api_key,
            }
            if tools is not None:
                kwargs["tools"] = tools
            return await litellm.acompletion(**kwargs)
        except Exception as e:
            last_error = e
            logger.warning("Finding chat LLM call failed for model %s: %s", model, e)
            await asyncio.sleep(1)
    raise last_error or RuntimeError("All models failed")


def _merge_investigation(existing: dict | None, corrected: Investigation, fingerprint: str) -> dict:
    merged = corrected.model_dump()
    merged["finding_fingerprint"] = fingerprint
    if existing:
        for field in ("tools_used", "prompt_tokens", "completion_tokens", "estimated_cost_usd", "raw_reasoning"):
            if field in existing:
                merged[field] = existing[field]
    return merged


@router.get("/findings/{fingerprint}/chat")
async def get_finding_chat(fingerprint: str):
    messages = await _load_finding_chat_messages(fingerprint)
    if not messages:
        return {"messages": []}
    return {"messages": _visible_chat_messages(messages)}


@router.post("/findings/{fingerprint}/chat")
async def finding_chat(fingerprint: str, request: FindingChatRequest):
    messages = await _load_finding_chat_messages(fingerprint)
    if messages is None:
        messages = await _init_finding_chat_messages(fingerprint)
        if messages is None:
            async def _not_found_stream():
                yield {"data": json.dumps({"type": "error", "message": f"Finding not found: {fingerprint}"})}
            return _scan_sse(_not_found_stream())

    messages.append({"role": "user", "content": request.message})

    async def event_stream():
        model_chain = _finding_chat_model_chain()

        for _ in range(settings.max_tool_rounds):
            try:
                response = await _call_finding_chat_llm(model_chain, messages)
            except Exception as e:
                yield {"data": json.dumps({"type": "error", "content": str(e)})}
                return

            choice = response.choices[0].message
            content = choice.content or ""
            tool_calls = choice.tool_calls or []

            assistant_msg: dict = {"role": "assistant", "content": content or None}
            if tool_calls:
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in tool_calls
                ]
            messages.append(assistant_msg)

            if content:
                yield {"data": json.dumps({"type": "token", "content": content})}

            if not tool_calls:
                await _save_finding_chat_messages(fingerprint, messages)
                yield {"data": json.dumps({"type": "done", "fingerprint": fingerprint})}
                return

            for tc in tool_calls:
                tool_name = tc.function.name
                try:
                    tool_args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    tool_args = {}

                yield {"data": json.dumps({"type": "tool_start", "tool_name": tool_name, "tool_args": tool_args})}

                result = await execute_tool(tool_name, tool_args)
                success = not result.startswith("Error") and not result.startswith("Unknown tool")

                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
                yield {"data": json.dumps({"type": "tool_result", "tool_name": tool_name, "success": success})}

        await _save_finding_chat_messages(fingerprint, messages)
        yield {"data": json.dumps({"type": "token", "content": "\n\n(Reached tool call limit — showing partial results)"})}
        yield {"data": json.dumps({"type": "done", "fingerprint": fingerprint})}

    return _scan_sse(event_stream())


@router.post("/findings/{fingerprint}/reinvestigate")
async def reinvestigate_finding(fingerprint: str):
    messages = await _load_finding_chat_messages(fingerprint)
    if not messages:
        return {"status": "error", "message": "No chat session found for this finding"}

    finding = await _get_finding_dict(fingerprint)
    if not finding:
        return {"status": "error", "message": f"Finding not found: {fingerprint}"}

    existing = await _get_investigation_dict(fingerprint)
    messages.append({"role": "user", "content": _CORRECTION_PROMPT})

    try:
        response = await _call_finding_chat_llm(_finding_chat_model_chain(), messages, tools=None)
    except Exception as e:
        return {"status": "error", "message": str(e)}

    content = response.choices[0].message.content or ""
    messages.append({"role": "assistant", "content": content})
    await _save_finding_chat_messages(fingerprint, messages)

    tools_used = (existing or {}).get("tools_used", [])
    corrected = _parse_investigation(content, fingerprint, tools_used)
    merged = _merge_investigation(existing, corrected, fingerprint)

    client = await get_valkey_client()
    investigations = await get_stored_investigations()
    investigations[fingerprint] = merged
    await client.set(INVESTIGATIONS_KEY, json.dumps(investigations, default=str), ex=_FINDING_CHAT_TTL)

    return {"status": "corrected", "investigation": merged}
