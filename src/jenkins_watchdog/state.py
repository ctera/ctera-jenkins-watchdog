"""Valkey state manager — findings persistence, dedup, baselines, investigations."""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from jenkins_watchdog.clients.valkey import get_valkey_client

logger = logging.getLogger(__name__)

LOCK_KEY = "watchdog:lock"
SCAN_CANCELLED_KEY = "watchdog:scan:cancelled"
LOCK_TTL = 300
LAST_RUN_KEY = "watchdog:last_run"
FINDINGS_KEY = "watchdog:findings:latest"
INVESTIGATIONS_KEY = "watchdog:investigations:latest"
INCIDENT_PREFIX = "watchdog:incidents:"
INCIDENT_TTL = 43200
HISTORY_KEY = "watchdog:history:scans"
MAX_HISTORY = 50


@dataclass
class FindingsDiff:
    new: list = field(default_factory=list)
    ongoing: list = field(default_factory=list)
    resolved: list = field(default_factory=list)

    @property
    def has_new_findings(self) -> bool:
        return len(self.new) > 0

    @property
    def new_count(self) -> int:
        return len(self.new)


async def acquire_lock() -> bool:
    client = await get_valkey_client()
    result = await client.set(LOCK_KEY, "locked", nx=True, ex=LOCK_TTL)
    return result is not None


async def refresh_lock() -> None:
    """Extend lock TTL during long-running scans to prevent expiry."""
    client = await get_valkey_client()
    await client.expire(LOCK_KEY, LOCK_TTL)


async def release_lock() -> None:
    client = await get_valkey_client()
    await client.delete(LOCK_KEY, SCAN_CANCELLED_KEY)


async def request_scan_cancel() -> None:
    """Signal that the current scan should stop."""
    client = await get_valkey_client()
    await client.set(SCAN_CANCELLED_KEY, "1", ex=LOCK_TTL)


async def is_scan_cancelled() -> bool:
    client = await get_valkey_client()
    return await client.get(SCAN_CANCELLED_KEY) is not None


async def clear_scan_cancel() -> None:
    client = await get_valkey_client()
    await client.delete(SCAN_CANCELLED_KEY)


async def get_previous_findings() -> list[dict]:
    client = await get_valkey_client()
    data = await client.get(FINDINGS_KEY)
    if data:
        return json.loads(data)
    return []


async def store_run_result(
    findings: list,
    scan_id: str = "",
    duration_s: float = 0.0,
    token_usage: dict | None = None,
    diff: "FindingsDiff | None" = None,
) -> None:
    client = await get_valkey_client()
    now = datetime.now(timezone.utc).isoformat()

    previous_raw = await client.get(FINDINGS_KEY)
    previous_by_fp: dict[str, dict] = {}
    if previous_raw:
        for pf in json.loads(previous_raw):
            fp = pf.get("fingerprint", "")
            if fp:
                previous_by_fp[fp] = pf

    enriched = []
    new_fps = {f.fingerprint for f in (diff.new if diff else [])}
    for f in findings:
        d = f.to_dict()
        d["status"] = "new" if f.fingerprint in new_fps else "ongoing"
        d["last_seen"] = now

        prev = previous_by_fp.get(f.fingerprint, {})
        if prev.get("jira_issue"):
            d["jira_issue"] = prev["jira_issue"]

        first_seen_key = f"watchdog:first_seen:{f.fingerprint}"
        existing_first = await client.get(first_seen_key)
        if existing_first:
            d["first_seen"] = existing_first
        else:
            d["first_seen"] = now
            await client.set(first_seen_key, now, ex=604800)

        enriched.append(d)
    serialized = json.dumps(enriched, default=str)
    await client.set(FINDINGS_KEY, serialized, ex=604800)

    now = datetime.now(timezone.utc)
    run_info = {
        "last_run": now.isoformat(),
        "findings_count": len(findings),
        "scan_id": scan_id,
        "duration_s": round(duration_s, 1),
    }
    if token_usage:
        run_info["token_usage"] = token_usage
    await client.set(LAST_RUN_KEY, json.dumps(run_info), ex=604800)

    for finding in findings:
        incident_key = f"{INCIDENT_PREFIX}{finding.fingerprint}"
        await client.set(incident_key, "1", ex=INCIDENT_TTL, nx=True)

    history_entry = json.dumps({
        "scan_id": scan_id,
        "timestamp": now.isoformat(),
        "findings_count": len(findings),
        "critical_count": len([f for f in findings if f.severity == "critical"]),
        "warning_count": len([f for f in findings if f.severity == "warning"]),
        "duration_s": round(duration_s, 1),
        "token_usage": token_usage or {},
    }, default=str)
    await client.lpush(HISTORY_KEY, history_entry)
    await client.ltrim(HISTORY_KEY, 0, MAX_HISTORY - 1)


async def get_scan_history(limit: int = 20) -> list[dict]:
    """Retrieve recent scan history entries."""
    client = await get_valkey_client()
    entries = await client.lrange(HISTORY_KEY, 0, limit - 1)
    result = []
    for entry in entries:
        try:
            result.append(json.loads(entry))
        except json.JSONDecodeError:
            continue
    return result


async def store_investigations(investigations: dict) -> None:
    """Merge new investigation results with existing ones (keyed by fingerprint)."""
    if not investigations:
        return
    client = await get_valkey_client()
    existing = {}
    raw = await client.get(INVESTIGATIONS_KEY)
    if raw:
        try:
            existing = json.loads(raw)
        except json.JSONDecodeError:
            pass
    merged = {**existing, **{fp: inv.model_dump() for fp, inv in investigations.items()}}
    await client.set(INVESTIGATIONS_KEY, json.dumps(merged, default=str), ex=604800)


async def get_stored_investigations() -> dict:
    """Retrieve stored investigation results."""
    client = await get_valkey_client()
    data = await client.get(INVESTIGATIONS_KEY)
    if data:
        return json.loads(data)
    return {}


async def get_last_run_info() -> dict:
    client = await get_valkey_client()
    data = await client.get(LAST_RUN_KEY)
    if data:
        return json.loads(data)
    return {}


def compute_diff(previous: list[dict], current: list) -> FindingsDiff:
    prev_fingerprints = {f.get("fingerprint") for f in previous if f.get("fingerprint")}
    curr_fingerprints = {f.fingerprint for f in current}

    new = [f for f in current if f.fingerprint not in prev_fingerprints]
    ongoing = [f for f in current if f.fingerprint in prev_fingerprints]
    resolved_fps = prev_fingerprints - curr_fingerprints
    resolved = [f for f in previous if f.get("fingerprint") in resolved_fps]

    return FindingsDiff(new=new, ongoing=ongoing, resolved=resolved)
