"""Jenkins API client — async wrapper around python-jenkins."""

import asyncio
import functools
import logging
from typing import Any

import jenkins

from jenkins_watchdog.config import settings

logger = logging.getLogger(__name__)

_server: jenkins.Jenkins | None = None


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


async def _run_sync(func, *args, **kwargs):
    """Run a synchronous Jenkins API call in a thread."""
    return await asyncio.to_thread(functools.partial(func, *args, **kwargs))


async def get_nodes() -> list[dict]:
    """Get all Jenkins nodes (agents) with their info."""
    server = _get_server()
    nodes_info = await _run_sync(server.get_nodes)
    result = []
    for node in nodes_info:
        try:
            info = await _run_sync(server.get_node_info, node["name"])
            result.append(info)
        except Exception as e:
            logger.warning("Failed to get info for node %s: %s", node["name"], e)
            result.append({"displayName": node["name"], "offline": True, "error": str(e)})
    return result


async def get_node_info(name: str) -> dict:
    """Get detailed info for a specific Jenkins node."""
    server = _get_server()
    return await _run_sync(server.get_node_info, name)


async def get_queue_info() -> list[dict[str, Any]]:
    """Get the Jenkins build queue."""
    server = _get_server()
    info = await _run_sync(server.get_queue_info)
    return info


async def get_running_builds() -> list[dict]:
    """Get all currently running builds."""
    server = _get_server()
    builds = await _run_sync(server.get_running_builds)
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
