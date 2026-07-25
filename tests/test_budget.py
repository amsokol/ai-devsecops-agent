"""Concurrency and budgets: what a run does when it cannot afford everything it planned.

The properties asserted here are the ones a cost ceiling is worthless without: that overlapping
sessions stay under the limit, that the report does not reshuffle because two tasks finished in a
different order, and that a task nobody paid for is never mistaken for a check that passed.
"""

from __future__ import annotations

import asyncio
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from agent.backends import Brief, Budget, Failure, FakeBackend, Scripted, SessionResult
from agent.backends.port import Usage
from agent.budget import Ledger, RunBudget
from agent.config import BUILTIN_CONFIG_DIR, Config
from agent.domain import Outcome, Plan, PlannedTask, Reason, Role, RunResult, Trigger
from agent.errors import ConfigError
from agent.evidence import EvidenceStore
from agent.executor import Executed, execute
from agent.library import Library
from agent.session import Session
from agent.storage import FactCache
from agent.toolkit import Refused, Toolkit, Toolkits
from agent.tools import Grants
from agent.verdict import decide

BUDGET = Budget(seconds=60)
MOMENT = datetime(2026, 7, 25, tzinfo=UTC)
CAPABILITIES = ("deps-vuln", "deps-outdated", "code-vuln", "code-quality")
CLEAN: dict[str, object] = {"outcome": "clean", "findings": []}


def costing(tokens: int | None) -> Scripted:
    """A session that answers `clean` and reports the given cost, or reports none at all."""
    usage = Usage(known=tokens is not None, total_tokens=tokens)
    return Scripted(result=CLEAN, usage=usage)


class Overlapping:
    """A backend that actually yields, so concurrency is observable, and counts how many overlap."""

    name = "fake"

    def __init__(self, inner: FakeBackend, *, hold_seconds: float = 0.02) -> None:
        self.inner = inner
        self.hold_seconds = hold_seconds
        self.active = 0
        self.peak = 0
        self.finished: list[str] = []

    async def execute(self, brief: Brief) -> SessionResult:
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.hold_seconds)
            return await self.inner.execute(brief)
        finally:
            self.active -= 1
            self.finished.append(brief.task.id)

    async def close(self) -> None:
        await self.inner.close()


def plan_of(*capabilities: str) -> Plan:
    return Plan(
        playbook="playbooks/pr-review",
        trigger=Trigger.CHANGE_OPENED,
        tasks=tuple(
            PlannedTask(
                id=name,
                capability=f"capabilities/{name}",
                role=Role.ANALYST,
                required=True,
                ecosystem="ecosystems/python-uv",
                knowledge=(f"capabilities/{name}",),
            )
            for name in capabilities
        ),
    )


def run(
    backend: Overlapping | FakeBackend,
    plan: Plan,
    library: Library,
    tmp_path: Path,
    *,
    ledger: Ledger,
) -> list[Executed]:
    store = EvidenceStore()
    session = Session(
        repository=tmp_path,
        grants=Grants(binaries=frozenset(), hosts=frozenset()),
        cache=FactCache(None, writable=False),
        scratch_root=tmp_path / "scratch",
    )
    session.evidence = store
    return asyncio.run(
        execute(
            plan,
            backend=backend,
            library=library,
            notes="",
            evidence=store,
            tasks_dir=tmp_path / "tasks",
            budget=BUDGET,
            toolkits=Toolkits(session=session, now=MOMENT, quarantine_days=7),
            ledger=ledger,
        )
    )


def test_sessions_overlap_but_never_beyond_the_limit(library: Library, tmp_path: Path) -> None:
    backend = Overlapping(FakeBackend())
    executed = run(
        backend,
        plan_of(*CAPABILITIES),
        library,
        tmp_path,
        ledger=Ledger(RunBudget(max_parallel=2)),
    )
    assert backend.peak == 2
    assert len(executed) == len(CAPABILITIES)


