"""A revoked token must fail health, not pass it."""

import pytest

from jenkins_watchdog.reasoning.claude_auth import (
    STATUS_AUTH_FAILED,
    STATUS_FAILED,
    STATUS_NETWORK_UNAVAILABLE,
    STATUS_OK,
    STATUS_UNCONFIGURED,
    ClaudeCredentials,
    classify_probe_failure,
)
from jenkins_watchdog.reasoning.claude_health import deep_probe, shallow_probe


def test_a_revoked_token_is_classified_as_an_auth_failure() -> None:
    """This is the exact string the CLI prints on a revoked token.

    Verified against a deliberately invalid token: exit 1, and the 401 goes to STDOUT
    rather than stderr.
    """
    detail = "agent auth failed (exit 1): Failed to authenticate. API Error: 401 OAuth access token is invalid."

    assert classify_probe_failure(detail) == STATUS_AUTH_FAILED


def test_network_failures_are_not_reported_as_auth_failures() -> None:
    """"Rotate the token" and "the network is down" need different responses."""
    assert classify_probe_failure("connection refused") == STATUS_NETWORK_UNAVAILABLE
    assert classify_probe_failure("probe returned no output") == STATUS_FAILED


def test_shallow_probe_reports_a_missing_token(tmp_path) -> None:
    result = shallow_probe(ClaudeCredentials(token="", config_dir=str(tmp_path)))

    assert result.status == STATUS_UNCONFIGURED
    assert not result.ok


def test_a_missing_cli_is_a_broken_image_not_a_missing_credential(tmp_path) -> None:
    """The distinction matters in CI.

    CI legitimately has no token, so if the token were reported first, a build that
    shipped no usable CLI could hide behind "unconfigured" and reach production.
    """
    credentials = ClaudeCredentials(token="sk-ant-oat01-x", cli_path=str(tmp_path / "absent"))

    result = shallow_probe(credentials)

    assert result.status == STATUS_FAILED
    assert "claude" in result.detail.lower()


def test_shallow_probe_passes_on_presence_alone(tmp_path) -> None:
    """And is explicitly not evidence the token works -- that needs the deep probe."""
    binary = tmp_path / "claude"
    binary.write_text("#!/bin/sh\n")
    credentials = ClaudeCredentials(
        token="sk-ant-oat01-x", cli_path=str(binary), config_dir=str(tmp_path / "home")
    )

    result = shallow_probe(credentials)

    assert result.status == STATUS_OK
    assert result.mode == "shallow"


async def test_deep_probe_short_circuits_when_unconfigured(tmp_path) -> None:
    """No token means no subprocess: an unconfigured app must not spawn a CLI."""
    result = await deep_probe(ClaudeCredentials(token="", config_dir=str(tmp_path)))

    assert result.status == STATUS_UNCONFIGURED
    assert result.mode == "deep"


async def test_deep_probe_reads_stdout_for_the_failure_reason(tmp_path) -> None:
    """The CLI prints auth failures to STDOUT, not stderr.

    Reading only stderr would turn every 401 into an empty, unclassifiable failure.
    """
    binary = tmp_path / "claude"
    binary.write_text(
        "#!/bin/sh\necho 'Failed to authenticate. API Error: 401 OAuth access token is invalid.'\nexit 1\n"
    )
    binary.chmod(0o755)
    credentials = ClaudeCredentials(
        token="sk-ant-oat01-x", cli_path=str(binary), config_dir=str(tmp_path / "home")
    )

    result = await deep_probe(credentials)

    assert result.status == STATUS_AUTH_FAILED
    assert "401" in result.detail


async def test_deep_probe_passes_on_a_real_reply(tmp_path) -> None:
    binary = tmp_path / "claude"
    binary.write_text("#!/bin/sh\necho OK\n")
    binary.chmod(0o755)
    credentials = ClaudeCredentials(
        token="sk-ant-oat01-x", cli_path=str(binary), config_dir=str(tmp_path / "home")
    )

    result = await deep_probe(credentials)

    assert result.ok
    assert result.mode == "deep"


async def test_the_probe_runs_through_the_agent_s_own_scrubbed_environment(tmp_path, monkeypatch) -> None:
    """Otherwise the probe could pass on credentials the agent itself never sees."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-should-be-blanked")
    binary = tmp_path / "claude"
    binary.write_text(
        '#!/bin/sh\nif [ -n "$ANTHROPIC_API_KEY" ]; then echo "leaked"; exit 1; fi\n'
        'if [ -z "$CLAUDE_CONFIG_DIR" ]; then echo "unpinned"; exit 1; fi\necho OK\n'
    )
    binary.chmod(0o755)
    credentials = ClaudeCredentials(
        token="sk-ant-oat01-x", cli_path=str(binary), config_dir=str(tmp_path / "home")
    )

    result = await deep_probe(credentials)

    assert result.ok, result.detail


@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        ("403 Forbidden", STATUS_AUTH_FAILED),
        ("temporary failure in name resolution", STATUS_NETWORK_UNAVAILABLE),
        ("529 overloaded", STATUS_NETWORK_UNAVAILABLE),
        ("something else entirely", STATUS_FAILED),
    ],
)
def test_failure_classification(detail, expected) -> None:
    assert classify_probe_failure(detail) == expected
