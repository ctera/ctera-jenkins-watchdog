"""Small read-only Prometheus HTTP client."""

from __future__ import annotations

from typing import Any

import httpx


class PrometheusClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        endpoint: str,
        enabled: bool,
    ) -> None:
        self._http = http
        self._endpoint = endpoint.rstrip("/")
        self._enabled = enabled and bool(endpoint)

    async def query(self, promql: str) -> list[dict[str, Any]]:
        if not self._enabled:
            return []
        response = await self._http.get(f"{self._endpoint}/api/v1/query", params={"query": promql})
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") != "success":
            raise RuntimeError(str(payload.get("error") or "Prometheus query failed"))
        return list(payload.get("data", {}).get("result", []))

    async def query_range(
        self,
        promql: str,
        *,
        start: str,
        end: str,
        step: str,
    ) -> list[dict[str, Any]]:
        if not self._enabled:
            return []
        response = await self._http.get(
            f"{self._endpoint}/api/v1/query_range",
            params={"query": promql, "start": start, "end": end, "step": step},
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") != "success":
            raise RuntimeError(str(payload.get("error") or "Prometheus range query failed"))
        return list(payload.get("data", {}).get("result", []))
