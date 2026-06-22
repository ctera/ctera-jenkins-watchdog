"""Jenkins API tools for Claude to investigate CI/CD issues."""

import json
import logging

from jenkins_watchdog.clients.jenkins import (
    get_build_console_output,
    get_build_info,
    get_job_info,
    get_node_info,
    get_nodes,
    get_queue_info,
    get_recent_failed_builds,
    get_running_builds,
)

logger = logging.getLogger(__name__)

MAX_OUTPUT_BYTES = 4096


def _truncate(text: str) -> str:
    if len(text) > MAX_OUTPUT_BYTES:
        return text[:MAX_OUTPUT_BYTES] + "\n... [truncated]"
    return text


async def jenkins_list_agents() -> str:
    """List all Jenkins agents with their status."""
    try:
        nodes = await get_nodes()
        lines = []
        for node in nodes:
            name = node.get("displayName", "unknown")
            offline = node.get("offline", False)
            temp_offline = node.get("temporarilyOffline", False)
            idle = node.get("idle", True)
            executors = node.get("numExecutors", 0)
            reason = node.get("offlineCauseReason", "")

            status = "online"
            if offline and temp_offline:
                status = "temporarily_offline"
            elif offline:
                status = "offline"

            lines.append(f"{name}: status={status} executors={executors} idle={idle}")
            if reason:
                lines.append(f"  reason: {reason}")

            monitor = node.get("monitorData", {})
            if monitor:
                swap = monitor.get("hudson.node_monitors.SwapSpaceMonitor")
                if swap:
                    total = swap.get("totalPhysicalMemory", 0)
                    avail = swap.get("availablePhysicalMemory", 0)
                    if total:
                        used_pct = (1 - avail / total) * 100
                        lines.append(f"  memory: {used_pct:.0f}% used ({avail // (1024**3)}GB free / {total // (1024**3)}GB total)")

                disk = monitor.get("hudson.node_monitors.DiskSpaceMonitor")
                if disk:
                    free = disk.get("size", 0)
                    path = disk.get("path", "")
                    lines.append(f"  disk: {free // (1024**3)}GB free at {path}")

        return _truncate("\n".join(lines) if lines else "No Jenkins agents found")
    except Exception as e:
        return f"Error listing Jenkins agents: {e}"


async def jenkins_get_agent(name: str) -> str:
    """Get detailed info for a specific Jenkins agent."""
    try:
        info = await get_node_info(name)
        return _truncate(json.dumps(info, indent=2, default=str))
    except Exception as e:
        return f"Error getting Jenkins agent {name}: {e}"


async def jenkins_get_queue() -> str:
    """Get the Jenkins build queue."""
    try:
        queue = await get_queue_info()
        if not queue:
            return "Build queue is empty"

        lines = []
        for item in queue:
            task = item.get("task", {}).get("name", "unknown")
            why = item.get("why", "")
            in_queue = item.get("inQueueSince", 0)
            lines.append(f"- {task}: {why}")
            if in_queue:
                import time
                wait_min = (time.time() * 1000 - in_queue) / 60000
                lines.append(f"  waiting: {wait_min:.0f} min")

        return _truncate("\n".join(lines))
    except Exception as e:
        return f"Error getting Jenkins queue: {e}"


async def jenkins_get_running_builds() -> str:
    """Get all currently running builds."""
    try:
        builds = await get_running_builds()
        if not builds:
            return "No builds currently running"

        lines = []
        for build in builds:
            name = build.get("name", "unknown")
            number = build.get("number", 0)
            node = build.get("node", "")
            url = build.get("url", "")
            lines.append(f"- {name}#{number} on={node} url={url}")

        return _truncate("\n".join(lines))
    except Exception as e:
        return f"Error getting running builds: {e}"


async def jenkins_get_job(name: str) -> str:
    """Get info about a specific Jenkins job."""
    try:
        info = await get_job_info(name, depth=0)
        summary = {
            "name": info.get("name"),
            "url": info.get("url"),
            "color": info.get("color"),
            "buildable": info.get("buildable"),
            "inQueue": info.get("inQueue"),
            "lastBuild": info.get("lastBuild"),
            "lastSuccessfulBuild": info.get("lastSuccessfulBuild"),
            "lastFailedBuild": info.get("lastFailedBuild"),
            "healthReport": info.get("healthReport"),
        }
        return _truncate(json.dumps(summary, indent=2, default=str))
    except Exception as e:
        return f"Error getting Jenkins job {name}: {e}"


