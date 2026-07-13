"""Deterministic source and recipient routing policies."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from jenkins_watchdog.domain.routing import JobRoute, RoutingConfig


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    source: dict[str, Any]
    recipients: tuple[str, ...]
    route_id: str | None


def resolve_routing(
    *,
    config: RoutingConfig,
    incident_source: Mapping[str, Any],
    job_name: str | None,
    triggering_user_email: str | None,
) -> RoutingDecision:
    route, route_source = _match_route(config, job_name)
    source = _resolve_source(incident_source, route_source)
    recipients = _recipients(config, route, triggering_user_email)
    return RoutingDecision(source=source, recipients=recipients, route_id=route.id if route else None)


def _match_route(config: RoutingConfig, job_name: str | None) -> tuple[JobRoute | None, dict[str, Any] | None]:
    if not job_name:
        return None, None
    for route in config.routes:
        for pattern in route.jenkins_job_regexes:
            match = re.search(pattern, job_name)
            if not match:
                continue
            try:
                change_number = match.group(route.mr_number_capture)
            except (IndexError, KeyError):
                return route, None
            if not change_number:
                return route, None
            return route, {
                "kind": "merge_request",
                "confirmed": True,
                "provider": route.provider,
                "repository": route.repository,
                "change_number": str(change_number),
                "job_name": job_name,
            }
    return None, None


def _resolve_source(metadata: Mapping[str, Any], route_source: Mapping[str, Any] | None) -> dict[str, Any]:
    if metadata.get("reason") in {
        "partial_scm_metadata",
        "conflicting_source_metadata",
        "unsupported_scm_provider",
    }:
        return {"kind": "unknown", "confirmed": False, "reason": str(metadata["reason"])}
    if metadata.get("confirmed"):
        return dict(metadata)
    if route_source:
        return dict(route_source)
    return dict(metadata) if metadata else {"kind": "unknown", "confirmed": False}


def _recipients(config: RoutingConfig, route: JobRoute | None, triggering_user_email: str | None) -> tuple[str, ...]:
    if route and route.recipients:
        return route.recipients
    if route:
        team = config.team(route.team)
        if team and team.recipients:
            return team.recipients
    if triggering_user_email:
        return (triggering_user_email,)
    return config.global_fallback_recipients
