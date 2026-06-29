"""Per-scan configuration overrides (regular vs deep scan)."""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass

from jenkins_watchdog.config import settings


@dataclass(frozen=True)
class ScanOptions:
    deep: bool = False
    max_investigations_per_scan: int = 12
    max_tool_rounds: int = 15
    jenkins_failed_build_window_hours: int = 4
    jenkins_build_depth: int = 10
    full_build_logs: bool = False
    consecutive_failure_threshold: int = 3
    pipeline_history_limit: int = 15

    @classmethod
    def from_settings(cls) -> ScanOptions:
        return cls(
            max_investigations_per_scan=settings.max_investigations_per_scan,
            max_tool_rounds=settings.max_tool_rounds,
            jenkins_failed_build_window_hours=settings.jenkins_failed_build_window_hours,
        )

    @classmethod
    def deep_scan(cls) -> ScanOptions:
        return cls(
            deep=True,
            max_investigations_per_scan=50,
            max_tool_rounds=25,
            jenkins_failed_build_window_hours=24,
            jenkins_build_depth=50,
            full_build_logs=True,
            consecutive_failure_threshold=2,
            pipeline_history_limit=50,
        )


_scan_options: ContextVar[ScanOptions] = ContextVar(
    "scan_options",
    default=ScanOptions.from_settings(),
)


def get_scan_options() -> ScanOptions:
    return _scan_options.get()


def activate_scan_options(options: ScanOptions) -> Token:
    return _scan_options.set(options)


def reset_scan_options(token: Token) -> None:
    _scan_options.reset(token)
