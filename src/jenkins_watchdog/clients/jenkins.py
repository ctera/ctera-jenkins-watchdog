"""Jenkins API client — async wrapper with bulk endpoints and timeouts."""

import asyncio
import functools
import logging
import re
from typing import Any
from urllib.parse import urlparse

import httpx
import jenkins

from jenkins_watchdog.config import settings

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 15

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
