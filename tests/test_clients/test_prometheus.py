from __future__ import annotations

import httpx
import pytest

from jenkins_watchdog.clients.prometheus import PrometheusClient


def _client() -> httpx.AsyncClient:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("query") == "bad":
            return httpx.Response(200, json={"status": "error", "error": "invalid query"})
        return httpx.Response(
            200,
            json={"status": "success", "data": {"result": [{"metric": {}, "value": [1, "2"]}]}},
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_prometheus_query_and_range_are_read_only_and_validate_payload_status() -> None:
    http = _client()
    client = PrometheusClient(http, endpoint="https://prometheus.example/", enabled=True)
    try:
        assert len(await client.query("up")) == 1
        assert len(await client.query_range("up", start="1", end="2", step="5m")) == 1
        with pytest.raises(RuntimeError, match="invalid query"):
            await client.query("bad")
        with pytest.raises(RuntimeError, match="invalid query"):
            await client.query_range("bad", start="1", end="2", step="5m")
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_disabled_prometheus_is_an_empty_noop() -> None:
    http = _client()
    client = PrometheusClient(http, endpoint="", enabled=True)
    try:
        assert await client.query("up") == []
        assert await client.query_range("up", start="1", end="2", step="5m") == []
    finally:
        await http.aclose()
