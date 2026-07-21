"""Strict loader for versioned Jenkins source-attribution profiles."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from jenkins_watchdog.domain.source import SourceProfile, SourceProfileRegistry


class InvalidSourceProfileConfig(ValueError):
    pass


def load_source_profiles(path: str | Path) -> SourceProfileRegistry:
    config_path = Path(path)
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise InvalidSourceProfileConfig(f"cannot load source profiles {config_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise InvalidSourceProfileConfig("source profile config must be a mapping")
    _only_keys(raw, {"version", "profiles"}, "root")
    if raw.get("version") != 1:
        raise InvalidSourceProfileConfig("source profile config version must be 1")
    values = raw.get("profiles", [])
    if not isinstance(values, list):
        raise InvalidSourceProfileConfig("profiles must be a list")

    profiles: list[SourceProfile] = []
    profile_ids: set[str] = set()
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            raise InvalidSourceProfileConfig(f"profile {index} must be a mapping")
        _only_keys(
            value,
            {
                "id",
                "root_job_regexes",
                "provider",
                "primary_repository",
                "allowed_repositories",
                "allow_mr_comments",
            },
            f"profile {index}",
        )
        required = {"id", "root_job_regexes", "provider"}
        if not required.issubset(value):
            raise InvalidSourceProfileConfig(f"profile {index} is missing required fields")
        profile_id = _nonempty(value["id"], f"profile {index} id")
        if profile_id in profile_ids:
            raise InvalidSourceProfileConfig(f"duplicate source profile id {profile_id}")
        profile_ids.add(profile_id)
        patterns = _strings(value["root_job_regexes"], f"profile {profile_id} root_job_regexes")
        if not patterns:
            raise InvalidSourceProfileConfig(f"profile {profile_id} requires root_job_regexes")
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise InvalidSourceProfileConfig(
                    f"profile {profile_id} has invalid root job regex: {exc}"
                ) from exc
        provider = _nonempty(value["provider"], f"profile {profile_id} provider").lower()
        if provider not in {"github", "gitlab"}:
            raise InvalidSourceProfileConfig(f"profile {profile_id} provider must be github or gitlab")
        primary = value.get("primary_repository")
        if primary is not None:
            primary = _nonempty(primary, f"profile {profile_id} primary_repository")
        allowed = _strings(
            value.get("allowed_repositories", []),
            f"profile {profile_id} allowed_repositories",
        )
        if primary and allowed and primary not in allowed:
            raise InvalidSourceProfileConfig(
                f"profile {profile_id} primary_repository must be allowed"
            )
        comments = value.get("allow_mr_comments", False)
        if not isinstance(comments, bool):
            raise InvalidSourceProfileConfig(f"profile {profile_id} allow_mr_comments must be boolean")
        profiles.append(
            SourceProfile(
                id=profile_id,
                root_job_regexes=patterns,
                provider=provider,
                primary_repository=primary,
                allowed_repositories=allowed,
                allow_mr_comments=comments,
            )
        )
    return SourceProfileRegistry(version=1, profiles=tuple(profiles))


def _only_keys(value: dict[str, Any], expected: set[str], location: str) -> None:
    unknown = set(value) - expected
    if unknown:
        raise InvalidSourceProfileConfig(f"{location} contains unknown fields: {sorted(unknown)}")


def _nonempty(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidSourceProfileConfig(f"{location} must be a non-empty string")
    return value.strip()


def _strings(value: Any, location: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise InvalidSourceProfileConfig(f"{location} must be a list")
    return tuple(dict.fromkeys(_nonempty(item, location) for item in value))
