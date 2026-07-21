"""Validated routing values used by automation planning."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TeamRoute:
    name: str
    recipients: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class JobRoute:
    id: str
    team: str
    jenkins_job_regexes: tuple[str, ...]
    provider: str
    repository: str
    mr_number_capture: str
    recipients: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RoutingConfig:
    version: int
    teams: tuple[TeamRoute, ...]
    routes: tuple[JobRoute, ...]
    global_fallback_recipients: tuple[str, ...]

    def team(self, name: str) -> TeamRoute | None:
        return next((team for team in self.teams if team.name == name), None)
