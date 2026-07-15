from __future__ import annotations

import time

import httpx
import pytest

from jenkins_watchdog.clients import jenkins as jenkins_module
from jenkins_watchdog.clients.jenkins import FailedBuildSummary, JenkinsClient, is_mr_job, job_to_api_path


class FakeServer:
    def get_queue_info(self):
        return [{"id": 1}]

    def get_job_info(self, name: str, *, depth: int):
        return {"name": name, "depth": depth}

    def get_build_info(self, name: str, number: int):
        return {
            "name": name,
            "number": number,
            "actions": [
                None,
                {
                    "parameters": [
                        {"name": "SET", "value": 42},
                        {"name": "EMPTY", "value": None},
                        {"value": "ignored"},
                        "ignored",
                    ]
                },
            ],
        }

    def get_build_console_output(self, name: str, number: int):
        return f"{name} #{number} full console"

    def get_all_jobs(self, *, folder_depth: int):
        assert folder_depth == 2
        return [
            {"fullname": "portal/MR-42", "color": "red"},
            {"name": "nightly", "color": "yellow"},
            {"name": "broken", "color": "aborted"},
            {"name": "healthy", "color": "blue"},
            {"name": "nameless", "color": "red", "fullname": ""},
        ]

    def get_version(self):
        return "2.479"

    def get_whoami(self):
        return {"id": "watchdog"}


def _transport() -> httpx.MockTransport:
    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/computer/api/json":
            return httpx.Response(
                200,
                json={
                    "computer": [
                        {"displayName": "Built-In Node", "executors": []},
                        {
                            "displayName": "agent-a",
                            "executors": [
                                {"currentExecutable": None},
                                {"currentExecutable": {"url": "https://jenkins/job/folder/job/build/7/"}},
                                {
                                    "currentExecutable": {
                                        "number": 8,
                                        "url": "https://jenkins/job/portal/8/",
                                        "timestamp": 123,
                                        "estimatedDuration": 456,
                                    }
                                },
                            ],
                        },
                    ]
                },
            )
        if path == "/object/api/json":
            return httpx.Response(200, json={"ok": True})
        if path == "/list/api/json":
            return httpx.Response(200, json=[1, 2])
        if path == "/plain":
            return httpx.Response(200, text="plain text")
        if path.endswith("/logText/progressiveText"):
            start = request.url.params.get("start")
            if "missing-header" in path:
                return httpx.Response(200, text="probe")
            if "invalid-header" in path:
                return httpx.Response(200, text="probe", headers={"x-text-size": "bad"})
            if start == str(jenkins_module._CONSOLE_SIZE_PROBE_START):
                return httpx.Response(200, text="", headers={"x-text-size": "1000"})
            assert start == "900"
            return httpx.Response(200, text="tail output")
        if path.endswith("/consoleText"):
            return httpx.Response(200, text="fallback console")
        if path.endswith("/api/json") and "/job/" in path:
            if "/job/broken/" in path:
                return httpx.Response(500, json={"error": "broken"})
            name = "portal/MR-42" if "/job/portal/job/MR-42/" in path else "nightly"
            timestamp = 1_000_000 if name == "portal/MR-42" else 1
            return httpx.Response(
                200,
                json={
                    "builds": [
                        {
                            "number": 7,
                            "result": "FAILURE",
                            "timestamp": timestamp,
                            "duration": 60_000,
                            "url": f"https://jenkins/job/{name}/7/",
                        },
                        {"number": 6, "result": "SUCCESS", "timestamp": 1_000_000},
                    ]
                },
            )
        return httpx.Response(404, json={"path": path})

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_http_and_server_adapters_cover_nodes_builds_logs_and_parameters(monkeypatch) -> None:
    http = httpx.AsyncClient(base_url="https://jenkins.example", transport=_transport())
    client = JenkinsClient(
        base_url="https://jenkins.example/",
        username="user",
        token="token",
        failed_build_window_hours=4,
        timeout_seconds=1,
        server=FakeServer(),
        http_client=http,
    )
    try:
        assert client.base_url == "https://jenkins.example"
        assert await client.get_json("/object/api/json") == {"ok": True}
        with pytest.raises(ValueError, match="did not return an object"):
            await client.get_json("/list/api/json")
        assert await client.get_text("/plain") == "plain text"

        assert [item["displayName"] for item in await client.get_nodes()] == ["agent-a"]
        assert (await client.get_node_info("agent-a"))["displayName"] == "agent-a"
        with pytest.raises(KeyError, match="missing"):
            await client.get_node_info("missing")
        assert await client.get_queue_info() == [{"id": 1}]
        running = await client.get_running_builds()
        assert running == [
            {
                "name": "portal",
                "number": 8,
                "url": "https://jenkins/job/portal/8/",
                "node": "agent-a",
                "timestamp": 123,
                "estimatedDuration": 456,
            }
        ]
        assert (await client.get_job_info("portal", depth=2))["depth"] == 2
        assert (await client.get_build_info("portal", 8))["number"] == 8
        assert await client.get_build_console_output("portal", 8) == "portal #8 full console"
        assert await client.get_build_parameters("portal", 8) == {"SET": "42", "EMPTY": ""}
        assert await client.get_build_console_tail("portal", 8, max_bytes=100) == "tail output"
        assert await client.get_build_console_tail("missing-header", 8) == "fallback console"
        assert await client.get_build_console_tail("invalid-header", 8) == "fallback console"
        assert len(await client.get_job_recent_builds("portal", limit=2)) == 2

        monkeypatch.setattr(jenkins_module.time, "time", lambda: 4000.0)
        failures = await client.get_recent_failed_builds(window_hours=1, max_concurrency=2)
        assert [(item.job_name, item.build_number) for item in failures] == [("portal/MR-42", 7)]
        mr_failures = await client.get_recent_failed_builds(window_hours=1, mr_only=True)
        assert len(mr_failures) == 1 and mr_failures[0].is_mr
        assert await client.get_all_jobs(folder_depth=2)
        assert await client.get_version() == "2.479"
        assert await client.get_whoami() == {"id": "watchdog"}
        await client.close()
        assert not http.is_closed
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_sync_wrapper_has_explicit_timeout_and_constructor_requires_url() -> None:
    with pytest.raises(ValueError, match="URL is required"):
        JenkinsClient(base_url="")
    client = JenkinsClient(
        base_url="https://jenkins.example",
        timeout_seconds=0.001,
        server=FakeServer(),
        http_client=httpx.AsyncClient(transport=_transport()),
    )
    try:
        def slow() -> None:
            time.sleep(0.05)

        with pytest.raises(TimeoutError, match="timed out"):
            await client._run_sync(slow)
    finally:
        await client._http.aclose()


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("folder/MR-42", True),
        ("feature/PR-7", True),
        ("merge-request-8", True),
        ("nightly", False),
    ],
)
def test_jenkins_helpers_and_failed_build_serialization(name: str, expected: bool) -> None:
    assert is_mr_job(name) is expected
    assert job_to_api_path("folder/job") == "/job/folder/job/job"
    assert jenkins_module._job_name_from_build_url("https://jenkins/job/folder/job/build/7/") == "folder/build"
    assert jenkins_module._job_name_from_build_url("https://jenkins/not-a-job") == "unknown"
    summary = FailedBuildSummary("portal", 7, "FAILURE", 90_000, 123, "https://jenkins/7", False)
    assert summary.to_dict()["duration_minutes"] == 1.5
