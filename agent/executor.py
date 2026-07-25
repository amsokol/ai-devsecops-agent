"""Running the planned tasks through a backend and turning what comes back into outcomes.

This is where the run's honesty is enforced. A task that produced nothing usable is recorded as not
having run, never as having found nothing, and the difference is what keeps the gate from being
switched off by anyone who can break a tool or exhaust a budget.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from agent.backends.port import Brief, Budget, Failure, SessionResult
from agent.backends.select import Roster
from agent.brief import compose, digest, knowledge_for, role_instructions
from agent.budget import Ledger, RunBudget
from agent.domain import Outcome, Plan, PlannedTask, Reason
from agent.evidence import EvidenceStore
from agent.findings import Finding
from agent.library import Library
from agent.results import InvalidResult, TaskResult, read_result
from agent.toolkit import Toolkit, Toolkits
from agent.verdict import TaskOutcome

MAX_ATTEMPTS = 2
"""One retry, as the contract requires. A second would spend budget on a model that has already
shown it cannot produce a valid result, and delay the honest `unverified` that follows."""


@dataclass(slots=True)
class Attempt:
    number: int
    session: SessionResult
    prompt_digest: str
    result_path: Path
    rejected: str = ""

    def as_json(self) -> dict[str, object]:
        return self.session.as_json() | {
            "attempt": self.number,
            "prompt_digest": self.prompt_digest,
            "result_path": str(self.result_path),
            "rejected": self.rejected,
        }


@dataclass(slots=True)
class Executed:
    """What one task produced: its outcome, its findings, and the attempts behind them."""

    task: PlannedTask
    outcome: TaskOutcome
    result: TaskResult | None = None
    attempts: list[Attempt] = field(default_factory=list)
    calls: list[dict[str, object]] = field(default_factory=list)
    """Every tool call the task made, so a reader can see how a fact was obtained."""

    @property
    def findings(self) -> tuple[Finding, ...]:
        return self.result.findings if self.result else ()


async def execute(
    plan: Plan,
    *,
    roster: Roster,
    library: Library,
    notes: str,
    evidence: EvidenceStore,
    tasks_dir: Path,
    budget: Budget,
    toolkits: Toolkits,
    run_budget: RunBudget | None = None,
    ledger: Ledger | None = None,
) -> list[Executed]:
    """Run the planned tasks concurrently, up to the run budget's parallelism.

    Analysis tasks are independent, so they overlap. Two properties are preserved regardless of how
    they interleave: results come back in plan order, so a report does not reshuffle between runs;
    and a task the shared budget could not afford is recorded as `exhausted` rather than skipped, so
    nothing that was never attempted can be mistaken for a check that passed.
    """
    if not plan.tasks:
        return []

    accounting = ledger or Ledger(run_budget or RunBudget())
    slots = asyncio.Semaphore(accounting.budget.max_parallel)
    results: list[Executed | None] = [None] * len(plan.tasks)

    async def run_one(index: int, task: PlannedTask) -> None:
        async with slots:
            # Checked after the slot is taken, not before: while this task waited its turn, the
            # tasks ahead of it may have spent what was left.
            if not await accounting.may_start():
                results[index] = _not_afforded(task, accounting.exhausted_detail())
                return
            executed = await _execute_one(
                task,
                roster=roster,
                library=library,
                notes=notes,
                evidence=evidence,
                tasks_dir=tasks_dir,
                budget=budget,
                toolkits=toolkits,
            )
            for attempt in executed.attempts:
                await accounting.record(attempt.session.usage)
            results[index] = executed

    await asyncio.gather(*(run_one(index, task) for index, task in enumerate(plan.tasks)))
    return [item for item in results if item is not None]


async def _execute_one(
    task: PlannedTask,
    *,
    roster: Roster,
    library: Library,
    notes: str,
    evidence: EvidenceStore,
    tasks_dir: Path,
    budget: Budget,
    toolkits: Toolkits,
) -> Executed:
    instructions = role_instructions(task.role)
    knowledge = knowledge_for(library, task)
    # One toolkit for the task, not per attempt: a fact established before a result was rejected is
    # still a fact, and the retry can cite it instead of paying for the call again.
    toolkit = toolkits.for_task(task, step_limit=budget.steps)
    executed = Executed(task=task, outcome=_unverified(task, Reason.UNAVAILABLE))
    rejection = ""
    try:
        return await _attempts(
            task,
            executed=executed,
            roster=roster,
            instructions=instructions,
            knowledge=knowledge,
            notes=notes,
            evidence=evidence,
            tasks_dir=tasks_dir,
            budget=budget,
            toolkit=toolkit,
            rejection=rejection,
        )
    finally:
        # Recorded whatever happened, including for a task that failed: how a fact was obtained is
        # exactly what a reader needs when the answer is surprising.
        executed.calls = toolkit.as_json()


@dataclass(slots=True)
class Attempted[T]:
    """The sessions one task took, and what came out of the last of them."""

    attempts: list[Attempt] = field(default_factory=list)
    parsed: T | None = None
    failure: Failure | None = None
    rejected: str = ""
    """Why the last result file was refused, when no valid one arrived."""


async def run_attempts[T](
    task: PlannedTask,
    *,
    roster: Roster,
    tasks_dir: Path,
    budget: Budget,
    toolkit: Toolkit,
    prompt_for: Callable[[int, str, Path], str],
    parse: Callable[[Path], T],
) -> Attempted[T]:
    """Run one task until it produces a valid result, or until the one retry is used up.

    Shared by analysis and by fixing, because the retry rule is contract behaviour rather than a
    detail of either: one more attempt when the result file was refused or the session never
    started, and then an honest failure. Two copies of this loop would eventually disagree about how
    many attempts a task gets, and the manifest would stop meaning the same thing between kinds.
    """
    attempted: Attempted[T] = Attempted()
    rejection = ""
    for number in range(1, MAX_ATTEMPTS + 1):
        directory = tasks_dir / task.id / f"attempt-{number}"
        result_path = directory / "result.json"
        prompt = prompt_for(number, rejection, result_path)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "prompt.md").write_text(prompt, encoding="utf-8")

        session = await roster.for_role(task.role).execute(
            Brief(
                task=task,
                prompt=prompt,
                result_path=result_path,
                workspace=directory,
                budget=budget,
                toolkit=toolkit,
                attempt=number,
            )
        )
        attempt = Attempt(
            number=number, session=session, prompt_digest=digest(prompt), result_path=result_path
        )
        attempted.attempts.append(attempt)

        if session.failure is not None:
            attempted.failure = session.failure
            if session.failure is Failure.NOT_STARTED and number < MAX_ATTEMPTS:
                # Nothing was spent and nothing was decided: a session that never started is an
                # environment problem, and one more attempt is cheaper than an inconclusive run.
                rejection = f"the previous session did not start: {session.detail}"
                continue
            return attempted

        try:
            attempted.parsed = parse(result_path)
        except InvalidResult as error:
            attempt.rejected = str(error)
            rejection = str(error)
            attempted.rejected = str(error)
            if number < MAX_ATTEMPTS:
                continue
            return attempted
        attempted.failure = None
        attempted.rejected = ""
        return attempted

    return attempted


async def _attempts(
    task: PlannedTask,
    *,
    executed: Executed,
    roster: Roster,
    instructions: str,
    knowledge: tuple[tuple[str, str], ...],
    notes: str,
    evidence: EvidenceStore,
    tasks_dir: Path,
    budget: Budget,
    toolkit: Toolkit,
    rejection: str,
) -> Executed:
    def prompt_for(number: int, refused: str, result_path: Path) -> str:
        return compose(
            task=task,
            instructions=instructions,
            knowledge=knowledge,
            notes=notes,
            result_path=result_path,
            tools=tuple((tool.name, tool.description) for tool in toolkit.tools()),
            attempt=number,
            invalid_reason=refused or rejection,
            given=toolkit.caveats,
        )

    attempted = await run_attempts(
        task,
        roster=roster,
        tasks_dir=tasks_dir,
        budget=budget,
        toolkit=toolkit,
        prompt_for=prompt_for,
        parse=lambda path: read_result(
            path,
            capability=task.capability,
            known_evidence=evidence.keys(),
            ecosystem=task.ecosystem,
        ),
    )
    executed.attempts = attempted.attempts
    if attempted.parsed is not None:
        executed.result = attempted.parsed
        executed.outcome = TaskOutcome(
            id=task.id,
            capability=task.capability,
            required=task.required,
            outcome=attempted.parsed.outcome,
            reason=attempted.parsed.reason,
        )
    elif attempted.failure is not None:
        executed.outcome = _from_failure(task, attempted.failure)
    else:
        executed.outcome = _unverified(task, Reason.INVALID_RESULT)
    return executed


def _not_afforded(task: PlannedTask, detail: str) -> Executed:
    """A task the shared budget could not pay for: no session, no findings, exhausted.

    Recorded as an attempt with no model behind it, so the manifest shows why the task is missing
    instead of leaving a reader to guess that the plan changed.
    """
    return Executed(
        task=task,
        outcome=TaskOutcome(
            id=task.id,
            capability=task.capability,
            required=task.required,
            outcome=Outcome.EXHAUSTED,
            reason=Reason.EXHAUSTED,
        ),
        attempts=[
            Attempt(
                number=0,
                session=SessionResult(
                    backend="none",
                    model="",
                    duration_ms=0,
                    failure=Failure.EXHAUSTED,
                    detail=detail,
                ),
                prompt_digest="",
                result_path=Path(),
            )
        ],
    )


def _from_failure(task: PlannedTask, failure: Failure) -> TaskOutcome:
    ran_out = failure in {Failure.TIMED_OUT, Failure.EXHAUSTED}
    outcome = Outcome.EXHAUSTED if ran_out else Outcome.UNVERIFIED
    return TaskOutcome(
        id=task.id,
        capability=task.capability,
        required=task.required,
        outcome=outcome,
        reason=failure.reason,
    )


def _unverified(task: PlannedTask, reason: Reason) -> TaskOutcome:
    return TaskOutcome(
        id=task.id,
        capability=task.capability,
        required=task.required,
        outcome=Outcome.UNVERIFIED,
        reason=reason,
    )
