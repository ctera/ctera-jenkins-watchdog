"""Prometheus client (direct HTTP, in-cluster, no auth)."""

import logging

import httpx

from jenkins_watchdog.config import settings

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None


def get_prometheus_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=settings.prometheus_endpoint,
            timeout=httpx.Timeout(settings.request_timeout_s, connect=5.0),
        )
    return _client


async def query_instant(promql: str) -> list[dict]:
    """Execute an instant PromQL query."""
    if not settings.prometheus_enabled or not settings.prometheus_endpoint:
        return []
    client = get_prometheus_client()
    resp = await client.get("/api/v1/query", params={"query": promql})
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") == "success":
        return data["data"]["result"]
    logger.warning("Prometheus query failed: %s", data.get("error"))
    return []


async def query_range(promql: str, start: str, end: str, step: str = "5m") -> list[dict]:
    """Execute a range PromQL query."""
    client = get_prometheus_client()
    resp = await client.get(
        "/api/v1/query_range",
        params={"query": promql, "start": start, "end": end, "step": step},
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") == "success":
        return data["data"]["result"]
    return []


async def close_prometheus_client() -> None:
    global _client
    if _client:
        await _client.aclose()
        _client = None