def test_results_come_back_in_plan_order(library: Library, tmp_path: Path) -> None:
    """Whatever order sessions finish in, the report must not depend on it."""
    backend = Overlapping(FakeBackend())
    executed = run(
        backend, plan_of(*CAPABILITIES), library, tmp_path, ledger=Ledger(RunBudget(max_parallel=4))
    )
    assert [item.task.id for item in executed] == list(CAPABILITIES)


def test_a_spent_run_budget_leaves_the_rest_exhausted(library: Library, tmp_path: Path) -> None:
    backend = FakeBackend(default=costing(1000))
    ledger = Ledger(RunBudget(max_parallel=1, tokens=1500))
    executed = run(backend, plan_of(*CAPABILITIES), library, tmp_path, ledger=ledger)

    ran, refused = executed[:2], executed[2:]
    assert [item.outcome.outcome for item in ran] == [Outcome.CLEAN, Outcome.CLEAN]
    assert all(item.outcome.outcome is Outcome.EXHAUSTED for item in refused)
    assert all(item.outcome.reason is Reason.EXHAUSTED for item in refused)
    # An exhausted task must be explainable: which budget, and that no model was involved.
    session = refused[0].attempts[0].session
    assert session.failure is Failure.EXHAUSTED
    assert session.backend == "none"
    assert "1500" in session.detail
    assert ledger.spend.tokens == 2000


def test_a_required_exhausted_task_refuses_the_merge(library: Library, tmp_path: Path) -> None:
    """The whole point of the `exhausted` outcome: unaffordable is not the same as fine."""
    backend = FakeBackend(default=costing(1000))
    executed = run(
        backend,
        plan_of(*CAPABILITIES),
        library,
        tmp_path,
        ledger=Ledger(RunBudget(max_parallel=1, tokens=500)),
    )
    verdict = decide(tuple(item.outcome for item in executed), ())
    assert verdict.result is RunResult.INCONCLUSIVE


def test_a_session_that_reports_nothing_is_not_free(library: Library, tmp_path: Path) -> None:
    """Unknown usage cannot be counted as zero, so it is counted separately and said out loud."""
    backend = FakeBackend(default=costing(None))
    ledger = Ledger(RunBudget(max_parallel=2, tokens=100))
    executed = run(backend, plan_of(*CAPABILITIES), library, tmp_path, ledger=ledger)
    assert all(item.outcome.outcome is Outcome.CLEAN for item in executed)
    assert ledger.spend.tokens == 0
    assert ledger.spend.unaccounted_sessions == len(CAPABILITIES)
    assert ledger.spend.accounted_sessions == 0


def test_the_step_budget_stops_a_task_and_tells_it_to_report(tmp_path: Path) -> None:
    session = Session(
        repository=tmp_path,
        grants=Grants(binaries=frozenset(), hosts=frozenset()),
        cache=FactCache(None, writable=False),
        scratch_root=tmp_path / "scratch",
    )
    task = PlannedTask(
        id="deps-vuln@python-uv",
        capability="capabilities/deps-vuln",
        role=Role.ANALYST,
        required=True,
        ecosystem="ecosystems/python-uv",
    )
    kit = Toolkit(session=session, task=task, now=MOMENT, quarantine_days=7, step_limit=1)
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")

    assert kit.call("read_file", {"path": "uv.lock"})["call"]
    with pytest.raises(Refused, match="step budget"):
        kit.call("read_file", {"path": "uv.lock"})


def test_a_scheduled_run_is_given_less_than_a_reviewed_one() -> None:
    execution = Config.load().execution
    interactive = execution.budget_for(Trigger.CHANGE_OPENED)
    scheduled = execution.budget_for(Trigger.MAINTAIN_SCHEDULED)
    assert scheduled.task_seconds < interactive.task_seconds
    assert scheduled.max_parallel <= interactive.max_parallel
    # Omitted keys are inherited, so a tightened section states only its differences.
    assert scheduled.run_tokens == interactive.run_tokens


def test_a_budget_that_makes_no_sense_stops_the_run(tmp_path: Path) -> None:
    directory = tmp_path / "config"
    shutil.copytree(BUILTIN_CONFIG_DIR, directory)
    path = directory / "execution.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace("max_parallel: 4", "max_parallel: 0"),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="max_parallel"):
        Config.load(directory)
