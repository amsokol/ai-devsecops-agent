"""A backend that runs no model.

Not a convenience for tests: it is the only way to assert what the core does when a session fails,
returns nothing, or returns nonsense. Those paths decide whether the gate is fail-closed, and
proving them against a live model would be slow, expensive and nondeterministic — so the
properties that matter most would end up the least tested.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agent.backends.port import Brief, Failure, Session, Usage

MODEL = "fake"


@dataclass
class Scripted:
    """One scripted answer: what to write to the result file, and how the session ended."""

    result: dict[str, Any] | str | None = None
    failure: Failure | None = None
    detail: str = ""
    usage: Usage = field(default_factory=lambda: Usage(known=True, total_tokens=1000))


class FakeBackend:
    """Answers by task id, falling back to a default. Records every brief it was given."""

    name = "fake"

    def __init__(
        self,
        answers: dict[str, Scripted] | None = None,
        default: Scripted | None = None,
        on_execute: Callable[[Brief], None] | None = None,
    ) -> None:
        self.answers = answers or {}
        self.default = default or Scripted(result={"outcome": "clean", "findings": []})
        self.briefs: list[Brief] = []
        self._on_execute = on_execute

    async def execute(self, brief: Brief) -> Session:
        self.briefs.append(brief)
        if self._on_execute is not None:
            self._on_execute(brief)
        scripted = self.answers.get(brief.task.id, self.default)
        if scripted.result is not None:
            brief.result_path.parent.mkdir(parents=True, exist_ok=True)
            text = (
                scripted.result
                if isinstance(scripted.result, str)
                else json.dumps(scripted.result, ensure_ascii=False)
            )
            brief.result_path.write_text(text, encoding="utf-8")
        return Session(
            backend=self.name,
            model=MODEL,
            duration_ms=1,
            usage=scripted.usage,
            failure=scripted.failure,
            detail=scripted.detail,
        )

    async def close(self) -> None:
        return None
