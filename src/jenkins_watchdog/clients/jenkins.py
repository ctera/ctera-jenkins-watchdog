"""Explicit Jenkins client used by detector adapters."""

from __future__ import annotations

import asyncio
import functools
import logging
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
import jenkins

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 15.0
MR_JOB_PATTERN = re.compile(
    r"(?:^|[/_-])(?:MR|mr|PR|pr)(?:[/_-]|$)|merge[-_]?request|MergeRequest|GatedMergeRequest|/MR-|/PR-",
    re.IGNORECASE,
)
FAILED_BUILD_RESULTS = frozenset({"FAILURE"})
FAILED_JOB_COLORS = frozenset({"red", "yellow", "aborted"})
_COMPUTER_TREE = (
    "computer[displayName,offline,temporarilyOffline,offlineCauseReason,idle,"
    "numExecutors,monitorData,executors[currentExecutable[number,url,timestamp,estimatedDuration]]]"
)


def is_mr_job(name: str) -> bool:
    return bool(MR_JOB_PATTERN.search(name))


def job_to_api_path(name: str) -> str:
    return "/job/" + "/job/".join(name.split("/"))


def _job_name_from_build_url(url: str) -> str:
    path = urlparse(url).path
    match = re.search(r"/job/(.+?)/\d+/?$", path)
    if match:
        return match.group(1).replace("/job/", "/")
    match = re.search(r"/job/([^/]+)/", path)
    return match.group(1) if match else "unknown"


@dataclass(frozen=True)
class FailedBuildSummary:
    job_name: str
    build_number: int
    result: str
    duration_ms: int
    timestamp_ms: int
    url: str
    is_mr: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_name": self.job_name,
            "build_number": self.build_number,
            "result": self.result,
            "duration_ms": self.duration_ms,
            "duration_minutes": round(self.duration_ms / 60000, 1),
            "timestamp_ms": self.timestamp_ms,
            "url": self.url,
            "is_mr": self.is_mr,
        }


