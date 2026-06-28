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

_NOISE_PATTERNS = re.compile(
    r"(?:"
    r"^\s*\[Pipeline\]\s*$|"
    r"^\s*---\s*$|"
    r"^\s*\+\s|"
    r"Downloading|Progress \(|"
    r"^\[INFO\].*Downloading|"
    r"Finished:|SUCCESS \["
    r")",
    re.IGNORECASE,
)

_DYNAMIC = re.compile(r"\b\d+\b|\b[0-9a-f]{8,}\b|\b\d+\.\d+\.\d+\b", re.IGNORECASE)


def extract_error_lines(console: str, *, max_lines: int = 25, tail_chars: int = 50000) -> list[str]:
    """Extract the most relevant error lines from build console output."""
    if not console:
        return []

    text = console[-tail_chars:] if len(console) > tail_chars else console
    lines = text.splitlines()
    candidates: list[tuple[int, str]] = []

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or len(stripped) < 5:
            continue
        if _NOISE_PATTERNS.search(stripped):
            continue
        if _ERROR_INDICATORS.search(stripped):
            candidates.append((i, stripped))

    if not candidates and lines:
        # Fall back to last non-empty lines
        for line in reversed(lines):
            stripped = line.strip()
            if stripped and not _NOISE_PATTERNS.search(stripped):
                candidates.append((len(lines), stripped))
                if len(candidates) >= max_lines:
                    break
        candidates.reverse()

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
    if any(k in text for k in ("compilation failure", "compile error", "syntax error", "cannot find symbol")):
        return "compilation_error"
    if any(k in text for k in ("tests failed", "test failure", "assertion", "pytest", "junit")):
        return "test_failure"
    if any(k in text for k in ("connection refused", "timeout", "no route", "unreachable", "503", "502")):
        return "infrastructure"
    if any(k in text for k in ("permission denied", "not found", "invalid parameter", "missing required")):
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
        n = _DYNAMIC.sub("N", line.lower().strip())
        n = re.sub(r"\s+", " ", n)[:200]
        normalized.append(n)

    raw = "|".join(normalized)
    return hashlib.sha256(raw.encode()).hexdigest()[:12]
