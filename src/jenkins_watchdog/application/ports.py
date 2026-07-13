"""Ports implemented by v2 infrastructure adapters."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Any, Protocol, Self

from jenkins_watchdog.application.types import CursorPage, EnqueueScan, ScanEvent
from jenkins_watchdog.domain.model import (
    Action,
    CheckResult,
    DeliveryAttempt,
    FindingObservation,
    Incident,
    Investigation,
    Scan,
    ScanMode,
)


class ScanRepository(Protocol):
    async def lock_enqueue(self) -> None: ...

    async def active(self) -> Scan | None: ...

    async def add(self, request: EnqueueScan) -> Scan: ...

    async def get(self, scan_id: str) -> Scan | None: ...

    async def list(self, *, limit: int, cursor: str | None = None) -> CursorPage: ...

    async def claim(self, *, owner: str, now: datetime, lease_seconds: int) -> Scan | None: ...

    async def heartbeat(self, scan_id: str, *, owner: str, now: datetime, lease_seconds: int) -> bool: ...

    async def request_cancel(self, scan_id: str, *, now: datetime) -> tuple[Scan, bool] | None: ...

    async def save(self, scan: Scan) -> None: ...


class CheckExecutionRepository(Protocol):
    async def get(self, scan_id: str, check_name: str) -> CheckResult | None: ...

    async def save(self, scan_id: str, result: CheckResult) -> None: ...


class FindingRepository(Protocol):
    async def add_observations(self, scan_id: str, observations: tuple[FindingObservation, ...]) -> None: ...

    async def unlinked_for_scan(self, scan_id: str) -> tuple[FindingObservation, ...]: ...


class IncidentRepository(Protocol):
    async def get(self, incident_id: str) -> Incident | None: ...

    async def get_by_correlation(self, rule_id: str, key: str) -> Incident | None: ...

    async def list(
        self,
        *,
        limit: int,
        cursor: str | None = None,
        status: str | None = None,
        severity: str | None = None,
        source_type: str | None = None,
    ) -> CursorPage: ...

    async def active(self) -> tuple[Incident, ...]: ...

    async def observed_ids_for_scan(self, scan_id: str) -> frozenset[str]: ...

    async def observations(self, incident_id: str) -> tuple[FindingObservation, ...]: ...

    async def save(self, incident: Incident) -> None: ...

    async def link_observation(self, incident: Incident, observation: FindingObservation) -> None: ...


class InvestigationRepository(Protocol):
    async def latest_for_incident(self, incident_id: str) -> Investigation | None: ...

    async def save(self, investigation: Investigation) -> None: ...


class ActionRepository(Protocol):
    async def get(self, action_id: str) -> Action | None: ...

    async def add(self, action: Action) -> Action: ...

    async def list(
        self,
        *,
        limit: int,
        cursor: str | None = None,
        status: str | None = None,
        action_type: str | None = None,
        incident_id: str | None = None,
    ) -> CursorPage: ...

    async def for_incident(self, incident_id: str) -> tuple[Action, ...]: ...

    async def claim(self, *, owner: str, now: datetime, lease_seconds: int) -> Action | None: ...

    async def heartbeat(self, action_id: str, *, owner: str, now: datetime, lease_seconds: int) -> bool: ...

    async def save(self, action: Action) -> None: ...


class DeliveryAttemptRepository(Protocol):
    async def save(self, attempt: DeliveryAttempt) -> None: ...

    async def for_action(self, action_id: str) -> tuple[DeliveryAttempt, ...]: ...


class EventRepository(Protocol):
    async def append(self, scan_id: str, event_type: str, payload: dict[str, Any], *, now: datetime) -> ScanEvent: ...

    async def after(self, scan_id: str, sequence: int, *, limit: int = 500) -> tuple[ScanEvent, ...]: ...


class UnitOfWork(AbstractAsyncContextManager["UnitOfWork"], Protocol):
    scans: ScanRepository
    checks: CheckExecutionRepository
    findings: FindingRepository
    incidents: IncidentRepository
    investigations: InvestigationRepository
    actions: ActionRepository
    delivery_attempts: DeliveryAttemptRepository
    events: EventRepository

    async def __aenter__(self) -> Self: ...

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


class UnitOfWorkFactory(Protocol):
    def __call__(self) -> UnitOfWork: ...


class CheckRunner(Protocol):
    @property
    def check_names(self) -> tuple[str, ...]: ...

    def checks_for_categories(self, categories: tuple[str, ...]) -> tuple[str, ...]: ...

    async def run(self, scan_id: str, check_name: str, mode: ScanMode) -> CheckResult: ...


class EventNotifier(Protocol):
    async def publish(self, event: ScanEvent) -> None: ...


class ReasoningPort(Protocol):
    async def triage(self, incident: Incident, observations: tuple[FindingObservation, ...]) -> dict[str, Any]: ...

    async def investigate(self, incident: Incident, observations: tuple[FindingObservation, ...]) -> Investigation: ...

    async def chat(self, *, message: str, incident: Incident | None = None) -> str: ...


class ActionDeliveryPort(Protocol):
    async def deliver(self, action: Action) -> dict[str, Any]: ...


class PayloadRenderer(Protocol):
    template_version: str

    def render(self, action_type: str, context: dict[str, Any]) -> dict[str, Any]: ...