async def jenkins_get_recent_failed_builds(
    window_hours: int | None = None,
    mr_only: bool = False,
    limit: int = 25,
) -> str:
    """Get recent failed Jenkins builds within a time window."""
    try:
        failed_builds = await get_recent_failed_builds(window_hours=window_hours, mr_only=mr_only)
        if not failed_builds:
            hours = window_hours if window_hours is not None else "configured"
            scope = "MR/PR " if mr_only else ""
            return f"No recent failed {scope}builds in the last {hours} hour(s)"

        lines = []
        for build in failed_builds[:limit]:
            mr_tag = " [MR]" if build.is_mr else ""
            duration_min = build.duration_ms / 60000
            lines.append(
                f"- {build.job_name}#{build.build_number}{mr_tag}: {build.result} "
                f"({duration_min:.1f} min) url={build.url}"
            )

        if len(failed_builds) > limit:
            lines.append(f"... and {len(failed_builds) - limit} more")

        return _truncate("\n".join(lines))
    except Exception as e:
        return f"Error getting recent failed builds: {e}"


async def jenkins_get_build(job_name: str, build_number: int) -> str:
    """Get info about a specific build."""
    try:
        info = await get_build_info(job_name, build_number)
        summary = {
            "number": info.get("number"),
            "result": info.get("result"),
            "duration": info.get("duration"),
            "estimatedDuration": info.get("estimatedDuration"),
            "timestamp": info.get("timestamp"),
            "building": info.get("building"),
            "builtOn": info.get("builtOn"),
            "changeSets": len(info.get("changeSets", [])),
            "actions": [a.get("_class", "") for a in info.get("actions", []) if a.get("_class")],
        }
        return _truncate(json.dumps(summary, indent=2, default=str))
    except Exception as e:
        return f"Error getting build {job_name}#{build_number}: {e}"


async def jenkins_get_build_log(job_name: str, build_number: int, tail_lines: int = 100) -> str:
    """Get console output for a specific build."""
    try:
        output = await get_build_console_output(job_name, build_number)
        lines = output.split("\n")
        if len(lines) > tail_lines:
            lines = lines[-tail_lines:]
        return _truncate("\n".join(lines))
    except Exception as e:
        return f"Error getting build log for {job_name}#{build_number}: {e}"


TOOL_DEFINITIONS = [
    {
        "name": "jenkins_list_agents",
        "description": "List all Jenkins agents/nodes with status, memory, disk, and executor info. Use to see which agents are online/offline.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "jenkins_get_agent",
        "description": "Get detailed info for a specific Jenkins agent including monitor data, executors, and offline reason.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Agent/node name"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "jenkins_get_queue",
        "description": "Get the Jenkins build queue. Shows queued builds, wait reasons, and how long they've been waiting.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "jenkins_get_running_builds",
        "description": "Get all currently running Jenkins builds. Shows job name, build number, and which agent is running them.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "jenkins_get_job",
        "description": "Get info about a specific Jenkins job including last build status, health report, and build history.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Job name (use full path for folder jobs, e.g. 'folder/job-name')"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "jenkins_get_recent_failed_builds",
        "description": (
            "List recent failed Jenkins builds (FAILURE, UNSTABLE, ABORTED) within a time window. "
            "Use to find broken MR/PR pipelines or other failing jobs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "window_hours": {
                    "type": "integer",
                    "description": "Look back this many hours (default: configured window, usually 4)",
                },
                "mr_only": {
                    "type": "boolean",
                    "description": "Only return merge-request / PR style jobs",
                    "default": False,
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of builds to return (default 25)",
                    "default": 25,
                },
            },
        },
    },
    {
        "name": "jenkins_get_build",
        "description": "Get info about a specific build: result, duration, what node ran it, change sets.",
        "input_schema": {
            "type": "object",
            "properties": {
                "job_name": {"type": "string", "description": "Job name"},
                "build_number": {"type": "integer", "description": "Build number"},
            },
            "required": ["job_name", "build_number"],
        },
    },
    {
        "name": "jenkins_get_build_log",
        "description": "Get console output (logs) for a specific build. Use to find error messages, stack traces, or failure reasons.",
        "input_schema": {
            "type": "object",
            "properties": {
                "job_name": {"type": "string", "description": "Job name"},
                "build_number": {"type": "integer", "description": "Build number"},
                "tail_lines": {"type": "integer", "description": "Number of recent lines to return (default 100)", "default": 100},
            },
            "required": ["job_name", "build_number"],
        },
    },
]

TOOL_HANDLERS = {
    "jenkins_list_agents": lambda args: jenkins_list_agents(),
    "jenkins_get_agent": lambda args: jenkins_get_agent(args["name"]),
    "jenkins_get_queue": lambda args: jenkins_get_queue(),
    "jenkins_get_running_builds": lambda args: jenkins_get_running_builds(),
    "jenkins_get_recent_failed_builds": lambda args: jenkins_get_recent_failed_builds(
        args.get("window_hours"),
        args.get("mr_only", False),
        args.get("limit", 25),
    ),
    "jenkins_get_job": lambda args: jenkins_get_job(args["name"]),
    "jenkins_get_build": lambda args: jenkins_get_build(args["job_name"], args["build_number"]),
    "jenkins_get_build_log": lambda args: jenkins_get_build_log(
        args["job_name"], args["build_number"], args.get("tail_lines", 100)
    ),
}
