"""Locating prompts/system.md — the one place that resolution is allowed to happen.

This existed inline in both engine.py and triage.py as

    PROMPTS_DIR = Path(__file__).parent.parent.parent.parent / "prompts"

which is correct in a source checkout and wrong in the container. There the package is
imported from site-packages, so the four `.parent` hops land on
``/usr/local/lib/python3.12/prompts`` while the real prompts sit at ``/app/prompts``.
Both call sites then failed *silently* — engine fell back to a one-line system prompt,
triage returned "" — so production ran without ``system.md`` and nothing said so.

Hence two rules here: the directory is configurable (``WATCHDOG_PROMPTS_DIR``, set in the
image), and a miss is logged at WARNING rather than swallowed.
"""

from __future__ import annotations

import logging
from pathlib import Path

from jenkins_watchdog.config import settings

logger = logging.getLogger(__name__)

# Source-checkout layout: src/jenkins_watchdog/reasoning/ -> repo root -> prompts/
_CHECKOUT_PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "prompts"

_warned: set[str] = set()


def prompts_dir() -> Path:
    """The directory holding system.md. Explicit setting wins over the checkout guess."""
    configured = settings.prompts_dir.strip()
    return Path(configured) if configured else _CHECKOUT_PROMPTS_DIR


def read_prompt(name: str) -> str:
    """Prompt file contents, or "" if it is not there — warning once per missing file.

    Callers still degrade gracefully, but no longer do so invisibly: a deployment that
    shipped without its prompts says so in the logs the first time it matters.
    """
    path = prompts_dir() / name
    try:
        return path.read_text()
    except OSError as exc:
        key = str(path)
        if key not in _warned:
            _warned.add(key)
            logger.warning(
                "Prompt file %s is unreadable (%s) — falling back to built-in defaults. "
                "Set WATCHDOG_PROMPTS_DIR to the directory containing %s.",
                path,
                exc,
                name,
            )
        return ""
