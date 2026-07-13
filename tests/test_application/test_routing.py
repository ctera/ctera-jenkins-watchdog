from jenkins_watchdog.application.routing import resolve_routing
from jenkins_watchdog.domain.routing import JobRoute, RoutingConfig, TeamRoute


def config() -> RoutingConfig:
    return RoutingConfig(
        version=1,
        teams=(TeamRoute("platform", ("platform@example.com",)),),
        routes=(
            JobRoute(
                id="app-mrs",
                team="platform",
                jenkins_job_regexes=(r"^app/MR-(?P<mr>\d+)$",),
                provider="github",
                repository="ctera/app",
                mr_number_capture="mr",
            ),
        ),
        global_fallback_recipients=("fallback@example.com",),
    )


def test_route_source_and_recipient_precedence() -> None:
    decision = resolve_routing(
        config=config(),
        incident_source={"kind": "unknown", "confirmed": False},
        job_name="app/MR-42",
        triggering_user_email="trigger@example.com",
    )

    assert decision.source == {
        "kind": "merge_request",
        "confirmed": True,
        "provider": "github",
        "repository": "ctera/app",
        "change_number": "42",
        "job_name": "app/MR-42",
    }
    assert decision.recipients == ("platform@example.com",)


def test_complete_metadata_overrides_route_but_partial_metadata_is_unknown() -> None:
    complete = resolve_routing(
        config=config(),
        incident_source={
            "kind": "merge_request",
            "confirmed": True,
            "provider": "gitlab",
            "repository": "ctera/other",
            "change_number": "99",
        },
        job_name="app/MR-42",
        triggering_user_email=None,
    )
    partial = resolve_routing(
        config=config(),
        incident_source={"kind": "unknown", "confirmed": False, "reason": "partial_scm_metadata"},
        job_name="app/MR-42",
        triggering_user_email=None,
    )

    assert complete.source["provider"] == "gitlab"
    assert partial.source == {
        "kind": "unknown",
        "confirmed": False,
        "reason": "partial_scm_metadata",
    }


def test_recipient_falls_back_to_trigger_then_global() -> None:
    triggered = resolve_routing(
        config=config(), incident_source={}, job_name=None, triggering_user_email="trigger@example.com"
    )
    global_fallback = resolve_routing(config=config(), incident_source={}, job_name=None, triggering_user_email=None)

    assert triggered.recipients == ("trigger@example.com",)
    assert global_fallback.recipients == ("fallback@example.com",)


def test_job_recipient_override_beats_team_and_unmatched_job_uses_trigger() -> None:
    configured = RoutingConfig(
        version=1,
        teams=(TeamRoute("platform", ("team@example.com",)),),
        routes=(
            JobRoute(
                id="override",
                team="platform",
                jenkins_job_regexes=(r"^job-(?P<mr>\d+)$",),
                provider="gitlab",
                repository="ctera/app",
                mr_number_capture="mr",
                recipients=("owner@example.com",),
            ),
        ),
        global_fallback_recipients=("fallback@example.com",),
    )

    matched = resolve_routing(
        config=configured,
        incident_source={},
        job_name="job-42",
        triggering_user_email="trigger@example.com",
    )
    unmatched = resolve_routing(
        config=configured,
        incident_source={},
        job_name="other",
        triggering_user_email="trigger@example.com",
    )

    assert matched.recipients == ("owner@example.com",)
    assert matched.source["change_number"] == "42"
    assert unmatched.recipients == ("trigger@example.com",)


def test_missing_capture_or_team_yields_unknown_source_and_global_recipient() -> None:
    configured = RoutingConfig(
        version=1,
        teams=(),
        routes=(
            JobRoute(
                id="bad-capture",
                team="missing",
                jenkins_job_regexes=(r"^job-(\d+)$",),
                provider="github",
                repository="ctera/app",
                mr_number_capture="missing",
            ),
        ),
        global_fallback_recipients=("fallback@example.com",),
    )

    decision = resolve_routing(
        config=configured,
        incident_source={},
        job_name="job-42",
        triggering_user_email=None,
    )

    assert decision.source == {"kind": "unknown", "confirmed": False}
    assert decision.recipients == ("fallback@example.com",)
