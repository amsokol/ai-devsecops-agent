"""Running the planned tasks through a backend and turning what comes back into outcomes.

This is where the run's honesty is enforced. A task that produced nothing usable is recorded as not
having run, never as having found nothing, and the difference is what keeps the gate from being
switched off by anyone who can break a tool or exhaust a budget.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from agent.backends.port import Backend, Brief, Budget, Failure, SessionResult
from agent.brief import compose, digest, knowledge_for, role_instructions
from agent.domain import Outcome, Plan, PlannedTask, Reason
from agent.errors import ConfigError
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
    backend: Backend,
    library: Library,
    notes: str,
    evidence: EvidenceStore,
    tasks_dir: Path,
    budget: Budget,
    toolkits: Toolkits,
) -> list[Executed]:
    """Run every planned task in order.

    Sequential for now. Concurrency belongs with budgets: running four sessions at once without a
    run budget that they share turns a cost ceiling into a suggestion, so the two arrive together.
    """
    return [
        await _execute_one(
            task,
            backend=backend,
            library=library,
            notes=notes,
            evidence=evidence,
            tasks_dir=tasks_dir,
            budget=budget,
            toolkits=toolkits,
        )
        for task in plan.tasks
    ]


async def _execute_one(
    task: PlannedTask,
    *,
    backend: Backend,
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
    toolkit = toolkits.for_task(task)
    executed = Executed(task=task, outcome=_unverified(task, Reason.UNAVAILABLE))
    rejection = ""
    try:
        return await _attempts(
            task,
            executed=executed,
            backend=backend,
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


async def _attempts(
    task: PlannedTask,
    *,
    executed: Executed,
    backend: Backend,
    instructions: str,
    knowledge: tuple[tuple[str, str], ...],
    notes: str,
    evidence: EvidenceStore,
    tasks_dir: Path,
    budget: Budget,
    toolkit: Toolkit,
    rejection: str,
) -> Executed:
    for number in range(1, MAX_ATTEMPTS + 1):
        directory = tasks_dir / task.id / f"attempt-{number}"
        result_path = directory / "result.json"
        prompt = compose(
            task=task,
            instructions=instructions,
            knowledge=knowledge,
            notes=notes,
            result_path=result_path,
            tools=tuple((tool.name, tool.description) for tool in toolkit.tools()),
            attempt=number,
            invalid_reason=rejection,
        )
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "prompt.md").write_text(prompt, encoding="utf-8")

        session = await backend.execute(
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
        executed.attempts.append(attempt)

        if session.failure is not None:
            executed.outcome = _from_failure(task, session.failure)
            if session.failure is Failure.NOT_STARTED and number < MAX_ATTEMPTS:
                # Nothing was spent and nothing was decided: a session that never started is an
                # environment problem, and one more attempt is cheaper than an inconclusive run.
                rejection = f"the previous session did not start: {session.detail}"
                continue
            return executed

        try:
            result = read_result(
                result_path, capability=task.capability, known_evidence=evidence.keys()
            )
        except InvalidResult as error:
            attempt.rejected = str(error)
            rejection = str(error)
            if number < MAX_ATTEMPTS:
                continue
            executed.outcome = _unverified(task, Reason.INVALID_RESULT)
            return executed

        executed.result = result
        executed.outcome = TaskOutcome(
            id=task.id,
            capability=task.capability,
            required=task.required,
            outcome=result.outcome,
            reason=result.reason,
        )
        return executed

    return executed


def _from_failure(task: PlannedTask, failure: Failure) -> TaskOutcome:
    outcome = Outcome.EXHAUSTED if failure is Failure.TIMED_OUT else Outcome.UNVERIFIED
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


def budget_from(raw: dict[str, object], *, where: Path) -> Budget:
    seconds = raw.get("task_seconds", 600)
    steps = raw.get("task_steps")
    if not isinstance(seconds, int) or seconds <= 0:
        raise ConfigError(f"{where}: task_seconds must be a positive integer")
    if steps is not None and (not isinstance(steps, int) or steps <= 0):
        raise ConfigError(f"{where}: task_steps must be a positive integer when set")
    return Budget(seconds=seconds, steps=steps)
