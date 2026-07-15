"""Build console log analysis — extract errors and signatures for correlation."""

import hashlib
import re

# Lines that often indicate the actual failure (not noise)
_ERROR_INDICATORS = re.compile(
    r"(?:"
    r"error|exception|failed|failure|fatal|assertion|traceback|"
    r"BUILD FAILED|Tests failed|npm ERR!|maven.*FAILURE|"
    r"Compilation failure|NonZeroExitCode|exit code [1-9]|"
    r"OOMKilled|Killed|No such file|permission denied|timeout"
    r")",
    re.IGNORECASE,
)
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

_NOISE_PATTERNS = re.compile(
    r"(?:"
    r"^\s*\[Pipeline\](?:\s|$)|"
    r"^\s*---\s*$|"
    r"^(?:\[\d{4}-\d{2}-\d{2}T[^\]]+\]\s*)?\+\s|"
    r"Downloading|Progress \(|"
    r"^\[INFO\].*Downloading|"
    r"Finished:|SUCCESS \[|"
    r"^> git |# timeout=\d+|"
    r"ErrorAction\$ErrorId|"
    r"Timeout set to expire in|"
    r"skipped due to earlier failure|"
    r"Email was triggered for:|Sending (?:an )?email|"
    r"Build returned error, collecting|"
    r"Build step ['\"]?.*['\"]? marked build as failure|"
    r"No test report files were found\. Configuration error\?|"
    r"Seen branch in repository|"
    r"^\*\s+\[new branch\].*\s+->\s+|"
    r"^(?:\[[^\]]+\]\s*)?-rw[rwx.-]+\s+|"
    r"^(?:\[[^\]]+\]\s*)?<div\b|"
    r"Test Passed:|"
    r"^goto error$|^echo Error|"
    r"YOU FURTHER ACKNOWLEDGE THAT THE APPLE SOFTWARE|"
    r"IN NO EVENT WILL APPLE BE LIABLE|"
    r"court of competent jurisdiction finds any clause|"
    r"litigation or other dispute resolution between You and Apple"
    r")",
    re.IGNORECASE,
)

_DYNAMIC = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b|"
    r"\b[0-9a-f]{8,}\b|"
    r"\b\d+\.\d+\.\d+\b|"
    r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?|"
    r"\b\d+\b",
    re.IGNORECASE,
)


def extract_error_lines(console: str, *, max_lines: int = 25, tail_chars: int = 50000) -> list[str]:
    """Extract the most relevant error lines from build console output."""
    if not console:
        return []

    text = console[-tail_chars:] if len(console) > tail_chars else console
    lines = text.splitlines()
    candidates: list[tuple[int, str]] = []

    for i, line in enumerate(lines):
        stripped = _ANSI_ESCAPE.sub("", line).strip()
        if not stripped or len(stripped) < 5:
            continue
        if _NOISE_PATTERNS.search(stripped):
            continue
        if _ERROR_INDICATORS.search(stripped):
            candidates.append((i, stripped))

    # Prefer lines near the end; dedupe while preserving order
    seen: set[str] = set()
    result: list[str] = []
    for _idx, line in sorted(candidates, key=lambda x: x[0], reverse=True):
        key = line[:120]
        if key in seen:
            continue
        seen.add(key)
        result.append(line[:500])
        if len(result) >= max_lines:
            break

    result.reverse()
    return result


def classify_failure(error_lines: list[str]) -> str:
    """Heuristic failure category from extracted error lines."""
    text = " ".join(error_lines).lower()
    if any(k in text for k in ("oomkilled", "outofmemory", "cannot allocate memory", "heap space")):
        return "resource_exhaustion"
    if any(
        k in text
        for k in (
            "compilation failure",
            "compile error",
            "syntax error",
            "cannot find symbol",
            "fatal error",
            "make: ***",
            "rpmbuild returned error",
            "bad exit status",
            "multiplecompilationerrorsexception",
        )
    ):
        return "compilation_error"
    if any(k in text for k in ("does not merge cleanly", "automatic merge failed", "merge conflict")):
        return "scm_conflict"
    if any(
        k in text
        for k in (
            "connection refused",
            "connection reset",
            "broken pipe",
            "unknownhost",
            "eofexception",
            "timeout",
            "no route",
            "unreachable",
            "503",
            "502",
        )
    ):
        return "infrastructure"
    if any(
        k in text
        for k in (
            "tests failed",
            "test failure",
            "assertion",
            "pytest",
            "junit",
            "total tests run",
            "configuration failures",
            "suite ",
            "failures:",
            "no test report files",
        )
    ):
        return "test_failure"
    if any(
        k in text
        for k in (
            "permission denied",
            "not found",
            "invalid parameter",
            "missing required",
            "requires ",
            "no such property",
            "couldn't find any revision",
            "no open merge requests",
        )
    ):
        return "configuration"
    if any(k in text for k in ("docker", "container", "imagepull", "registry")):
        return "container_runtime"
    return "unknown"


def error_signature(error_lines: list[str]) -> str:
    """Normalize error lines into a stable signature for cross-job correlation."""
    if not error_lines:
        return ""

    normalized: list[str] = []
    for line in error_lines[-5:]:
        n = re.sub(r"https?://\S+", "URL", line.lower().strip())
        n = _DYNAMIC.sub("N", n)
        n = re.sub(r"^\[N\]\s*", "", n)
        n = re.sub(r"quiet period for \S+ is N seconds", "quiet period for JOB is N seconds", n)
        n = re.sub(r"\s*#N\b", " #N", n)
        n = re.sub(r"\s+", " ", n)[:200]
        normalized.append(n)

    raw = "|".join(normalized)
    return hashlib.sha256(raw.encode()).hexdigest()[:12]
