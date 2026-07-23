"""Background scan scheduler — runs periodic regular and deep scans."""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

from jenkins_watchdog.config import settings

logger = logging.getLogger(__name__)

_scheduler_task: asyncio.Task | None = None
_last_regular_scan: float = 0
_last_deep_scan: float = 0


async def _scheduler_loop():
    global _last_regular_scan, _last_deep_scan

    regular_interval = settings.scheduler_scan_interval_minutes * 60
    deep_interval = settings.scheduler_deep_scan_interval_minutes * 60

    await asyncio.sleep(10)

    try:
        from jenkins_watchdog.state import release_lock
        await release_lock()
        logger.info("[scheduler] Cleared any stale lock from previous run")
    except Exception:
        pass

    await asyncio.sleep(50)

    while True:
        try:
            now = time.monotonic()

            if deep_interval > 0 and (now - _last_deep_scan) >= deep_interval:
                logger.info("[scheduler] Starting scheduled deep scan")
                if await _run_scheduled_scan(deep=True):
                    _last_deep_scan = time.monotonic()
                    _last_regular_scan = time.monotonic()

            elif regular_interval > 0 and (now - _last_regular_scan) >= regular_interval:
                logger.info("[scheduler] Starting scheduled regular scan")
                if await _run_scheduled_scan(deep=False):
                    _last_regular_scan = time.monotonic()

            await asyncio.sleep(30)

        except asyncio.CancelledError:
            logger.info("[scheduler] Scheduler stopped")
            return
        except Exception as e:
            logger.exception("[scheduler] Scheduler error: %s", e)
            await asyncio.sleep(60)


async def _is_cancelled() -> bool:
    try:
        from jenkins_watchdog.clients.valkey import get_valkey_client
        client = await get_valkey_client()
        return bool(await client.get("watchdog:scan:cancelled"))
    except Exception:
        return False


async def _run_scheduled_scan(deep: bool = False) -> bool:
    """Run a scan programmatically. Returns True if the scan actually ran."""
    from jenkins_watchdog.checks.registry import run_all_checks
    from jenkins_watchdog.api.router import deduplicate_findings, correlate_findings, priority_score
    from jenkins_watchdog.reasoning.context import gather_cluster_context
    from jenkins_watchdog.reasoning.engine import investigate_finding
    from jenkins_watchdog.reasoning.gate import should_investigate
    from jenkins_watchdog.reasoning.triage import triage_findings
    from jenkins_watchdog.scan_options import ScanOptions, activate_scan_options, reset_scan_options
    from jenkins_watchdog.state import (
        acquire_lock, release_lock, refresh_lock,
        compute_diff, get_previous_findings, get_stored_investigations,
        store_investigations, store_run_result,
    )
    from jenkins_watchdog.api.models import Investigation
    import uuid

    if not await acquire_lock():
        logger.info("[scheduler] Scan lock busy — skipping scheduled scan")
        return False

    from jenkins_watchdog.clients.valkey import get_valkey_client
    client = await get_valkey_client()
    await client.set(
        "watchdog:scan:progress",
        json.dumps({
            "phase": "checks",
            "deep": deep,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }),
        ex=3600,
    )

    scan_id = str(uuid.uuid4())[:8]
    started_at = datetime.now(timezone.utc)
    token = None

    try:
        scan_opts = ScanOptions.deep_scan() if deep else ScanOptions.from_settings()
        token = activate_scan_options(scan_opts)

        findings = await run_all_checks()

        from jenkins_watchdog.state import get_dismissed_fingerprints
        dismissed = await get_dismissed_fingerprints()
        findings = [f for f in findings if f.fingerprint not in dismissed]

        findings = deduplicate_findings(findings)
        findings = correlate_findings(findings)

        cluster_context = await gather_cluster_context()

        if findings and not deep:
            triage_result = await triage_findings(findings, cluster_context=cluster_context)
            dismissed_fps = {d.finding.fingerprint for d in triage_result.dismissed}

            from jenkins_watchdog.state import dismiss_fingerprint_with_details
            for d in triage_result.dismissed:
                await dismiss_fingerprint_with_details(
                    d.finding.fingerprint, reason=d.reason, auto=True, symptom=d.finding.symptom,
                )

            findings = [f for f in findings if f.fingerprint not in dismissed_fps]

        previous = await get_previous_findings()
        diff = compute_diff(previous, findings)
        existing_investigations = await get_stored_investigations()

        if deep:
            to_investigate = [f for f in findings if f.severity in ("critical", "warning")]
        else:
            to_investigate = [f for f in diff.new if f.severity in ("critical", "warning")]
            to_investigate += [f for f in diff.ongoing if f.severity == "critical"]

        to_investigate = [
            f for f in to_investigate
            if should_investigate(f, diff, existing_investigations, deep=deep)
        ]
        to_investigate.sort(key=priority_score, reverse=True)
        to_investigate = to_investigate[:scan_opts.max_investigations_per_scan]

        max_scheduled = 10 if deep else scan_opts.max_investigations_per_scan
        to_investigate = to_investigate[:max_scheduled]

        await client.set(
            "watchdog:scan:progress",
            json.dumps({
                "phase": "investigating",
                "deep": deep,
                "total": len(to_investigate),
                "completed": 0,
            }),
            ex=3600,
        )

        investigations: dict[str, Investigation] = {}
        completed = 0
        for finding in to_investigate:
            await refresh_lock()
            cancelled = await _is_cancelled()
            if cancelled:
                logger.info("[scheduler] Scan cancelled — stopping investigations")
                break
            try:
                result = await investigate_finding(
                    finding, cluster_context=cluster_context, all_findings=findings,
                )
                if result:
                    investigations[finding.fingerprint] = result
            except Exception as e:
                logger.warning("[scheduler] Investigation failed for %s: %s", finding.resource, e)
            completed += 1
            await client.set(
                "watchdog:scan:progress",
                json.dumps({
                    "phase": "investigating",
                    "deep": deep,
                    "total": len(to_investigate),
                    "completed": completed,
                }),
                ex=3600,
            )

        duration_s = (datetime.now(timezone.utc) - started_at).total_seconds()
        total_cost = sum(inv.estimated_cost_usd for inv in investigations.values())
        token_usage = {
            "prompt_tokens": sum(inv.prompt_tokens for inv in investigations.values()),
            "completion_tokens": sum(inv.completion_tokens for inv in investigations.values()),
            "estimated_cost_usd": round(total_cost, 4),
        }

        await store_run_result(findings, scan_id, duration_s, token_usage, diff=diff, deep=deep)
        await store_investigations(investigations)

        logger.info(
            "[scheduler] Scan %s complete: deep=%s findings=%d investigations=%d duration=%.1fs",
            scan_id, deep, len(findings), len(investigations), duration_s,
        )

        if settings.auto_jira_enabled:
            await _auto_create_jira_bugs(findings, investigations, diff)

        return True

    except Exception as e:
        logger.exception("[scheduler] Scheduled scan failed: %s", e)
        return False
    finally:
        try:
            reset_scan_options(token)
        except Exception:
            pass
        try:
            await client.delete("watchdog:scan:progress")
        except Exception:
            pass
        await release_lock()


