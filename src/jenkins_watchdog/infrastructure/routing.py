"""Strict YAML loader for versioned automation routing."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from jenkins_watchdog.domain.routing import JobRoute, RoutingConfig, TeamRoute


class InvalidRoutingConfig(ValueError):
    pass


def load_routing_config(path: str | Path) -> RoutingConfig:
    config_path = Path(path)
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise InvalidRoutingConfig(f"cannot load routing config {config_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise InvalidRoutingConfig("routing config must be a mapping")
    _only_keys(raw, {"version", "teams", "routes", "global_fallback_recipients"}, "root")
    if raw.get("version") != 1:
        raise InvalidRoutingConfig("routing config version must be 1")

    teams_raw = raw.get("teams", {})
    if not isinstance(teams_raw, dict):
        raise InvalidRoutingConfig("teams must be a mapping")
    teams: list[TeamRoute] = []
    for name, value in teams_raw.items():
        if not isinstance(name, str) or not isinstance(value, dict):
            raise InvalidRoutingConfig("each team must be a named mapping")
        _only_keys(value, {"recipients"}, f"team {name}")
        teams.append(TeamRoute(name=name, recipients=_emails(value.get("recipients", []), f"team {name}")))

    routes_raw = raw.get("routes", [])
    if not isinstance(routes_raw, list):
        raise InvalidRoutingConfig("routes must be a list")
    routes: list[JobRoute] = []
    route_ids: set[str] = set()
    for index, value in enumerate(routes_raw):
        if not isinstance(value, dict):
            raise InvalidRoutingConfig(f"route {index} must be a mapping")
        _only_keys(
            value,
            {"id", "team", "jenkins_job_regexes", "provider", "repository", "mr_number_capture", "recipients"},
            f"route {index}",
        )
        required = {"id", "team", "jenkins_job_regexes", "provider", "repository", "mr_number_capture"}
        if not required.issubset(value):
            raise InvalidRoutingConfig(f"route {index} is missing required fields")
        route_id = _nonempty(value["id"], f"route {index} id")
        if route_id in route_ids:
            raise InvalidRoutingConfig(f"duplicate route id {route_id}")
        route_ids.add(route_id)
        team = _nonempty(value["team"], f"route {route_id} team")
        if team not in {item.name for item in teams}:
            raise InvalidRoutingConfig(f"route {route_id} references unknown team {team}")
        patterns = value["jenkins_job_regexes"]
        if not isinstance(patterns, list) or not patterns:
            raise InvalidRoutingConfig(f"route {route_id} requires Jenkins job regexes")
        capture = _nonempty(value["mr_number_capture"], f"route {route_id} capture")
        compiled: list[str] = []
        for pattern in patterns:
            pattern = _nonempty(pattern, f"route {route_id} regex")
            try:
                regex = re.compile(pattern)
            except re.error as exc:
                raise InvalidRoutingConfig(f"route {route_id} has invalid regex: {exc}") from exc
            if capture not in regex.groupindex and not capture.isdigit():
                raise InvalidRoutingConfig(f"route {route_id} capture {capture!r} is not defined")
            compiled.append(pattern)
        provider = _nonempty(value["provider"], f"route {route_id} provider").lower()
        if provider not in {"github", "gitlab"}:
            raise InvalidRoutingConfig(f"route {route_id} provider must be github or gitlab")
        routes.append(
            JobRoute(
                id=route_id,
                team=team,
                jenkins_job_regexes=tuple(compiled),
                provider=provider,
                repository=_nonempty(value["repository"], f"route {route_id} repository"),
                mr_number_capture=capture,
                recipients=_emails(value.get("recipients", []), f"route {route_id}"),
            )
        )

    return RoutingConfig(
        version=1,
        teams=tuple(teams),
        routes=tuple(routes),
        global_fallback_recipients=_emails(raw.get("global_fallback_recipients", []), "global fallback"),
    )


def _only_keys(value: dict[str, Any], expected: set[str], location: str) -> None:
    unknown = set(value) - expected
    if unknown:
        raise InvalidRoutingConfig(f"{location} contains unknown fields: {sorted(unknown)}")


def _nonempty(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidRoutingConfig(f"{location} must be a non-empty string")
    return value.strip()


def _emails(value: Any, location: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise InvalidRoutingConfig(f"{location} recipients must be a list")
    result: list[str] = []
    for item in value:
        email = _nonempty(item, f"{location} recipient")
        if "@" not in email or email.startswith("@") or email.endswith("@"):
            raise InvalidRoutingConfig(f"{location} has invalid recipient {email!r}")
        result.append(email)
    return tuple(dict.fromkeys(result))
