"""Tool registry — aggregates all tool definitions and provides execution dispatch."""

import logging
from typing import Any

from jenkins_watchdog.tools.jenkins import (
    TOOL_DEFINITIONS as JENKINS_TOOLS,
)
from jenkins_watchdog.tools.jenkins import (
    TOOL_HANDLERS as JENKINS_HANDLERS,
)
from jenkins_watchdog.tools.k8s import (
    TOOL_DEFINITIONS as K8S_TOOLS,
)
from jenkins_watchdog.tools.k8s import (
    TOOL_HANDLERS as K8S_HANDLERS,
)
from jenkins_watchdog.tools.prometheus import (
    TOOL_DEFINITIONS as PROM_TOOLS,
)
from jenkins_watchdog.tools.prometheus import (
    TOOL_HANDLERS as PROM_HANDLERS,
)

logger = logging.getLogger(__name__)

_RAW_TOOLS: list[dict] = K8S_TOOLS + PROM_TOOLS + JENKINS_TOOLS


def _to_openai_format(tool: dict) -> dict:
    """Convert Anthropic-native tool def (input_schema) to OpenAI-compatible (parameters)."""
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
        },
    }


ALL_TOOL_DEFINITIONS: list[dict] = [_to_openai_format(t) for t in _RAW_TOOLS]

_ALL_HANDLERS: dict[str, Any] = {}
_ALL_HANDLERS.update(K8S_HANDLERS)
_ALL_HANDLERS.update(PROM_HANDLERS)
_ALL_HANDLERS.update(JENKINS_HANDLERS)


async def execute_tool(tool_name: str, arguments: dict) -> str:
    """Execute a tool by name with given arguments. Returns string result."""
    handler = _ALL_HANDLERS.get(tool_name)
    if handler is None:
        return f"Unknown tool: {tool_name}. Available: {list(_ALL_HANDLERS.keys())}"

    try:
        result = handler(arguments)
        if hasattr(result, "__await__"):
            result = await result
        return result
    except Exception as e:
        logger.exception("Tool %s failed", tool_name)
        return f"Tool execution error ({tool_name}): {e}"
