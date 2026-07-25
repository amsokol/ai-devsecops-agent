"""Concurrency and budgets: what a run does when it cannot afford everything it planned.

The properties asserted here are the ones a cost ceiling is worthless without: that overlapping
sessions stay under the limit, that the report does not reshuffle because two tasks finished in a
different order, and that a task nobody paid for is never mistaken for a check that passed.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from agent.backends import Brief, Budget, Failure, FakeBackend, Scripted, SessionResult
from agent.backends.port import Usage
from agent.backends.select import Roster
from agent.budget import Ledger, RunBudget
from agent.domain import Outcome, Plan, PlannedTask, Reason, Role, RunResult, Trigger
from agent.errors import ConfigError
from agent.evidence import EvidenceStore
from agent.executor import Executed, execute
from agent.library import Library
from agent.overlay import Overlay
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
            roster=Roster.of(backend),
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


MAINTENANCE_BLOCK = (
    "maintenance:\n  models:\n    analyst: fake/none\n  limits:\n    tokens_per_run: 800\n"
    "    minutes_per_task: 10\n    tasks_at_once: 1\n"
    "  queue:\n    max_new_issues_per_run: 5\n    max_open_fix_requests: 3\n"
)


def spending(review: str, tmp_path: Path, library: Library) -> Overlay:
    """An overlay carrying the given `review:` block and the minimum around it."""
    root = tmp_path / "overlay"
    root.mkdir(exist_ok=True)
    (root / "agent.yaml").write_text(
        f"schema: 1\nquarantine:\n  days: 7\n{MAINTENANCE_BLOCK}{review}",
        encoding="utf-8",
    )
    return Overlay.load(root, library=library, notes_limit=8000)


def test_maintenance_is_given_less_than_a_review_somebody_is_waiting_for(overlay: Overlay) -> None:
    """The product decides both sets of numbers; the agent only decides which set a trigger gets."""
    review = overlay.settings_for(Trigger.CHANGE_OPENED).limits
    maintenance = overlay.settings_for(Trigger.MAINTAIN_SCHEDULED).limits
    assert maintenance.seconds_per_task < review.seconds_per_task
    assert maintenance.tasks_at_once <= review.tasks_at_once
    assert review.tokens_per_run is not None
    assert maintenance.tokens_per_run is not None
    assert maintenance.tokens_per_run < review.tokens_per_run


def test_maintenance_started_by_hand_is_held_to_the_same_numbers(overlay: Overlay) -> None:
    """The work is identical whether a timer or a person started it, so the ceilings are too."""
    assert overlay.settings_for(Trigger.MAINTAIN_REQUESTED) is overlay.maintenance
    assert overlay.settings_for(Trigger.MAINTAIN_SCHEDULED) is overlay.maintenance


def test_no_ceiling_is_something_a_product_writes_rather_than_omits(
    tmp_path: Path, library: Library
) -> None:
    """`null` is a decision on the record; a missing key is a question nobody answered."""
    written = spending(
        "review:\n  models:\n    analyst: fake/none\n  limits:\n    tokens_per_run: null\n"
        "    minutes_per_task: 15\n    tasks_at_once: 3\n",
        tmp_path,
        library,
    )
    assert written.review.limits.tokens_per_run is None
    assert written.maintenance.limits.tokens_per_run == 800

    with pytest.raises(ConfigError, match=r"tokens_per_run.*required"):
        spending(
            "review:\n  models:\n    analyst: fake/none\n  limits:\n    minutes_per_task: 15\n"
            "    tasks_at_once: 3\n",
            tmp_path,
            library,
        )


def test_a_limit_that_makes_no_sense_stops_the_run(tmp_path: Path, library: Library) -> None:
    """Nothing runs at zero tasks at once, and a run that reports nothing found would be a lie."""
    with pytest.raises(ConfigError, match="tasks_at_once"):
        spending(
            "review:\n  models:\n    analyst: fake/none\n  limits:\n    tokens_per_run: 9\n"
            "    minutes_per_task: 15\n    tasks_at_once: 0\n",
            tmp_path,
            library,
        )
