"""Claude Code transport for the reasoning layer.

Two capabilities, both authenticated by ``CLAUDE_CODE_OAUTH_TOKEN`` (see ``claude_auth``):

``complete`` -- a one-shot, tool-free turn. Used wherever the old adapter wanted a single
answer (triage, structured extraction). Returns the same
``(content, tool_calls, usage)`` triple ``_call_with_fallback`` used to, so the parsing
and accounting call sites are untouched by the transport swap.

``run_agent`` -- the tool loop. Unlike the old adapter, the *SDK* drives this loop: the
app's read-only tools are published as an in-process MCP server, so Claude Code decides
which to call and we observe the results. Tools still execute in this process, on the
parent side, which is what keeps their truncation and error handling intact and keeps
integration credentials out of the subprocess entirely.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    tool,
)

from jenkins_watchdog.config import settings
from jenkins_watchdog.reasoning.claude_auth import ClaudeCredentials, secret_setting_names
from jenkins_watchdog.scan_options import ScanOptions, activate_scan_options, reset_scan_options
from jenkins_watchdog.tools import _RAW_TOOLS, execute_tool

logger = logging.getLogger(__name__)

MCP_SERVER_NAME = "watchdog"

# Aggregate context budget per agent run. A per-result cap alone does not bound context:
# an investigation that reads one console log per failing build makes dozens of calls, and
# dozens times any fixed cap still exhausts the window. The floor keeps a late result
# readable rather than eliding it to nothing.
_HISTORY_BUDGET = {False: 120_000, True: 240_000}
_RESULT_FLOOR = {False: 2_000, True: 4_000}

_REDACTED = "[redacted]"
# Below this length a "secret" is more likely to appear by coincidence in a log than to be
# the credential itself, and blind replacement would corrupt the evidence.
_MIN_REDACTABLE_SECRET_LEN = 8


def qualified_tool_name(name: str) -> str:
    """Tools published through an SDK MCP server are addressed with this prefix."""
    return f"mcp__{MCP_SERVER_NAME}__{name}"


def plain_tool_name(name: str) -> str:
    prefix = f"mcp__{MCP_SERVER_NAME}__"
    return name[len(prefix) :] if name.startswith(prefix) else name


def secret_values() -> tuple[str, ...]:
    """Credential values currently configured, longest first.

    Longest-first so that a token containing another secret as a substring is replaced
    whole rather than leaving a partial tail behind.
    """
    values = set()
    for name in secret_setting_names():
        value = getattr(settings, name, "")
        if isinstance(value, str) and len(value.strip()) >= _MIN_REDACTABLE_SECRET_LEN:
            values.add(value.strip())
    return tuple(sorted(values, key=len, reverse=True))


def redact(text: str, secrets: Sequence[str]) -> str:
    """Strip configured credentials out of tool output.

    Tools talk to Jenkins, GitHub and GitLab, and several echo response bodies back on
    error -- ``tools/source_code.py`` returns ``resp.text[:500]`` verbatim on a non-2xx.
    An echoed request URL or error body can carry the token that made the request, and
    from there it reaches the model's context and the stored investigation trace.
    """
    for secret in secrets:
        if secret in text:
            text = text.replace(secret, _REDACTED)
    return text


def bounded_tool_output(value: str, limit: int) -> str:
    """Elide the middle of an over-long result, keeping both ends.

    Head and tail are where the useful evidence sits in a console log -- the command and
    the traceback -- so a midpoint elision preserves more signal than a plain truncation.
    """
    if len(value) <= limit or limit <= 0:
        return value
    half = limit // 2
    return f"{value[:half]}\n... [tool output compacted] ...\n{value[-half:]}"


@dataclass
class ToolCallRecorder:
    """Collects executions as the agent makes them, for the trace and the progress events.

    The SDK executes tools on our behalf, so this is where the app still sees each call --
    the equivalent of the old loop appending to its own trace.

    It also owns redaction and the aggregate context budget. Once the SDK is driving, this
    wrapper is the last point the app controls before content enters a context window it
    no longer manages, so both have to live here or nowhere.
    """

    deep: bool = False
    tools_used: list[str] = field(default_factory=list)
    returned_chars: int = 0
    # Called with (bare tool name, succeeded) once execution finishes. The SSE chat paths
    # need the outcome, and this is the only place that actually knows it.
    on_result: Callable[[str, bool], Awaitable[None]] | None = None
    _secrets: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self._secrets:
            self._secrets = secret_values()

    def record(self, name: str) -> None:
        self.tools_used.append(name)

    def bound(self, output: str) -> str:
        """Redact, then cap what this result may add to the context.

        The cap is the *remaining* budget, not a per-result floor: a floor applied per call
        is not a bound at all, because N calls times any fixed floor still exhausts the
        window. Once less than one readable result remains, the content is withheld rather
        than handed over as an unusable fragment.
        """
        output = redact(output, self._secrets)
        remaining = _HISTORY_BUDGET[self.deep] - self.returned_chars
        if remaining < _RESULT_FLOOR[self.deep]:
            return (
                "[tool output withheld: this investigation's context budget is spent. "
                "Conclude from the evidence already gathered.]"
            )
        bounded = bounded_tool_output(output, remaining)
        self.returned_chars += len(bounded)
        return bounded


ToolInvoker = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


def _looks_like_tool_failure(output: str, name: str) -> bool:
    """Did this tool call fail?

    The tool layer signals failure by prefix, across two layers, and the previous chat
    paths got this wrong in both directions: they tested ``startswith("Error")``, which
    caught the handler errors but never matched either dispatcher string, so a crashed
    tool rendered in the UI as a success.
    """
    return output.startswith(
        (
            "Unknown tool:",
            f"Tool execution error ({name})",
            "Error ",  # handler-level: "Error getting ...", "Error listing ...", etc.
            "Failed to ",
        )
    )


def build_tool_server(
    recorder: ToolCallRecorder,
    scan_options: ScanOptions,
    *,
    definitions: Sequence[dict] | None = None,
) -> tuple[Any, ToolInvoker]:
    """Publish the read-only tool registry as an in-process MCP server.

    Returns the server config and the same invoker its handlers use, so a test can drive a
    tool call through the real path -- dispatch, redaction, the aggregate cap, the trace --
    without spawning a CLI.

    The registry's own JSON Schemas are reused verbatim (``_RAW_TOOLS`` is already
    Anthropic-native ``name``/``description``/``input_schema``), so the tool surface the
    model sees cannot drift from the surface the handlers expect.

    ``scan_options`` is passed explicitly rather than read from the ContextVar inside the
    handler. Tools now execute inside the SDK's own task, and ``tools/jenkins.py`` reads
    ``get_scan_options()`` to decide the 64KB full-log cap -- if that lookup saw the
    default instead of the active deep-scan options, deep scans would silently lose full
    build logs with nothing failing.
    """
    raw = list(definitions if definitions is not None else _RAW_TOOLS)

    async def invoke(name: str, args: dict[str, Any]) -> dict[str, Any]:
        arguments = {key: value for key, value in args.items() if value is not None}
        token = activate_scan_options(scan_options)
        try:
            output = await execute_tool(name, arguments)
        finally:
            reset_scan_options(token)
        recorder.record(name)
        # execute_tool never raises; every failure comes back as text, from two layers:
        # the dispatcher ("Unknown tool: ...", "Tool execution error (...)") and the
        # handlers themselves ("Error getting Jenkins job ...", "Failed to decode ...").
        # Flagging them with is_error lets the model try a different tool instead of
        # reasoning over an error blob as though it were evidence.
        #
        # "No builds found ..." and friends are deliberately NOT errors — an empty result
        # is a real answer, and marking it failed would push the model to retry a query
        # that correctly returned nothing.
        failed = _looks_like_tool_failure(output, name)
        if recorder.on_result is not None:
            await recorder.on_result(name, not failed)
        return {
            "content": [{"type": "text", "text": recorder.bound(output)}],
            "is_error": failed,
        }

    def _handler(name: str) -> Any:
        async def run(args: dict[str, Any]) -> dict[str, Any]:
            return await invoke(name, args)

        return run

    tools = [
        tool(
            str(definition["name"]),
            str(definition.get("description", "")),
            definition.get("input_schema") or {"type": "object", "properties": {}},
        )(_handler(str(definition["name"])))
        for definition in raw
    ]
    return create_sdk_mcp_server(name=MCP_SERVER_NAME, tools=tools), invoke


def allowed_tool_names(definitions: Sequence[dict] | None = None) -> list[str]:
    raw = definitions if definitions is not None else _RAW_TOOLS
    return [qualified_tool_name(str(d["name"])) for d in raw]


# --- the runtime ------------------------------------------------------------


class AgentSession:
    """One live CLI session. ``send`` may be called more than once to continue the turn."""

    def __init__(self, client: ClaudeSDKClient) -> None:
        self._client = client

    async def send(self, prompt: str) -> AsyncIterator[Any]:
        await self._client.query(prompt)
        async for message in self._client.receive_response():
            yield message


@asynccontextmanager
async def _default_session(options: ClaudeAgentOptions) -> AsyncIterator[AgentSession]:
    """The real transport. Tests inject a replacement so no CLI is ever spawned."""
    async with ClaudeSDKClient(options=options) as client:
        yield AgentSession(client)


async def _iter_with_idle_timeout(messages: AsyncIterator[Any], idle_seconds: float) -> AsyncIterator[Any]:
    """Yield messages, failing if none arrives within ``idle_seconds``.

    The window resets on every message, so a long investigation that keeps streaming is
    never cut off -- only a silent one is. Without this, a wedged CLI hangs the scan
    forever rather than failing one investigation.
    """
    iterator = messages.__aiter__()
    while True:
        try:
            if idle_seconds > 0:
                message = await asyncio.wait_for(iterator.__anext__(), timeout=idle_seconds)
            else:
                message = await iterator.__anext__()
        except StopAsyncIteration:
            return
        except TimeoutError as exc:
            raise TimeoutError(f"claude code produced no output for {idle_seconds:.0f}s") from exc
        yield message


@dataclass
class AgentTurn:
    """What one agent run produced, in the shape the reasoning layer already expects."""

    content: str = ""
    tools_used: list[str] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    # Every assistant text block in order, for callers that want the narration rather than
    # the conclusion.
    narration: tuple[str, ...] = ()


def _usage_from(result: ResultMessage | None) -> tuple[int, int]:
    """Map the CLI's Anthropic-shaped usage onto the adapter's field names."""
    raw = (result.usage if result is not None else None) or {}
    return int(raw.get("input_tokens") or 0), int(raw.get("output_tokens") or 0)


def _raise_for_result(result: ResultMessage | None) -> None:
    """Turn a failed turn into an exception the callers already know how to handle.

    A turn that errors still yields a ResultMessage, so silence here would look like an
    empty answer rather than a failure -- and an auth failure in particular has to be loud.
    """
    if result is None:
        raise RuntimeError("claude code returned no result message")
    if result.is_error:
        detail = "; ".join(result.errors or []) or result.result or result.subtype
        raise RuntimeError(f"claude code turn failed ({result.subtype}): {detail}"[:500])


class ClaudeCodeRuntime:
    """Spawns the `claude` CLI to answer a prompt, with or without tools.

    Concurrency is capped because every call is a subprocess: a scan can queue far more
    investigations than a pod can afford to run at once, and this pod already OOMKills.
    """

    def __init__(
        self,
        *,
        credentials: ClaudeCredentials | None = None,
        model: str = "",
        fallback_model: str = "",
        max_concurrent: int = 0,
        idle_timeout_s: float = 0.0,
        session_factory: Callable[[ClaudeAgentOptions], Any] | None = None,
    ) -> None:
        self._credentials = credentials or ClaudeCredentials.from_settings(settings)
        self._model = model or settings.llm_model
        self._fallback_model = fallback_model or settings.llm_fallback_model
        self._idle_timeout_s = idle_timeout_s or settings.llm_agent_idle_timeout_s
        # Built here, never at module scope: a module-level Semaphore binds to whichever
        # event loop happened to import it.
        self._semaphore = asyncio.Semaphore(max(1, max_concurrent or settings.llm_max_concurrent_agents))
        # Injectable so tests never spawn a real CLI.
        self._session_factory = session_factory or _default_session

    @property
    def configured(self) -> bool:
        return self._credentials.configured

    def ensure_ready(self) -> None:
        self._credentials.ensure_ready()

    def options(
        self,
        *,
        system_prompt: str,
        max_turns: int,
        mcp_servers: dict[str, Any] | None = None,
        allowed_tools: Sequence[str] | None = None,
        hooks: dict[str, Any] | None = None,
    ) -> ClaudeAgentOptions:
        kwargs: dict[str, Any] = {
            "system_prompt": system_prompt,
            "model": self._model,
            "max_turns": max_turns,
            # Scrub every credential from the subprocess and hand it the OAuth token; pin
            # CLAUDE_CONFIG_DIR so a bad token cannot fall back to another identity.
            "env": self._credentials.subprocess_env_overrides(),
            # Three isolation axes, all closed deliberately: only our MCP server, no
            # filesystem settings, and no built-in tools.
            "strict_mcp_config": True,
            # [] disables filesystem settings; None would load ALL of them (the SDK default
            # matches the CLI, so None is the opposite of isolation). Left at None, a dev
            # run would read this repo's own CLAUDE.md and settings into an incident
            # investigator's system prompt.
            "setting_sources": [],
            # Nothing here should read or write the filesystem or run commands; the only
            # capability the agent gets is our read-only registry. [] is a meaningful
            # value, so it is always passed -- never gated on truthiness.
            "tools": [],
            # Fail closed. With an explicit allowed_tools allowlist, a tool the model
            # invents is denied instead of executed, inside a pod holding Jenkins and SCM
            # credentials and driven by untrusted console logs.
            "permission_mode": "default",
        }
        if self._fallback_model:
            kwargs["fallback_model"] = self._fallback_model
        if mcp_servers:
            kwargs["mcp_servers"] = mcp_servers
        if allowed_tools:
            kwargs["allowed_tools"] = list(allowed_tools)
        if hooks:
            kwargs["hooks"] = hooks
        cli_path = self._credentials.resolved_cli_path()
        if cli_path:
            kwargs["cli_path"] = cli_path
        return ClaudeAgentOptions(**kwargs)

    async def complete(self, *, system_prompt: str, prompt: str) -> AgentTurn:
        """One tool-free turn. The whole answer is the assistant's text."""
        options = self.options(system_prompt=system_prompt, max_turns=1)
        parts: list[str] = []
        result: ResultMessage | None = None
        async with self._semaphore:
            async with self._session_factory(options) as session:
                stream = _iter_with_idle_timeout(session.send(prompt), self._idle_timeout_s)
                async for message in stream:
                    if isinstance(message, AssistantMessage):
                        parts.extend(b.text for b in message.content if isinstance(b, TextBlock))
                    elif isinstance(message, ResultMessage):
                        result = message
        _raise_for_result(result)
        text = (result.result or "").strip() if result is not None else ""
        if not text:
            text = "\n".join(p for p in parts if p).strip()
        prompt_tokens, completion_tokens = _usage_from(result)
        return AgentTurn(
            content=text,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=(result.total_cost_usd or 0.0) if result is not None else 0.0,
            narration=tuple(p.strip() for p in parts if p.strip()),
        )

    async def run_agent(
        self,
        *,
        system_prompt: str,
        prompt: str,
        scan_options: ScanOptions,
        max_turns: int,
        summary_prompt: str = "",
        on_text: Callable[[str], Awaitable[None]] | None = None,
        on_tool_call: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
        on_tool_result: Callable[[str, bool], Awaitable[None]] | None = None,
    ) -> AgentTurn:
        """Run the tool loop. The SDK owns the turns; we own the tools.

        Progress is emitted from the message stream rather than from a loop body we no
        longer have: a TextBlock is reasoning, a ToolUseBlock is a tool call. That keeps
        the events in the order they happened for free.
        """
        recorder = ToolCallRecorder(deep=scan_options.deep, on_result=on_tool_result)
        server, _ = build_tool_server(recorder, scan_options)
        options = self.options(
            system_prompt=system_prompt,
            max_turns=max_turns,
            mcp_servers={MCP_SERVER_NAME: server},
            allowed_tools=allowed_tool_names(),
            hooks={"PreToolUse": [HookMatcher(hooks=[_noop_gate])]},
        )

        parts: list[str] = []
        result: ResultMessage | None = None
        prompt_tokens = completion_tokens = 0
        cost = 0.0

        async def drain(text: str) -> ResultMessage | None:
            last: ResultMessage | None = None
            stream = _iter_with_idle_timeout(session.send(text), self._idle_timeout_s)
            async for message in stream:
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock) and block.text.strip():
                            parts.append(block.text.strip())
                            if on_text is not None:
                                await on_text(block.text.strip())
                        elif isinstance(block, ToolUseBlock):
                            if on_tool_call is not None:
                                await on_tool_call(plain_tool_name(block.name), dict(block.input or {}))
                elif isinstance(message, ResultMessage):
                    last = message
            return last

        async with self._semaphore:
            async with self._session_factory(options) as session:
                result = await drain(prompt)
                _raise_for_result(result)
                prompt_tokens, completion_tokens = _usage_from(result)
                cost = result.total_cost_usd or 0.0 if result is not None else 0.0

                # The SDK stops at max_turns without necessarily concluding. The old loop
                # handled this by appending a summary prompt and doing one final tool-free
                # call; the equivalent here is a second turn on the same live session.
                if summary_prompt and _hit_turn_limit(result):
                    logger.warning("Agent hit max_turns (%d) — asking for a summary", max_turns)
                    follow_up = await drain(summary_prompt)
                    _raise_for_result(follow_up)
                    extra_prompt, extra_completion = _usage_from(follow_up)
                    prompt_tokens += extra_prompt
                    completion_tokens += extra_completion
                    cost += (follow_up.total_cost_usd or 0.0) if follow_up is not None else 0.0
                    result = follow_up

        # The final answer is ResultMessage.result, NOT every assistant block joined: the
        # agent narrates as it explores ("I will inspect the build..."), and that planning
        # must never reach an operator or the assessment extractor.
        final = (result.result or "").strip() if result is not None else ""
        if not final:
            final = parts[-1] if parts else ""
        return AgentTurn(
            content=final,
            tools_used=list(recorder.tools_used),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost,
            narration=tuple(parts),
        )


def _hit_turn_limit(result: ResultMessage | None) -> bool:
    if result is None:
        return False
    reason = (result.stop_reason or "") or (result.subtype or "")
    return "max_turns" in reason or "turn_limit" in reason


async def _noop_gate(payload: Any, tool_use_id: str | None, context: Any) -> dict:
    """A PreToolUse hook, deliberately NOT can_use_tool.

    ``can_use_tool`` is only invoked for calls that would otherwise prompt, and every tool
    here is pre-permitted via ``allowed_tools``, so it would never fire -- the SDK ships an
    explicit warning about exactly this shadowing. A hook sees every call, which is the
    seam a future budget or policy gate needs.
    """
    del payload, tool_use_id, context
    return {}
