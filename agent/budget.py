"""What a run may spend, shared by all of its tasks.

Per-task limits — wall clock and steps — are enforced where the task runs. This module is about the
ceiling the whole run shares, and it exists because concurrency without a shared ceiling turns a
cost limit into a suggestion: four sessions each inside its own budget can still spend four times
what anyone agreed to.

Reaching the ceiling never turns into a pass. A task that was not started is recorded as
`exhausted`, exactly like one that ran out of time, so the run says the check did not happen.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from agent.backends.port import Usage


@dataclass(frozen=True, slots=True)
class RunBudget:
    """The run's shared ceiling.

    `tokens` is optional because it can only be enforced where the backend reports usage. A money
    ceiling is deliberately absent: it needs a price per model, and a limit computed from prices
    nobody maintains would refuse work for a number that is wrong.
    """

    max_parallel: int = 4
    tokens: int | None = None


@dataclass(slots=True)
class Spend:
    """What sessions have reported so far.

    Sessions that report nothing are counted separately rather than as zero: silence is not evidence
    of a cheap run, and a total that hides it would make the ceiling meaningless.
    """

    tokens: int = 0
    accounted_sessions: int = 0
    unaccounted_sessions: int = 0

    def as_json(self) -> dict[str, object]:
        return {
            "tokens": self.tokens,
            "accounted_sessions": self.accounted_sessions,
            "unaccounted_sessions": self.unaccounted_sessions,
        }


class Ledger:
    """Tracks spend and answers whether another task may start.

    Guarded by a lock because tasks run concurrently: two tasks reading the total at the same moment
    could each conclude there was room for one more.
    """

    def __init__(self, budget: RunBudget) -> None:
        self.budget = budget
        self.spend = Spend()
        self._lock = asyncio.Lock()

    async def may_start(self) -> bool:
        async with self._lock:
            if self.budget.tokens is None:
                return True
            return self.spend.tokens < self.budget.tokens

    async def record(self, usage: Usage) -> None:
        async with self._lock:
            if not usage.known:
                self.spend.unaccounted_sessions += 1
                return
            self.spend.accounted_sessions += 1
            if usage.total_tokens is not None:
                self.spend.tokens += usage.total_tokens

    def exhausted_detail(self) -> str:
        return (
            f"the run's token budget of {self.budget.tokens} is spent "
            f"({self.spend.tokens} used), so this task was not started"
        )
