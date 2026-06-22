"""Jenkins API client — async wrapper with bulk endpoints and timeouts."""

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

from jenkins_watchdog.config import settings

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 15

MR_JOB_PATTERN = re.compile(
    r"(?:^|[/_-])(?:MR|mr|PR|pr)(?:[/_-]|$)|merge[-_]?request|MergeRequest|GatedMergeRequest|/MR-|/PR-",
    re.IGNORECASE,
)
FAILED_BUILD_RESULTS = frozenset({"FAILURE", "UNSTABLE", "ABORTED"})
FAILED_JOB_COLORS = frozenset({"red", "yellow", "aborted"})

_server: jenkins.Jenkins | None = None
_client: httpx.AsyncClient | None = None

_COMPUTER_TREE = (
    "computer[displayName,offline,temporarilyOffline,offlineCauseReason,idle,"
    "numExecutors,monitorData,executors[currentExecutable[number,url,timestamp,estimatedDuration]]]"
)


def _get_server() -> jenkins.Jenkins:
    global _server
    if _server is None:
        if not settings.jenkins_url:
            raise RuntimeError("WATCHDOG_JENKINS_URL not configured")
        _server = jenkins.Jenkins(
            settings.jenkins_url,
            username=settings.jenkins_user or None,
            password=settings.jenkins_token or None,
        )
        logger.info("Jenkins client initialized: %s", settings.jenkins_url)
    return _server


def _auth() -> httpx.BasicAuth | None:
    if settings.jenkins_user and settings.jenkins_token:
        return httpx.BasicAuth(settings.jenkins_user, settings.jenkins_token)
    return None


def get_jenkins_http_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=settings.jenkins_url.rstrip("/"),
            auth=_auth(),
            timeout=httpx.Timeout(DEFAULT_TIMEOUT_S, connect=5.0),
            verify=True,
        )
    return _client


async def _run_sync(func, *args, timeout: float = DEFAULT_TIMEOUT_S, **kwargs):
    """Run a synchronous Jenkins API call in a thread with timeout."""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(functools.partial(func, *args, **kwargs)),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        raise TimeoutError(f"Jenkins API call timed out after {timeout}s") from None


async def _get_computers() -> list[dict[str, Any]]:
    """Fetch all Jenkins computers in one bulk API call."""
    client = get_jenkins_http_client()
    resp = await client.get("/computer/api/json", params={"tree": _COMPUTER_TREE})
    resp.raise_for_status()
    data = resp.json()
    return data.get("computer", [])


def is_mr_job(name: str) -> bool:
    """Return True when the job name looks like a merge/PR pipeline."""
    return bool(MR_JOB_PATTERN.search(name))


def job_to_api_path(name: str) -> str:
    """Convert a Jenkins full job name to its REST API path prefix."""
    return "/job/" + "/job/".join(name.split("/"))


def _job_name_from_build_url(url: str) -> str:
    path = urlparse(url).path
    match = re.search(r"/job/(.+?)/\d+/?$", path)
    if match:
        return match.group(1).replace("/job/", "/")
    match = re.search(r"/job/([^/]+)/", path)
    return match.group(1) if match else "unknown"


async def get_nodes() -> list[dict]:
    """Get all Jenkins nodes (agents) with their info."""
    computers = await _get_computers()
    return [node for node in computers if node.get("displayName") not in ("Built-In Node", "master")]


async def get_node_info(name: str) -> dict:
    """Get detailed info for a specific Jenkins node."""
    computers = await _get_computers()
    for node in computers:
        if node.get("displayName") == name:
            return node
    raise KeyError(f"Jenkins node {name!r} not found")


async def get_queue_info() -> list[dict[str, Any]]:
    """Get the Jenkins build queue."""
    server = _get_server()
    info = await _run_sync(server.get_queue_info)
    return info


async def get_running_builds() -> list[dict]:
    """Get all currently running builds via a single bulk computer API call."""
    computers = await _get_computers()
    builds: list[dict] = []

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


async def get_job_info(name: str, depth: int = 0) -> dict:
    """Get info about a specific job."""
    server = _get_server()
    return await _run_sync(server.get_job_info, name, depth=depth)


async def get_build_info(name: str, number: int) -> dict:
    """Get info about a specific build."""
    server = _get_server()
    return await _run_sync(server.get_build_info, name, number)


async def get_build_console_output(name: str, number: int) -> str:
    """Get console output for a specific build."""
    server = _get_server()
    return await _run_sync(server.get_build_console_output, name, number)


async def get_all_jobs(folder_depth: int = 1) -> list[dict]:
    """Get all Jenkins jobs."""
    server = _get_server()
    return await _run_sync(server.get_all_jobs, folder_depth=folder_depth)


async def get_job_recent_builds(job_name: str, limit: int = 10) -> list[dict[str, Any]]:
    """Fetch recent builds for a job via the tree API (includes result and timestamp)."""
    client = get_jenkins_http_client()
    tree = f"builds[number,result,timestamp,duration,url]{{0,{limit}}}"
    resp = await client.get(f"{job_to_api_path(job_name)}/api/json", params={"tree": tree})
    resp.raise_for_status()
    return resp.json().get("builds", [])


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


async def get_recent_failed_builds(
    window_hours: float | None = None,
    *,
    mr_only: bool = False,
    folder_depth: int = 2,
    build_limit: int = 10,
    max_concurrency: int = 30,
) -> list[FailedBuildSummary]:
    """Return failed builds from jobs whose last status indicates failure."""
    if window_hours is None:
        window_hours = settings.jenkins_failed_build_window_hours

    cutoff_ms = (time.time() - window_hours * 3600) * 1000
    jobs = await get_all_jobs(folder_depth=folder_depth)
    candidates = [
        job.get("fullname") or job.get("name", "")
        for job in jobs
        if job.get("color") in FAILED_JOB_COLORS and (job.get("fullname") or job.get("name"))
    ]

    semaphore = asyncio.Semaphore(max_concurrency)

    async def _fetch_job_builds(job_name: str) -> list[FailedBuildSummary]:
        async with semaphore:
            try:
                builds = await get_job_recent_builds(job_name, limit=build_limit)
            except Exception as exc:
                logger.debug("Failed to fetch builds for %s: %s", job_name, exc)
                return []

            failed: list[FailedBuildSummary] = []
            is_mr = is_mr_job(job_name)
            for build in builds:
                result = build.get("result")
                timestamp_ms = build.get("timestamp", 0)
                if result not in FAILED_BUILD_RESULTS or timestamp_ms < cutoff_ms:
                    continue
                failed.append(
                    FailedBuildSummary(
                        job_name=job_name,
                        build_number=build.get("number", 0),
                        result=result,
                        duration_ms=build.get("duration", 0),
                        timestamp_ms=timestamp_ms,
                        url=build.get("url", ""),
                        is_mr=is_mr,
                    )
                )
            return failed

    results = await asyncio.gather(*[_fetch_job_builds(name) for name in candidates])
    failed_builds = [build for job_builds in results for build in job_builds]
    if mr_only:
        failed_builds = [build for build in failed_builds if build.is_mr]
    failed_builds.sort(key=lambda build: build.timestamp_ms, reverse=True)
    return failed_builds


async def get_version() -> str:
    """Get Jenkins server version."""
    server = _get_server()
    return await _run_sync(server.get_version)


async def get_whoami() -> dict:
    """Get current authenticated user info."""
    server = _get_server()
    return await _run_sync(server.get_whoami)


async def close_jenkins_client() -> None:
    global _client
    if _client:
        await _client.aclose()
        _client = None
