"""Dependency-free v2 domain model and policies."""

from jenkins_watchdog.domain.model import (
    CheckExecution,
    CheckResult,
    FindingObservation,
    Incident,
    IncidentOccurrence,
    IncidentStatus,
    Scan,
    ScanMode,
    ScanStatus,
    Severity,
)
from jenkins_watchdog.domain.policies import CorrelationDecision, correlate_observation

__all__ = [
    "CheckExecution",
    "CheckResult",
    "CorrelationDecision",
    "FindingObservation",
    "Incident",
    "IncidentOccurrence",
    "IncidentStatus",
    "Scan",
    "ScanMode",
    "ScanStatus",
    "Severity",
    "correlate_observation",
]
