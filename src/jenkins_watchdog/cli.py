"""Headless CLI scan runner for CronJob-based scheduled scans."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
import uuid
from dataclasses import replace

from jenkins_watchdog.api.router import (
    correlate_findings,
    deduplicate_findings,
    priority_score,
)
from jenkins_watchdog.checks.base import Finding
from jenkins_watchdog.checks.registry import run_all_checks
from jenkins_watchdog.clients.jenkins import get_version
from jenkins_watchdog.clients.k8s import get_core_v1, run_sync
from jenkins_watchdog.clients.prometheus import query_instant
from jenkins_watchdog.clients.valkey import get_valkey_client
from jenkins_watchdog.config import settings
from jenkins_watchdog.reasoning.context import gather_cluster_context
from jenkins_watchdog.reasoning.engine import investigate_finding
from jenkins_watchdog.reasoning.gate import should_investigate
from jenkins_watchdog.reasoning.triage import triage_findings
from jenkins_watchdog.scan_options import ScanOptions, activate_scan_options, reset_scan_options
from jenkins_watchdog.state import (
    compute_diff,
    dismiss_fingerprint_with_details,
    get_dismissed_fingerprints,
    get_previous_findings,
    get_stored_investigations,
    store_investigations,
    store_run_result,
)

logger = logging.getLogger(__name__)

_SEVERITY_ORDER = {"critical": 0, "warning": 1, "low": 2}


def main() -> None:
    parser = argparse.ArgumentParser(description="Jenkins Watchdog CLI")
    parser.add_argument("mode", choices=["dry-run", "quick", "normal", "deep"])
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--max-investigations", type=int, default=None)
    args = parser.parse_args()

    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    exit_code = asyncio.run(_run(args))
    sys.exit(exit_code)


async def _run(args: argparse.Namespace) -> int:
    if args.mode == "dry-run":
        return await _dry_run(args)
    if args.mode == "quick":
        return await _quick_scan(args)
    return await _full_scan(args)


async def _dry_run(args: argparse.Namespace) -> int:
    results: dict[str, dict] = {}

    try:
        await run_sync(get_core_v1().list_namespace)
        results["k8s"] = {"status": "OK"}
    except Exception as e:
        results["k8s"] = {"status": "FAILED", "error": str(e)}

    try:
        version = await get_version()
        results["jenkins"] = {"status": "OK", "version": version}
    except Exception as e:
        results["jenkins"] = {"status": "FAILED", "error": str(e)}

    if settings.prometheus_enabled:
        try:
            await query_instant("up")
            results["prometheus"] = {"status": "OK"}
        except Exception as e:
            results["prometheus"] = {"status": "FAILED", "error": str(e)}
    else:
        results["prometheus"] = {"status": "SKIPPED", "reason": "disabled"}

    try:
        client = await get_valkey_client()
        await client.ping()
        results["valkey"] = {"status": "OK"}
    except Exception as e:
        results["valkey"] = {"status": "FAILED", "error": str(e)}

    failed = [name for name, info in results.items() if info["status"] == "FAILED"]

    if args.json:
        print(json.dumps({"mode": "dry-run", "checks": results, "failed": failed}, indent=2))
    else:
        print("=== Jenkins Watchdog Connectivity Check (dry-run) ===")
        for name, info in results.items():
            if info["status"] == "OK":
                extra = f" ({info['version']})" if name == "jenkins" and "version" in info else ""
                print(f"  {name}: OK{extra}")
            elif info["status"] == "SKIPPED":
                print(f"  {name}: SKIPPED ({info.get('reason', '')})")
            else:
                print(f"  {name}: FAILED — {info.get('error', 'unknown error')}")
        if failed:
            print(f"\n{len(failed)} check(s) failed: {', '.join(failed)}")
        else:
            print("\nAll connectivity checks passed.")

    return 1 if failed else 0


async def _collect_findings() -> list[Finding]:
    findings = await run_all_checks()
    try:
        dismissed = await get_dismissed_fingerprints()
        findings = [f for f in findings if f.fingerprint not in dismissed]
    except Exception as e:
        logger.warning("Valkey unavailable, skipping dismissed filter: %s", e)
    return correlate_findings(deduplicate_findings(findings))


def _count_by_severity(findings: list[Finding]) -> dict[str, int]:
    counts = {"critical": 0, "warning": 0, "low": 0}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    return counts


def _sort_findings(findings: list[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: (_SEVERITY_ORDER.get(f.severity, 99), f.resource))


def _print_findings(mode: str, findings: list[Finding], duration_s: float) -> None:
    counts = _count_by_severity(findings)
    print(f"=== Jenkins Watchdog Scan ({mode}) ===")
    print(f"Completed in {duration_s:.1f}s\n")
    print(
        f"Found {len(findings)} findings "
        f"({counts['critical']} critical, {counts['warning']} warning, {counts['low']} low)\n"
    )
    for f in _sort_findings(findings):
        print(f"[{f.severity.upper()}]  {f.resource}: {f.symptom}")


def _findings_json_payload(mode: str, findings: list[Finding], duration_s: float, **extra) -> dict:
    counts = _count_by_severity(findings)
    payload = {
        "mode": mode,
        "duration_s": round(duration_s, 1),
        "findings_count": len(findings),
        "critical_count": counts["critical"],
        "warning_count": counts["warning"],
        "low_count": counts["low"],
        "findings": [f.to_dict() for f in findings],
    }
    payload.update(extra)
    return payload


async def _quick_scan(args: argparse.Namespace) -> int:
    started = time.monotonic()
    findings = await _collect_findings()
    duration_s = time.monotonic() - started
    counts = _count_by_severity(findings)

    if args.json:
        print(json.dumps(_findings_json_payload(args.mode, findings, duration_s), indent=2, default=str))
    else:
        _print_findings(args.mode, findings, duration_s)

    return 1 if counts["critical"] > 0 else 0


async def _safe_get_previous_findings() -> list[dict]:
    try:
        return await get_previous_findings()
    except Exception as e:
        logger.warning("Valkey unavailable, treating all findings as new: %s", e)
        return []


async def _safe_get_stored_investigations() -> dict:
    try:
        return await get_stored_investigations()
    except Exception as e:
        logger.warning("Valkey unavailable, no stored investigations: %s", e)
        return {}


def _build_investigation_plan(
    findings: list[Finding],
    diff,
    existing_investigations: dict,
    *,
    deep: bool,
    max_investigations: int,
) -> list[Finding]:
    if deep:
        to_investigate = [f for f in findings if f.severity in ("critical", "warning")]
    else:
        to_investigate = [f for f in diff.new if f.severity in ("critical", "warning")]
        to_investigate += [f for f in diff.ongoing if f.severity == "critical"]
        to_investigate += [
            f
            for f in findings
            if f.category == "jenkins_pipeline_pattern"
            and f.context.get("pattern") in ("consecutive_failures", "shared_failure_signature", "regression")
            and f not in to_investigate
        ]

    to_investigate = [
        f for f in to_investigate if should_investigate(f, diff, existing_investigations, deep=deep)
    ]
    to_investigate.sort(key=priority_score, reverse=True)
    return to_investigate[:max_investigations]


async def _full_scan(args: argparse.Namespace) -> int:
    deep = args.mode == "deep"
    scan_opts = ScanOptions.deep_scan() if deep else ScanOptions.from_settings()
    if args.max_investigations is not None:
        scan_opts = replace(scan_opts, max_investigations_per_scan=args.max_investigations)

    token = activate_scan_options(scan_opts)
    started = time.monotonic()
    scan_id = str(uuid.uuid4())[:8]
    total_prompt_tokens = 0
    total_completion_tokens = 0

    try:
        findings = await _collect_findings()
        cluster_context = await gather_cluster_context()

        if findings and not deep:
            triage_result = await triage_findings(findings, cluster_context=cluster_context)
            total_prompt_tokens += triage_result.prompt_tokens
            total_completion_tokens += triage_result.completion_tokens

            dismissed_fps = {d.finding.fingerprint for d in triage_result.dismissed}
            findings = [f for f in findings if f.fingerprint not in dismissed_fps]

            for d in triage_result.dismissed:
                try:
                    await dismiss_fingerprint_with_details(
                        d.finding.fingerprint,
                        reason=d.reason,
                        auto=True,
                        symptom=d.finding.symptom,
                    )
                except Exception as e:
                    logger.warning("Valkey unavailable, could not persist triage dismissal: %s", e)

        previous = await _safe_get_previous_findings()
        diff = compute_diff(previous, findings)
        existing_investigations = await _safe_get_stored_investigations()

        to_investigate = _build_investigation_plan(
            findings,
            diff,
            existing_investigations,
            deep=deep,
            max_investigations=scan_opts.max_investigations_per_scan,
        )

        logger.info(
            "[scan:%s] Investigation plan: deep=%s count=%d max=%d",
            scan_id,
            deep,
            len(to_investigate),
            scan_opts.max_investigations_per_scan,
        )

        investigations = {}
        for finding in to_investigate:
            try:
                result = await investigate_finding(
                    finding,
                    cluster_context=cluster_context,
                    all_findings=findings,
                )
                if result:
                    investigations[finding.fingerprint] = result
                    total_prompt_tokens += result.prompt_tokens
                    total_completion_tokens += result.completion_tokens
            except Exception:
                logger.exception("[scan:%s] Investigation failed for %s", scan_id, finding.resource)

        duration_s = time.monotonic() - started
        total_cost = sum(inv.estimated_cost_usd for inv in investigations.values())
        token_usage = {
            "prompt_tokens": total_prompt_tokens,
            "completion_tokens": total_completion_tokens,
            "estimated_cost_usd": round(total_cost, 4),
        }

        try:
            await store_run_result(findings, scan_id, duration_s, token_usage, diff=diff, deep=deep)
            await store_investigations(investigations)
        except Exception as e:
            logger.warning("Valkey unavailable, results not persisted: %s", e)

        counts = _count_by_severity(findings)

        if args.json:
            payload = _findings_json_payload(
                args.mode,
                findings,
                duration_s,
                scan_id=scan_id,
                new_findings=diff.new_count,
                investigations_performed=len(investigations),
                token_usage=token_usage,
                investigations={
                    fp: inv.model_dump() for fp, inv in investigations.items()
                },
            )
            print(json.dumps(payload, indent=2, default=str))
        else:
            _print_findings(args.mode, findings, duration_s)
            if to_investigate:
                print(f"\nInvestigated {len(investigations)}/{len(to_investigate)} finding(s)\n")
            for finding in to_investigate:
                inv = investigations.get(finding.fingerprint)
                if not inv:
                    continue
                print(f"[INVESTIGATION] {finding.resource}")
                print(f"  Root cause: {inv.root_cause[:300]}")
                print(f"  Confidence: {inv.confidence}")
                if inv.suggested_fix:
                    print(f"  Fix: {inv.suggested_fix[:300]}")
                print()

        return 1 if counts["critical"] > 0 else 0
    finally:
        reset_scan_options(token)
