"""The Cursor SDK adapter.

The tools are passed as in-process callables, so a tool call runs inside this process against the
run's own evidence store, cache and allowlists. Nothing about the run's state has to be serialised,
and there is no port and no token to leak.

Ambient configuration is deliberately not loaded. A gate whose behaviour depends on the settings of
whoever happens to run it cannot say what it checked, and in CI those settings are nobody's.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable, Mapping
from typing import Any

from cursor_sdk import (
    AsyncAgent,
    AsyncClient,
    CursorAgentError,
    CustomTool,
    CustomToolContext,
    LocalAgentOptions,
    SandboxOptions,
)

from agent.backends.port import Brief, Failure, SessionResult, Usage
from agent.errors import ConfigError
from agent.toolkit import Refused, Toolkit

API_KEY_VARIABLE = "CURSOR_API_KEY"


class CursorBackend:
    """One bridge for the whole run, one agent per task.

    A task gets its own agent because the conversations must not see each other: a review comment
    from one capability leaking into another's context is how a finding acquires a rationale nobody
    can trace.
    """

    name = "cursor"

    def __init__(self, *, model: str, sandbox: bool = True, api_key: str | None = None) -> None:
        self.model = model
        self.sandbox = sandbox
        # Read once, at construction: discovering a missing key after the plan has been built wastes
        # the run and reports it as a task failure rather than as the configuration error it is.
        self.api_key = api_key or os.environ.get(API_KEY_VARIABLE)
        if not self.api_key:
            raise ConfigError(
                f"{API_KEY_VARIABLE} is not set, so the cursor backend cannot start a session"
            )
        self._client: AsyncClient | None = None
        self._lock = asyncio.Lock()

    async def execute(self, brief: Brief) -> SessionResult:
        started = time.monotonic()
        try:
            client = await self._bridge(brief)
        except (CursorAgentError, OSError) as error:
            return self._failed(Failure.NOT_STARTED, error, started)

        try:
            agent = await client.agents.create(
                model=self.model,
                api_key=self.api_key,
                local=LocalAgentOptions(
                    cwd=str(brief.workspace),
                    setting_sources=[],
                    sandbox_options=SandboxOptions(enabled=self.sandbox),
                    custom_tools=_custom_tools(brief.toolkit),
                ),
            )
        except CursorAgentError as error:
            return self._failed(Failure.NOT_STARTED, error, started)

        try:
            return await self._converse(agent, brief, started)
        finally:
            await agent.close()

    async def _converse(self, agent: AsyncAgent, brief: Brief, started: float) -> SessionResult:
        try:
            run = await agent.send(brief.prompt)
        except CursorAgentError as error:
            return self._failed(Failure.NOT_STARTED, error, started)

        try:
            result = await asyncio.wait_for(run.wait(), timeout=brief.budget.seconds)
        except TimeoutError:
            # Cancelling matters: an abandoned session keeps spending on a budget this run has
            # already given up on.
            if run.status == "running":
                await run.cancel()
            return SessionResult(
                backend=self.name,
                model=self.model,
                duration_ms=_elapsed(started),
                failure=Failure.TIMED_OUT,
                detail=f"the task exceeded {brief.budget.seconds}s",
            )
        except CursorAgentError as error:
            return self._failed(Failure.FAILED, error, started)

        failure = _failure_of(str(result.status))
        return SessionResult(
            backend=self.name,
            model=str(result.model.id) if result.model else self.model,
            duration_ms=result.duration_ms or _elapsed(started),
            usage=_usage(result.usage),
            failure=failure,
            detail="" if failure is None else f"run {run.id} ended as {result.status}",
            transcript=result.result or "",
        )

    async def _bridge(self, brief: Brief) -> AsyncClient:
        async with self._lock:
            if self._client is None:
                self._client = await AsyncClient.launch_bridge(workspace=str(brief.workspace))
            return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _failed(self, failure: Failure, error: Exception, started: float) -> SessionResult:
        detail = str(error)
        if "sandboxing is not supported" in detail.lower():
            # The shipped default asks for a sandbox. When the machine cannot provide one, every
            # session fails the same way, and the SDK's wording does not say which file to change.
            detail = (
                "this environment cannot provide the Cursor local sandbox. Set `sandbox: false` "
                "under `backends: cursor:` in agent/config/backends.yaml (or a --config-dir copy "
                "of it) to run without it — a deliberate decision, not a silent downgrade. "
                "Original: "
                f"{error}"
            )
        return SessionResult(
            backend=self.name,
            model=self.model,
            duration_ms=_elapsed(started),
            failure=failure,
            detail=detail,
        )


def _custom_tools(toolkit: Toolkit) -> dict[str, CustomTool]:
    """Expose the registry as SDK tools, one thin wrapper each.

    A refusal is returned to the model as an error rather than raised: it has to be able to see that
    a host is not allowed and record a gap, instead of the session dying on a tool call.
    """

    def wrap(name: str) -> Callable[[Mapping[str, Any], CustomToolContext], dict[str, Any]]:
        def execute(arguments: Mapping[str, Any], _context: CustomToolContext) -> dict[str, Any]:
            try:
                return toolkit.call(name, dict(arguments or {}))
            except Refused as refusal:
                return {"error": str(refusal)}

        return execute

    return {
        tool.name: CustomTool(
            execute=wrap(tool.name), description=tool.description, input_schema=tool.schema
        )
        for tool in toolkit.tools()
    }


def _failure_of(status: str) -> Failure | None:
    match status:
        case "finished":
            return None
        case "cancelled":
            return Failure.CANCELLED
        case "expired":
            return Failure.TIMED_OUT
        case _:
            return Failure.FAILED


def _usage(usage: Any) -> Usage:
    if usage is None:
        return Usage()
    return Usage(
        known=True,
        input_tokens=getattr(usage, "input_tokens", None),
        output_tokens=getattr(usage, "output_tokens", None),
        total_tokens=getattr(usage, "total_tokens", None),
    )


def _elapsed(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