async def _auto_create_jira_bugs(findings, investigations, diff):
    """Auto-create Jira bugs for new findings that meet the severity threshold."""
    from jenkins_watchdog.api.jira import create_bug, CreateBugRequest, _jira_configured

    if not _jira_configured():
        logger.debug("[scheduler] Jira not configured — skipping auto-bug creation")
        return

    threshold_order = {"critical": 0, "warning": 1, "low": 2}
    threshold = threshold_order.get(settings.auto_jira_severity_threshold, 1)

    for finding in diff.new:
        finding_severity = threshold_order.get(finding.severity, 2)
        if finding_severity > threshold:
            continue

        if finding.category == "jenkins_agent" and "offline" in finding.symptom.lower():
            if "k3s" not in finding.resource.lower():
                continue

        if finding.context.get("jira_issue"):
            continue

        inv = investigations.get(finding.fingerprint)

        if inv:
            description = (
                f"Resource: {finding.resource}\n"
                f"Symptom: {finding.symptom}\n"
                f"Severity: {finding.severity}\n\n"
                f"Root Cause: {inv.root_cause}\n\n"
                f"Evidence:\n" + "\n".join(f"- {e}" for e in (inv.evidence or [])) + "\n\n"
                f"Impact: {inv.impact}\n\n"
                f"Suggested Fix: {inv.suggested_fix}\n"
            )
            if inv.fix_location:
                description += f"\nLocation: {inv.fix_location}"
        else:
            description = (
                f"Resource: {finding.resource}\n"
                f"Symptom: {finding.symptom}\n"
                f"Severity: {finding.severity}\n"
                f"Category: {finding.category}\n"
            )

        summary = f"[{finding.severity.upper()}] {finding.symptom}"
        if len(summary) > 255:
            summary = summary[:252] + "..."

        req = CreateBugRequest(
            project_key=settings.auto_jira_project,
            issue_type="Task",
            summary=summary,
            description=description,
            assignee_email=settings.auto_jira_assignee_email or None,
            finding_fingerprint=finding.fingerprint,
        )

        try:
            result = await create_bug(req)
            if hasattr(result, "key"):
                logger.info("[scheduler] Auto-created Jira %s for %s", result.key, finding.resource)
            else:
                logger.warning("[scheduler] Jira auto-create returned unexpected: %s", result)
        except Exception as e:
            logger.warning("[scheduler] Failed to auto-create Jira for %s: %s", finding.resource, e)


def start_scheduler():
    global _scheduler_task
    if not settings.scheduler_enabled:
        logger.info("[scheduler] Scheduler disabled (set WATCHDOG_SCHEDULER_ENABLED=true to enable)")
        return
    if _scheduler_task and not _scheduler_task.done():
        return
    _scheduler_task = asyncio.create_task(_scheduler_loop())
    logger.info(
        "[scheduler] Started: regular every %dm, deep every %dm, auto_jira=%s",
        settings.scheduler_scan_interval_minutes,
        settings.scheduler_deep_scan_interval_minutes,
        settings.auto_jira_enabled,
    )


def stop_scheduler():
    global _scheduler_task
    if _scheduler_task and not _scheduler_task.done():
        _scheduler_task.cancel()
        _scheduler_task = None