class JenkinsClient:
    def __init__(
        self,
        *,
        base_url: str,
        username: str = "",
        token: str = "",
        failed_build_window_hours: float = 4,
        timeout_seconds: float = DEFAULT_TIMEOUT_S,
        server: Any | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("Jenkins URL is required")
        self.base_url = base_url.rstrip("/")
        self.failed_build_window_hours = failed_build_window_hours
        self.timeout_seconds = timeout_seconds
        self._server = server or jenkins.Jenkins(
            self.base_url,
            username=username or None,
            password=token or None,
        )
        auth = httpx.BasicAuth(username, token) if username and token else None
        self._http = http_client or httpx.AsyncClient(
            base_url=self.base_url,
            auth=auth,
            timeout=httpx.Timeout(timeout_seconds, connect=min(5.0, timeout_seconds)),
            verify=True,
        )
        self._owns_http = http_client is None

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def _run_sync(self, func: Any, *args: Any, timeout: float | None = None, **kwargs: Any) -> Any:
        call_timeout = timeout if timeout is not None else self.timeout_seconds
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(functools.partial(func, *args, **kwargs)),
                timeout=call_timeout,
            )
        except TimeoutError:
            raise TimeoutError(f"Jenkins API call timed out after {call_timeout}s") from None

    async def _get_computers(self) -> list[dict[str, Any]]:
        response = await self._http.get("/computer/api/json", params={"tree": _COMPUTER_TREE})
        response.raise_for_status()
        return response.json().get("computer", [])

    async def get_nodes(self) -> list[dict[str, Any]]:
        computers = await self._get_computers()
        return [node for node in computers if node.get("displayName") not in ("Built-In Node", "master")]

    async def get_node_info(self, name: str) -> dict[str, Any]:
        for node in await self._get_computers():
            if node.get("displayName") == name:
                return node
        raise KeyError(f"Jenkins node {name!r} not found")

    async def get_queue_info(self) -> list[dict[str, Any]]:
        return await self._run_sync(self._server.get_queue_info)

    async def get_running_builds(self) -> list[dict[str, Any]]:
        computers = await self._get_computers()
        builds: list[dict[str, Any]] = []
        for computer in computers:
            node_name = computer.get("displayName", "unknown")
            for executor in computer.get("executors", []):
                executable = executor.get("currentExecutable")
                if not executable or "number" not in executable:
                    continue
                url = executable.get("url", "")
                builds.append(
                    {
                        "name": _job_name_from_build_url(url),
                        "number": executable["number"],
                        "url": url,
                        "node": node_name,
                        "timestamp": executable.get("timestamp"),
                        "estimatedDuration": executable.get("estimatedDuration"),
                    }
                )
        return builds

    async def get_job_info(self, name: str, depth: int = 0) -> dict[str, Any]:
        return await self._run_sync(self._server.get_job_info, name, depth=depth)

    async def get_build_info(self, name: str, number: int) -> dict[str, Any]:
        return await self._run_sync(self._server.get_build_info, name, number)

    async def get_build_console_output(self, name: str, number: int) -> str:
        return await self._run_sync(self._server.get_build_console_output, name, number)

    async def get_build_parameters(self, name: str, number: int) -> dict[str, str]:
        info = await self.get_build_info(name, number)
        params: dict[str, str] = {}
        for action in info.get("actions") or []:
            if not isinstance(action, dict):
                continue
            for param in action.get("parameters") or []:
                if isinstance(param, dict) and param.get("name"):
                    value = param.get("value")
                    params[param["name"]] = str(value) if value is not None else ""
        return params

    async def get_all_jobs(self, folder_depth: int = 1) -> list[dict[str, Any]]:
        return await self._run_sync(self._server.get_all_jobs, folder_depth=folder_depth)

    async def get_job_recent_builds(self, job_name: str, limit: int = 10) -> list[dict[str, Any]]:
        tree = f"builds[number,result,timestamp,duration,url]{{0,{limit}}}"
        response = await self._http.get(f"{job_to_api_path(job_name)}/api/json", params={"tree": tree})
        response.raise_for_status()
        return response.json().get("builds", [])

    async def get_recent_failed_builds(
        self,
        window_hours: float | None = None,
        *,
        mr_only: bool = False,
        folder_depth: int = 2,
        build_limit: int = 10,
        max_concurrency: int = 30,
    ) -> list[FailedBuildSummary]:
        window = self.failed_build_window_hours if window_hours is None else window_hours
        cutoff_ms = (time.time() - window * 3600) * 1000
        jobs = await self.get_all_jobs(folder_depth=folder_depth)
        candidates = [
            job.get("fullname") or job.get("name", "")
            for job in jobs
            if job.get("color") in FAILED_JOB_COLORS and (job.get("fullname") or job.get("name"))
        ]
        semaphore = asyncio.Semaphore(max_concurrency)

        async def fetch(job_name: str) -> list[FailedBuildSummary]:
            async with semaphore:
                try:
                    builds = await self.get_job_recent_builds(job_name, limit=build_limit)
                except Exception as exc:
                    logger.debug("Failed to fetch builds for %s: %s", job_name, exc)
                    return []
                result: list[FailedBuildSummary] = []
                mr_job = is_mr_job(job_name)
                for build in builds:
                    build_result = build.get("result")
                    timestamp_ms = build.get("timestamp", 0)
                    if build_result not in FAILED_BUILD_RESULTS or timestamp_ms < cutoff_ms:
                        continue
                    result.append(
                        FailedBuildSummary(
                            job_name=job_name,
                            build_number=build.get("number", 0),
                            result=build_result,
                            duration_ms=build.get("duration", 0),
                            timestamp_ms=timestamp_ms,
                            url=build.get("url", ""),
                            is_mr=mr_job,
                        )
                    )
                return result

        batches = await asyncio.gather(*(fetch(name) for name in candidates))
        failed_builds = [build for batch in batches for build in batch]
        if mr_only:
            failed_builds = [build for build in failed_builds if build.is_mr]
        failed_builds.sort(key=lambda build: build.timestamp_ms, reverse=True)
        return failed_builds

    async def get_version(self) -> str:
        return await self._run_sync(self._server.get_version)

    async def get_whoami(self) -> dict[str, Any]:
        return await self._run_sync(self._server.get_whoami)
