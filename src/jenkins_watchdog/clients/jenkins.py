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
_CONSOLE_SIZE_PROBE_START = 10**15
MR_JOB_PATTERN = re.compile(
    r"(?:^|[/_-])(?:MR|mr|PR|pr)(?:[/_-]|$)|merge[-_]?request|MergeRequest|GatedMergeRequest|/MR-|/PR-",
    re.IGNORECASE,
)
FAILED_BUILD_RESULTS = frozenset({"FAILURE"})
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

    async def get_json(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Read a Jenkins JSON endpoint through the configured authenticated client."""
        response = await self._http.get(path, params=params)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError(f"Jenkins endpoint {path!r} did not return an object")
        return payload

    async def get_text(self, path: str) -> str:
        """Read a Jenkins text endpoint through the configured authenticated client."""
        response = await self._http.get(path)
        response.raise_for_status()
        return response.text

    async def get_build_console_tail(self, name: str, number: int, *, max_bytes: int = 160_000) -> str:
        """Read a bounded tail of a build log through Jenkins' progressive endpoint."""
        path = f"{job_to_api_path(name)}/{number}/logText/progressiveText"
        probe = await self._http.get(path, params={"start": _CONSOLE_SIZE_PROBE_START})
        probe.raise_for_status()
        size_header = probe.headers.get("x-text-size")
        if size_header is None:
            return await self.get_text(f"{job_to_api_path(name)}/{number}/consoleText")
        try:
            size = max(0, int(size_header))
        except ValueError:
            return await self.get_text(f"{job_to_api_path(name)}/{number}/consoleText")
        response = await self._http.get(path, params={"start": max(0, size - max(1, max_bytes))})
        response.raise_for_status()
        return response.text

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

    async def get_job_recent_builds(
        self,
        job_name: str,
        limit: int = 10,
        *,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        start = max(0, offset)
        end = start + max(1, limit)
        tree = f"builds[number,result,timestamp,duration,url]{{{start},{end}}}"
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
            str(job.get("fullname") or job.get("name")) for job in jobs if job.get("fullname") or job.get("name")
        ]
        if mr_only:
            candidates = [name for name in candidates if is_mr_job(name)]
        semaphore = asyncio.Semaphore(max_concurrency)
        page_size = max(1, build_limit)

        async def fetch(job_name: str) -> list[FailedBuildSummary]:
            async with semaphore:
                result: list[FailedBuildSummary] = []
                mr_job = is_mr_job(job_name)
                seen_builds: set[int] = set()
                offset = 0
                while True:
                    try:
                        builds = await self.get_job_recent_builds(
                            job_name,
                            limit=page_size,
                            offset=offset,
                        )
                    except Exception as exc:
                        logger.debug("Failed to fetch builds for %s: %s", job_name, exc)
                        break
                    if not builds:
                        break

                    new_builds = 0
                    reached_cutoff = False
                    for build in builds:
                        build_number = int(build.get("number") or 0)
                        if build_number in seen_builds:
                            continue
                        seen_builds.add(build_number)
                        new_builds += 1
                        timestamp_ms = int(build.get("timestamp") or 0)
                        if timestamp_ms < cutoff_ms:
                            reached_cutoff = True
                            continue
                        build_result = str(build.get("result") or "")
                        if build_result not in FAILED_BUILD_RESULTS:
                            continue
                        result.append(
                            FailedBuildSummary(
                                job_name=job_name,
                                build_number=build_number,
                                result=build_result,
                                duration_ms=int(build.get("duration") or 0),
                                timestamp_ms=timestamp_ms,
                                url=str(build.get("url") or ""),
                                is_mr=mr_job,
                            )
                        )
                    if reached_cutoff or len(builds) < page_size or new_builds == 0:
                        break
                    offset += len(builds)
                return result

        batches = await asyncio.gather(*(fetch(name) for name in candidates))
        failed_builds = [build for batch in batches for build in batch]
        failed_builds.sort(key=lambda build: build.timestamp_ms, reverse=True)
        return failed_builds

    async def get_version(self) -> str:
        return await self._run_sync(self._server.get_version)

    async def get_whoami(self) -> dict[str, Any]:
        return await self._run_sync(self._server.get_whoami)
