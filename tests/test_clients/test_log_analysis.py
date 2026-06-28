"""Tests for build log analysis."""

from jenkins_watchdog.clients.log_analysis import classify_failure, error_signature, extract_error_lines


def test_extract_error_lines_finds_failures():
    console = """
[INFO] Starting build
[INFO] Running tests
Error: Connection refused to localhost:8081
Tests failed: 3 failures
BUILD FAILED
"""
    lines = extract_error_lines(console)
    assert any("Connection refused" in line for line in lines)
    assert any("BUILD FAILED" in line or "Tests failed" in line for line in lines)


def test_classify_failure_test():
    lines = ["Tests failed: assertion error in TestFoo", "BUILD FAILED"]
    assert classify_failure(lines) == "test_failure"


def test_classify_failure_compilation():
    lines = ["Compilation failure: cannot find symbol", "BUILD FAILED"]
    assert classify_failure(lines) == "compilation_error"


def test_classify_failure_infrastructure():
    lines = ["Connection refused: connect", "curl: (7) Failed to connect"]
    assert classify_failure(lines) == "infrastructure"


def test_error_signature_stable():
    lines = ["Error: same failure message here"]
    sig1 = error_signature(lines)
    sig2 = error_signature(lines)
    assert sig1 == sig2
    assert len(sig1) == 12
