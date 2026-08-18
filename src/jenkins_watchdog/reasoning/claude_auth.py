"""Credentials for the Claude Code subprocess: scrub the parent env, inject the token.

The agent authenticates with ``CLAUDE_CODE_OAUTH_TOKEN`` (mint one with ``claude
setup-token``), never with an Anthropic API key -- the OAuth token has no quota against
the raw Messages API, so the Agent SDK is the only path it works on.

Two properties this module exists to guarantee:

**App-scoped identity.** The token is resolved from the app's own settings
(``WATCHDOG_CLAUDE_CODE_OAUTH_TOKEN``) and handed to the subprocess explicitly as the
bare ``CLAUDE_CODE_OAUTH_TOKEN``. Nothing requires an operator to export it, so a
developer signed into a different Claude account interactively keeps working untouched,
and no other tool on the machine inherits this app's identity.

**No silent fallback.** ``CLAUDE_CONFIG_DIR`` is pinned to a private, credential-free
directory. Without that the CLI resolves a missing or revoked token against whatever
``~/.claude`` login happens to exist -- turns keep succeeding under the wrong identity,
which is the failure mode hardest to notice. An empty config dir leaves the token as the
only way in, so a bad token fails loud.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from jenkins_watchdog.config import Settings

CLAUDE_CODE_OAUTH_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
CLAUDE_CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"
ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"

_ENV_PREFIX = "WATCHDOG_"
# Matched against whole ``_``-separated segments, never as substrings: "valkey_host"
# contains "key" and would otherwise be scrubbed, breaking Valkey connectivity.
_SECRET_SEGMENTS = frozenset({"token", "secret", "password", "key", "pat", "credential"})
# Settings whose name ends in a credential-shaped segment but whose value is a filesystem
# path. Blanking these would break TLS, not protect anything.
_PATH_SETTINGS = frozenset({"valkey_client_key", "valkey_client_cert", "valkey_ca_cert"})
# The identifier half of a basic-auth pair. Useless to an attacker alone, but it halves
# the work, and the subprocess has no reason to see it.
_EXTRA_SECRET_SETTINGS = frozenset({"jenkins_user", "jira_user_email"})


def bundled_cli_path() -> Path | None:
    """The `claude` binary shipped inside the claude-agent-sdk wheel.

    The SDK prefers this over anything on ``$PATH``, and in the container it is the only
    copy that exists -- nothing installs a `claude` on PATH there. So a preflight that
    only consulted ``shutil.which`` would declare a perfectly working image broken.
    """
    try:
        import claude_agent_sdk
    except ImportError:  # pragma: no cover - the dependency is required
        return None
    candidate = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
    return candidate if candidate.is_file() else None


def secret_setting_names() -> frozenset[str]:
    """Settings fields that hold a credential.

    Derived from the model rather than hand-listed, so a newly added integration secret
    is scrubbed the day it lands instead of the day someone remembers to update a
    frozenset. Only ``str`` fields qualify -- an int named ``..._token_budget`` would
    otherwise match the "token" segment.
    """
    names = set(_EXTRA_SECRET_SETTINGS)
    for name, field in Settings.model_fields.items():
        if name in _PATH_SETTINGS or field.annotation is not str:
            continue
        if _SECRET_SEGMENTS & set(name.lower().split("_")):
            names.add(name)
    return frozenset(names & set(Settings.model_fields))


def credential_env_vars() -> frozenset[str]:
    """Environment variables that must not be visible to the subprocess.

    The subprocess reads untrusted input (a Jenkins console log, a merge-request body),
    so a prompt injection could otherwise exfiltrate a secret by reading it out of the
    environment. Integration access is unaffected: tools run in-process on the parent
    side, so the subprocess never needs these.

    ``ANTHROPIC_API_KEY`` is here and it is load-bearing rather than hygiene -- an API key
    present in the environment is still reported by the CLI as an available credential,
    so blanking it leaves the OAuth token as the only one.
    ``CLAUDE_CODE_OAUTH_TOKEN`` is deliberately absent: the subprocess needs it.
    """
    return frozenset(
        {ANTHROPIC_API_KEY_ENV} | {f"{_ENV_PREFIX}{name.upper()}" for name in secret_setting_names()}
    )


def credential_env_overrides(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Map of credential vars present in the parent env to ``""``.

    Blanked, never deleted: the SDK merges ``options.env`` over the inherited environment,
    so ``""`` is the only way to unset one. Blanking is enough -- an *empty*
    ``ANTHROPIC_API_KEY`` does not occupy the credential precedence slot the way a set one
    does.

    Only vars actually present are emitted, so this is ``{}`` in a clean test environment.
    """
    source = os.environ if environ is None else environ
    return {var: "" for var in credential_env_vars() if var in source}


