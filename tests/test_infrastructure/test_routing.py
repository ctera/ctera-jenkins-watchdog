from pathlib import Path

import pytest

from jenkins_watchdog.infrastructure.routing import InvalidRoutingConfig, load_routing_config


def test_checked_in_routing_config_is_valid() -> None:
    config = load_routing_config("config/routing.yaml")
    assert config.version == 1


@pytest.mark.parametrize(
    "content",
    [
        "version: 2\nteams: {}\nroutes: []\n",
        "version: 1\nteams: {}\nroutes: []\nunknown: true\n",
        "version: 1\nteams: {}\nroutes:\n  - id: bad\n    team: missing\n",
    ],
)
def test_invalid_routing_fails_startup(tmp_path: Path, content: str) -> None:
    path = tmp_path / "routing.yaml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(InvalidRoutingConfig):
        load_routing_config(path)


def test_complete_routing_config_loads_deduplicated_recipients(tmp_path: Path) -> None:
    path = tmp_path / "routing.yaml"
    path.write_text(
        """
version: 1
teams:
  platform:
    recipients: [platform@example.com, platform@example.com]
routes:
  - id: app-mrs
    team: platform
    jenkins_job_regexes: ['^app/MR-(?P<mr>\\d+)$']
    provider: GitHub
    repository: ctera/app
    mr_number_capture: mr
    recipients: [override@example.com]
global_fallback_recipients: [fallback@example.com]
""",
        encoding="utf-8",
    )

    config = load_routing_config(path)

    assert config.team("platform").recipients == ("platform@example.com",)
    assert config.team("missing") is None
    assert config.routes[0].provider == "github"
    assert config.routes[0].recipients == ("override@example.com",)


@pytest.mark.parametrize(
    "route",
    [
        "jenkins_job_regexes: ['[']\n    provider: github\n    repository: repo\n    mr_number_capture: mr",
        "jenkins_job_regexes: ['^MR-(\\d+)$']\n    provider: bitbucket\n    repository: repo\n    mr_number_capture: '1'",
        "jenkins_job_regexes: ['^MR-(\\d+)$']\n    provider: github\n    repository: repo\n    mr_number_capture: missing",
    ],
)
def test_route_regex_provider_and_capture_are_strict(tmp_path: Path, route: str) -> None:
    path = tmp_path / "routing.yaml"
    path.write_text(
        f"""version: 1
teams:
  platform:
    recipients: [platform@example.com]
routes:
  - id: bad
    team: platform
    {route}
""",
        encoding="utf-8",
    )

    with pytest.raises(InvalidRoutingConfig):
        load_routing_config(path)


@pytest.mark.parametrize("recipients", ["not-a-list", "['invalid']", "['@example.com']"])
def test_recipient_validation_is_strict(tmp_path: Path, recipients: str) -> None:
    path = tmp_path / "routing.yaml"
    path.write_text(
        f"version: 1\nteams: {{}}\nroutes: []\nglobal_fallback_recipients: {recipients}\n",
        encoding="utf-8",
    )
    with pytest.raises(InvalidRoutingConfig):
        load_routing_config(path)
