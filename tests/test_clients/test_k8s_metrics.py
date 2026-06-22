"""Tests for Kubernetes metrics API helpers."""

from jenkins_watchdog.clients.k8s_metrics import (
    format_bytes,
    format_cores,
    parse_cpu_quantity,
    parse_memory_quantity,
    usage_pct,
)


def test_parse_cpu_quantity():
    assert parse_cpu_quantity("500m") == 0.5
    assert parse_cpu_quantity("2") == 2.0
    assert parse_cpu_quantity("95333300n") == 0.0953333
    assert parse_cpu_quantity("") == 0.0


def test_parse_memory_quantity():
    assert parse_memory_quantity("256Mi") == 256 * 1024**2
    assert parse_memory_quantity("16Gi") == 16 * 1024**3
    assert parse_memory_quantity("5592220Ki") == 5592220 * 1024
    assert parse_memory_quantity("") == 0


def test_usage_pct():
    assert usage_pct(85, 100) == 85.0
    assert usage_pct(10, 0) is None


def test_format_helpers():
    assert format_cores(0.095) == "95m"
    assert format_cores(1.5) == "1.50"
    assert format_bytes(1024**3) == "1.0Gi"