@dataclass(frozen=True)
class ClaudeCredentials:
    """Everything the agent subprocess needs in order to authenticate as this app."""

    token: str = ""
    cli_path: str = ""
    config_dir: str = ""

    @classmethod
    def from_settings(cls, settings: Settings) -> ClaudeCredentials:
        return cls(
            token=settings.claude_code_oauth_token.strip(),
            cli_path=settings.claude_code_path.strip(),
            config_dir=settings.claude_config_dir.strip(),
        )

    @property
    def configured(self) -> bool:
        return bool(self.token)

    def agent_config_dir(self) -> Path:
        """A private ``CLAUDE_CONFIG_DIR`` holding no credentials.

        See the module docstring: its emptiness is the whole point. Created 0o700 because
        the CLI writes session state into it.
        """
        path = Path(self.config_dir) if self.config_dir else Path.home() / ".jenkins-watchdog" / "claude-home"
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return path

    def subprocess_env_overrides(self, environ: Mapping[str, str] | None = None) -> dict[str, str]:
        """The full ``ClaudeAgentOptions.env`` payload: scrub, then inject auth.

        One function owns both halves so they cannot drift apart -- zeroing
        ``ANTHROPIC_API_KEY`` without supplying the token would leave the agent
        unauthenticated, and supplying the token without zeroing the key would leave a
        second credential in scope.
        """
        env = credential_env_overrides(environ)
        if self.token:
            env[CLAUDE_CODE_OAUTH_TOKEN_ENV] = self.token
        env[CLAUDE_CONFIG_DIR_ENV] = str(self.agent_config_dir())
        return env

    def resolved_cli_path(self) -> str:
        """The ``claude`` binary to spawn. Empty means "let the SDK find it"."""
        if self.cli_path:
            return self.cli_path
        bundled = bundled_cli_path()
        return str(bundled) if bundled else ""

    def ensure_credentials(self) -> None:
        if self.configured:
            return
        raise RuntimeError(
            f"{_ENV_PREFIX}{CLAUDE_CODE_OAUTH_TOKEN_ENV} is required: reasoning is disabled.\n"
            "Mint one with `claude setup-token`, then set it in .env (local) or the "
            "jenkins-watchdog-secrets Secret (prod).\n"
            "Do NOT export it globally -- the app passes it to the agent subprocess itself."
        )

    def ensure_cli_available(self) -> None:
        if self.cli_path:
            if Path(self.cli_path).is_file():
                return
            raise RuntimeError(
                f"{_ENV_PREFIX}CLAUDE_CODE_PATH is set to {self.cli_path!r} but that file does not exist."
            )
        if bundled_cli_path() is not None or shutil.which("claude") is not None:
            return
        raise RuntimeError(
            "Claude Code CLI ('claude') not found: reasoning is disabled.\n"
            "It normally ships inside the claude-agent-sdk wheel; a missing binary usually "
            "means the SDK was installed from an sdist rather than a wheel.\n"
            f"Reinstall claude-agent-sdk, or set {_ENV_PREFIX}CLAUDE_CODE_PATH to the binary path."
        )

    def ensure_ready(self) -> None:
        """Both preflights, in the order whose error is most actionable."""
        self.ensure_credentials()
        self.ensure_cli_available()


# --- health -----------------------------------------------------------------

STATUS_OK = "ok"
STATUS_UNCONFIGURED = "unconfigured"
STATUS_AUTH_FAILED = "auth_failed"
STATUS_NETWORK_UNAVAILABLE = "network_unavailable"
STATUS_FAILED = "failed"

_AUTH_MARKERS = (
    "401",
    "403",
    "unauthor",
    "forbidden",
    "authentication failed",
    "authentication_error",
    "oauth",
    "invalid api key",
    "invalid x-api-key",
)
_NETWORK_MARKERS = (
    "connection refused",
    "connection reset",
    "timed out",
    "timeout",
    "temporary failure in name resolution",
    "could not resolve",
    "502",
    "503",
    "529",
)


def classify_probe_failure(detail: str) -> str:
    """Sort a failed probe into something an operator can act on.

    A revoked token and an unreachable network need different responses, and "unhealthy"
    tells nobody which one happened.
    """
    lowered = detail.lower()
    if any(marker in lowered for marker in _AUTH_MARKERS):
        return STATUS_AUTH_FAILED
    if any(marker in lowered for marker in _NETWORK_MARKERS):
        return STATUS_NETWORK_UNAVAILABLE
    return STATUS_FAILED
