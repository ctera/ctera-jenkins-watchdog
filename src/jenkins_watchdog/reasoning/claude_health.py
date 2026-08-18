"""Is the agent credential actually usable?

Nothing passive can answer that. With an OAuth token the credential is only exercised
when a subprocess runs a turn, so the binary can be present, the config dir writable, the
pod Ready -- and every investigation still 401s.

``claude auth status`` is no help either: it reports success whenever the variable is
merely *set*, so it passes on a revoked token. Only a real call distinguishes them, and
catching a rotated credential before a scan does is the entire reason this exists.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass

from jenkins_watchdog.reasoning.claude_auth import (
    STATUS_FAILED,
    STATUS_OK,
    STATUS_UNCONFIGURED,
    ClaudeCredentials,
    classify_probe_failure,
)

PROBE_PROMPT = "Reply with exactly: OK"
PROBE_TIMEOUT_S = 60.0


@dataclass(frozen=True)
class ProbeResult:
    status: str
    detail: str
    mode: str

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK


def shallow_probe(credentials: ClaudeCredentials) -> ProbeResult:
    """Presence only: a token is set and the CLI resolves. Spends nothing.

    Explicitly cannot detect revocation -- that is what the deep probe is for.
    """
    # CLI first, deliberately: a missing binary is a broken *image*, while a missing token
    # is a broken *deployment*. Reporting the token first would let a build that shipped no
    # CLI hide behind "unconfigured" in an environment that legitimately has no
    # credentials -- which is exactly what CI is.
    try:
        credentials.ensure_cli_available()
    except RuntimeError as exc:
        return ProbeResult(STATUS_FAILED, str(exc), "shallow")
    if not credentials.configured:
        return ProbeResult(STATUS_UNCONFIGURED, "WATCHDOG_CLAUDE_CODE_OAUTH_TOKEN is not set", "shallow")
    return ProbeResult(STATUS_OK, "token present and claude CLI resolved", "shallow")


async def deep_probe(credentials: ClaudeCredentials, *, timeout_s: float = PROBE_TIMEOUT_S) -> ProbeResult:
    """One real authenticated call, through exactly the env the agent gets.

    Running it through ``subprocess_env_overrides`` is the point: a token the app cannot
    reach, a revoked token, and a credential that leaked into the agent's scope all surface
    here rather than at investigation time.
    """
    shallow = shallow_probe(credentials)
    if not shallow.ok:
        return ProbeResult(shallow.status, shallow.detail, "deep")

    binary = credentials.resolved_cli_path() or shutil.which("claude") or "claude"

    try:
        process = await asyncio.create_subprocess_exec(
            binary,
            "-p",
            PROBE_PROMPT,
            "--max-turns",
            "1",
            env={**os.environ, **credentials.subprocess_env_overrides()},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        raw_out, raw_err = await asyncio.wait_for(process.communicate(), timeout=timeout_s)
    except Exception as exc:  # noqa: BLE001 -- any failure is a health verdict, never a crash
        detail = f"agent auth probe failed: {type(exc).__name__}: {exc}"
        return ProbeResult(classify_probe_failure(detail), detail, "deep")

    text = raw_out.decode(errors="replace").strip()
    if process.returncode != 0:
        # The CLI prints auth failures to STDOUT, not stderr, so the reason is usually in
        # text -- read both or a 401 reads as an empty failure.
        reason = text or raw_err.decode(errors="replace").strip()
        detail = f"agent auth failed (exit {process.returncode}): {reason[:200]}"
        return ProbeResult(classify_probe_failure(detail), detail, "deep")
    if not text:
        return ProbeResult(classify_probe_failure("probe returned no output"), "probe returned no output", "deep")
    return ProbeResult(STATUS_OK, f"agent auth OK: {text[:80]}", "deep")
