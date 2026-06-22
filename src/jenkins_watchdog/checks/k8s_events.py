"""Kubernetes cluster event checks — surface Warning events as findings."""

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from jenkins_watchdog.checks.base import Finding
from jenkins_watchdog.clients.k8s import get_core_v1, run_sync
from jenkins_watchdog.config import settings

logger = logging.getLogger(__name__)

WATCHDOG_NAMESPACE = "jenkins-watchdog"

MEANINGFUL_REASONS = frozenset(
    {
        "Unhealthy",
        "BackOff",
        "Failed",
        "Evicted",
        "OOMKilling",
        "FailedCreate",
        "FailedScheduling",
    }
)

CRITICAL_REASONS = frozenset({"OOMKilling", "Evicted"})


def _event_timestamp(ev) -> datetime:
    ts = ev.last_timestamp or ev.event_time or ev.metadata.creation_timestamp
    if ts is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts


def _group_key(ev) -> tuple[str, str, str, str]:
    obj = ev.involved_object
    return (
        obj.namespace or ev.metadata.namespace or "",
        obj.kind or "Unknown",
        obj.name or "unknown",
        ev.reason or "Unknown",
    )


class K8sEventsCheck:
    name = "k8s_events"

    async def run(self) -> list[Finding]:
        findings: list[Finding] = []
        v1 = get_core_v1()
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=settings.k8s_events_window_minutes)
        namespaces = {settings.jenkins_namespace, WATCHDOG_NAMESPACE}

        grouped: dict[tuple[str, str, str, str], list] = defaultdict(list)

        for ns in sorted(namespaces):
            try:
                result = await run_sync(
                    v1.list_namespaced_event,
                    ns,
                    field_selector="type=Warning",
                    timeout_seconds=15,
                )
            except Exception as exc:
                logger.warning("Failed to list events in namespace %s: %s", ns, exc)
                continue

            for ev in result.items:
                if ev.reason not in MEANINGFUL_REASONS:
                    continue
                if _event_timestamp(ev) < cutoff:
                    continue
                grouped[_group_key(ev)].append(ev)

        for (ns, kind, name, reason), events in grouped.items():
            events.sort(key=_event_timestamp)
            first = _event_timestamp(events[0])
            last = _event_timestamp(events[-1])
            total_count = sum(ev.count or 1 for ev in events)
            latest = events[-1]

            resource = f"{ns}/{kind}/{name}" if ns else f"{kind}/{name}"
            severity = "critical" if reason in CRITICAL_REASONS else "warning"

            findings.append(
                Finding(
                    severity=severity,
                    category="k8s_event",
                    resource=resource,
                    symptom=f"{reason}: {latest.message or 'no message'}",
                    context={
                        "reason": reason,
                        "message": latest.message or "",
                        "count": total_count,
                        "first_seen": first.isoformat(),
                        "last_seen": last.isoformat(),
                        "involved_object": {"namespace": ns, "kind": kind, "name": name},
                        "source": {
                            "component": latest.source.component if latest.source else "",
                            "host": latest.source.host if latest.source else "",
                        },
                    },
                )
            )

        return findings
