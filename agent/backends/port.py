"""The port every agent SDK is reached through.

Deliberately narrow: one call that executes one task and returns what happened. Everything specific
to an SDK stays behind it, so the second adapter inherits the core rather than reimplementing it,
and a fake adapter makes the whole pipeline testable without a model.

Nothing here knows about findings or verdicts. A backend runs a session and reports how it ended;
the result itself arrives as a file, validated by the core.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from agent.domain import PlannedTask, Reason
from agent.toolkit import Toolkit


class Failure(StrEnum):
    """How a session failed, in terms the core can act on.

    `NOT_STARTED` is kept apart from `FAILED` because the two need opposite responses: a session
    that never started is an environment problem worth retrying, while one that started and failed
    has already consumed budget and produced a transcript to inspect.
    """

    NOT_STARTED = "not-started"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed-out"

    @property
    def reason(self) -> Reason:
        match self:
            case Failure.NOT_STARTED | Failure.FAILED | Failure.CANCELLED:
                return Reason.UNAVAILABLE
            case Failure.TIMED_OUT:
                return Reason.EXHAUSTED


@dataclass(frozen=True, slots=True)
class Budget:
    """What one task may spend, enforced by the backend that runs it."""

    seconds: int
    steps: int | None = None


@dataclass(frozen=True, slots=True)
class Usage:
    """What a session cost. `known` is false when the backend reports nothing.

    An unknown cost is recorded as unknown rather than as zero: a budget that silently treats
    missing accounting as free spending is not a budget.
    """

    known: bool = False
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None

    def as_json(self) -> dict[str, object]:
        return {
            "known": self.known,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass(frozen=True, slots=True)
class Brief:
    """Everything a subagent is given: its task, its prompt, where to write, what it may spend.

    The prompt already contains the knowledge slice as text rather than as paths. A subagent able to
    open the library itself could read documents the plan did not select, and the point of a slice
    is that a task sees the rules for its own job and nothing else.
    """

    task: PlannedTask
    prompt: str
    result_path: Path

    workspace: Path
    """The only directory the backend's own file and shell tools may see.

    Deliberately the task's own directory and not the repository. An SDK brings its own tools, and
    a subagent that read the repository through them would bypass the never-send list and produce
    facts with no call behind them. The repository is reachable only through `toolkit`. The result
    file is written here, which is the one thing those native tools are for.
    """

    budget: Budget
    toolkit: Toolkit
    attempt: int = 1


@dataclass(frozen=True, slots=True)
class SessionResult:
    """What a backend reports back. The result itself is in `Brief.result_path`, not here."""

    backend: str
    model: str
    duration_ms: int
    usage: Usage = field(default_factory=Usage)
    failure: Failure | None = None
    detail: str = ""
    transcript: str = ""

    @property
    def ok(self) -> bool:
        return self.failure is None

    def as_json(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "model": self.model,
            "duration_ms": self.duration_ms,
            "usage": self.usage.as_json(),
            "failure": self.failure.value if self.failure else None,
            "detail": self.detail,
        }


class Backend(Protocol):
    """One agent SDK, reduced to the one thing the core needs from it."""

    name: str

    async def execute(self, brief: Brief) -> SessionResult: ...

    async def close(self) -> None: ...
