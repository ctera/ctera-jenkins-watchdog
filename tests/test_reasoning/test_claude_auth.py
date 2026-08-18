"""The subprocess must get the OAuth token and nothing else.

The agent subprocess reads untrusted input (Jenkins console logs, merge-request bodies),
so anything left in its environment is reachable by a prompt injection.
"""

import re
from pathlib import Path

import pytest

from jenkins_watchdog.config import Settings
from jenkins_watchdog.reasoning.claude_auth import (
    ANTHROPIC_API_KEY_ENV,
    CLAUDE_CODE_OAUTH_TOKEN_ENV,
    CLAUDE_CONFIG_DIR_ENV,
    ClaudeCredentials,
    credential_env_overrides,
    credential_env_vars,
    secret_setting_names,
)

TOKEN = "sk-ant-oat01-testtoken"


def _credentials(tmp_path: Path, *, token: str = TOKEN) -> ClaudeCredentials:
    return ClaudeCredentials(token=token, config_dir=str(tmp_path / "claude-home"))


def test_overrides_blank_only_secrets_that_are_set(tmp_path: Path) -> None:
    environ = {"WATCHDOG_JENKINS_TOKEN": "jenkins", "PATH": "/usr/bin"}

    overrides = credential_env_overrides(environ)

    assert overrides["WATCHDOG_JENKINS_TOKEN"] == ""
    # Unset secrets are not added -- the payload stays minimal, and is {} in a clean
    # test environment.
    assert "WATCHDOG_GITHUB_TOKEN" not in overrides
    # Non-credentials are never touched.
    assert "PATH" not in overrides


def test_anthropic_api_key_is_blanked_not_dropped(tmp_path: Path) -> None:
    """Blanking is load-bearing, not hygiene.

    The SDK merges options.env OVER the inherited env, so "" is the only way to unset a
    var. An API key left in scope is still reported by the CLI as an available
    credential; blanking it leaves the OAuth token as the only one.
    """
    environ = {ANTHROPIC_API_KEY_ENV: "sk-ant-api03-xxx"}

    env = _credentials(tmp_path).subprocess_env_overrides(environ)

    assert env[ANTHROPIC_API_KEY_ENV] == ""
    assert ANTHROPIC_API_KEY_ENV in credential_env_vars()


def test_oauth_token_is_injected_and_never_scrubbed(tmp_path: Path) -> None:
    env = _credentials(tmp_path).subprocess_env_overrides({})

    assert env[CLAUDE_CODE_OAUTH_TOKEN_ENV] == TOKEN
    # The app's own prefixed copy is scrubbed; the bare name the CLI reads is not.
    assert CLAUDE_CODE_OAUTH_TOKEN_ENV not in credential_env_vars()
    assert "WATCHDOG_CLAUDE_CODE_OAUTH_TOKEN" in credential_env_vars()


def test_config_dir_is_pinned_and_holds_no_credentials(tmp_path: Path) -> None:
    """The pinned config dir is what turns a revoked token into a loud 401.

    Without it the CLI falls back to whatever ~/.claude login exists and answers the turn
    under the wrong identity -- succeeding, which is the failure mode hardest to notice.
    """
    credentials = _credentials(tmp_path)

    env = credentials.subprocess_env_overrides({})
    path = Path(env[CLAUDE_CONFIG_DIR_ENV])

    assert path.is_dir()
    assert list(path.iterdir()) == []
    assert path.stat().st_mode & 0o777 == 0o700


def test_every_secret_setting_is_scrubbed() -> None:
    """Rot-guard: a newly added integration secret must not escape the scrub.

    Derived from the settings model, so this fails when someone adds a credential field
    that the scrub set does not cover.
    """
    pattern = re.compile(r"(^|_)(token|secret|password|key|pat|credential)$")
    declared = {
        name
        for name, field in Settings.model_fields.items()
        if field.annotation is str and pattern.search(name.lower())
    }
    # valkey_* are TLS file paths, not credentials.
    declared -= {"valkey_client_key", "valkey_client_cert", "valkey_ca_cert"}

    assert declared - secret_setting_names() == set()
    assert {f"WATCHDOG_{name.upper()}" for name in declared} - credential_env_vars() == set()


def test_valkey_host_is_not_mistaken_for_a_credential() -> None:
    """Segment matching, not substring matching.

    "valkey_host" contains "key"; scrubbing it would blank the Valkey hostname and break
    every session lookup, with nothing pointing at the auth layer as the cause.
    """
    assert "valkey_host" not in secret_setting_names()
    assert "valkey_client_key" not in secret_setting_names()
    assert "WATCHDOG_VALKEY_HOST" not in credential_env_vars()


def test_missing_token_fails_the_preflight(tmp_path: Path) -> None:
    credentials = _credentials(tmp_path, token="")

    assert not credentials.configured
    with pytest.raises(RuntimeError, match="disabled"):
        credentials.ensure_credentials()


def test_an_api_key_alone_does_not_satisfy_the_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An API key must not satisfy the preflight.

    The agent cannot authenticate with it (the subprocess blanks it), so accepting it here
    would turn a clear startup error into every turn failing at run time.
    """
    monkeypatch.setenv(ANTHROPIC_API_KEY_ENV, "sk-ant-api03-xxx")

    with pytest.raises(RuntimeError, match="disabled"):
        _credentials(tmp_path, token="").ensure_credentials()


def test_explicit_cli_path_must_exist(tmp_path: Path) -> None:
    missing = ClaudeCredentials(token=TOKEN, cli_path=str(tmp_path / "nope"))
    with pytest.raises(RuntimeError, match="does not exist"):
        missing.ensure_cli_available()

    binary = tmp_path / "claude"
    binary.write_text("#!/bin/sh\n")
    ClaudeCredentials(token=TOKEN, cli_path=str(binary)).ensure_cli_available()


def test_from_settings_strips_whitespace() -> None:
    """A stray newline gives a 401 that looks exactly like revocation."""
    settings = Settings(_env_file=None, claude_code_oauth_token=f"  {TOKEN}\n")

    assert ClaudeCredentials.from_settings(settings).token == TOKEN
