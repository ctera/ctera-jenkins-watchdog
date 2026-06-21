"""Prometheus tools for Claude to query metrics."""

import logging
from datetime import datetime, timedelta, timezone

from jenkins_watchdog.clients.prometheus import query_instant, query_range

logger = logging.getLogger(__name__)

MAX_OUTPUT_BYTES = 4096


def _truncate(text: str) -> str:
    if len(text) > MAX_OUTPUT_BYTES:
        return text[:MAX_OUTPUT_BYTES] + "\n... [truncated]"
    return text


async def prometheus_query(promql: str) -> str:
    """Execute an instant PromQL query and return results."""
    try:
        results = await query_instant(promql)
        if not results:
            return f"No results for query: {promql}"

        lines = []
        for r in results[:20]:
            metric = r.get("metric", {})
            value = r.get("value", [None, None])
            label_str = ", ".join(f'{k}="{v}"' for k, v in metric.items() if k != "__name__")
            name = metric.get("__name__", "")
            val = value[1] if len(value) > 1 else ""
            lines.append(f"{name}{{{label_str}}} => {val}")

        return _truncate("\n".join(lines))
    except Exception as e:
        return f"Error executing PromQL '{promql}': {e}"


async def prometheus_query_range(promql: str, duration_hours: int = 1, step: str = "5m") -> str:
    """Execute a range PromQL query over a time window."""
    try:
        now = datetime.now(timezone.utc)
        start = (now - timedelta(hours=duration_hours)).isoformat()
        end_ts = now.isoformat()

        results = await query_range(promql, start, end_ts, step)
        if not results:
            return f"No results for range query: {promql}"

        lines = []
        for r in results[:10]:
            metric = r.get("metric", {})
            values = r.get("values", [])
            label_str = ", ".join(f'{k}="{v}"' for k, v in metric.items() if k != "__name__")
            name = metric.get("__name__", "")
            lines.append(f"{name}{{{label_str}}}:")
            for ts, val in values[-5:]:
                lines.append(f"  {datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%H:%M')} => {val}")

        return _truncate("\n".join(lines))
    except Exception as e:
        return f"Error executing range query '{promql}': {e}"


TOOL_DEFINITIONS = [
    {
        "name": "prometheus_query",
        "description": "Execute an instant PromQL query. Use for current values like memory usage, CPU, pod counts, error rates.",
        "input_schema": {
            "type": "object",
            "properties": {
                "promql": {"type": "string", "description": "PromQL expression (e.g. 'container_memory_working_set_bytes{namespace=\"jenkins\"}')"},
            },
            "required": ["promql"],
        },
    },
    {
        "name": "prometheus_query_range",
        "description": "Execute a range PromQL query to see trends over time. Use for spotting spikes, growth, or degradation patterns.",
        "input_schema": {
            "type": "object",
            "properties": {
                "promql": {"type": "string", "description": "PromQL expression"},
                "duration_hours": {"type": "integer", "description": "How many hours back to look (default 1)", "default": 1},
                "step": {"type": "string", "description": "Query resolution step (default '5m')", "default": "5m"},
            },
            "required": ["promql"],
        },
    },
]

TOOL_HANDLERS = {
    "prometheus_query": lambda args: prometheus_query(args["promql"]),
    "prometheus_query_range": lambda args: prometheus_query_range(
        args["promql"], args.get("duration_hours", 1), args.get("step", "5m")
    ),
}
