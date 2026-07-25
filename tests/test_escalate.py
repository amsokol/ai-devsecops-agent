"""A check that keeps failing gets one issue, on the second run and not the first."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from agent.domain import Outcome, Reason, RunResult
from agent.escalate import Escalation, weigh
from agent.issues import LABEL, track_findings
from agent.scm.fake import FakePlatform
from agent.scm.marker import read
from agent.verdict import TaskOutcome, Verdict

WHEN = datetime(2026, 7, 25, 9, 0, tzinfo=UTC)
DEPS = "capabilities/deps-vuln"
CODE = "capabilities/code-vuln"
HEAD = "0f1e2d3c4b5a69788796a5b4c3d2e1f009182736"


def outcome(
    capability: str = DEPS,
    *,
    result: Outcome = Outcome.UNVERIFIED,
    reason: Reason | None = Reason.UNAVAILABLE,
) -> TaskOutcome:
    return TaskOutcome(
        id=f"{capability.rsplit('/', 1)[-1]}@python-uv",
        capability=capability,
        required=True,
        outcome=result,
        reason=reason,
    )


def again(
    outcomes: tuple[TaskOutcome, ...], memory: dict[str, Any], run: str = "run-2"
) -> tuple[tuple[Escalation, ...], dict[str, Any]]:
    return weigh(outcomes, memory=memory, run=run, when=WHEN)


def test_one_failure_tells_nobody_but_is_remembered() -> None:
    """A registry with a bad minute is not an outage, and an issue per blip earns a mute button."""
    escalations, memory = weigh((outcome(),), memory={}, run="run-1", when=WHEN)

    assert escalations == ()
    assert memory["failures"][f"{DEPS}:failure:unavailable"]["runs"] == 1


def test_the_same_failure_twice_running_escalates_once() -> None:
    _, first = weigh((outcome(),), memory={}, run="run-1", when=WHEN)

    escalations, memory = again((outcome(),), first)

    assert [item.key for item in escalations] == [f"{DEPS}:failure:unavailable"]
    assert escalations[0].runs == 2
    assert escalations[0].title == "agent: the deps-vuln check keeps failing"
    assert "in 2 scheduled runs with the same reason `unavailable`" in escalations[0].body
    assert "has not run to completion since 2026-07-25" in escalations[0].body
    assert read(escalations[0].body) == f"{DEPS}:failure:unavailable"
    assert memory["failures"][f"{DEPS}:failure:unavailable"]["since"] == WHEN.isoformat()


def test_a_different_reason_starts_its_own_count_without_erasing_the_first() -> None:
    """Two ways of failing are two things to say, and neither run proved the other over: the check
    has still not completed. Nobody is told yet, because no single reason has repeated."""
    _, first = weigh((outcome(),), memory={}, run="run-1", when=WHEN)

    escalations, memory = again((outcome(reason=Reason.INVALID_RESULT),), first)

    assert escalations == ()
    assert sorted(memory["failures"]) == [
        f"{DEPS}:failure:invalid-result",
        f"{DEPS}:failure:unavailable",
    ]


def test_a_check_that_completes_clears_its_streak_and_lets_its_issue_close() -> None:
    _, first = weigh((outcome(),), memory={}, run="run-1", when=WHEN)
    escalations, second = again((outcome(),), first)
    platform = FakePlatform()
    track_findings(
        platform,
        verdict=Verdict(result=RunResult.INCONCLUSIVE),
        outcomes=(outcome(),),
        head=HEAD,
        limit=5,
        escalations=escalations,
        label=LABEL,
    )
    assert len(platform.issues(label=LABEL)) == 1

    clean = (outcome(result=Outcome.CLEAN, reason=None),)
    left, memory = weigh(clean, memory=second, run="run-3", when=WHEN)
    record = track_findings(
        platform,
        verdict=Verdict(result=RunResult.PASS),
        outcomes=clean,
        head=HEAD,
        limit=5,
        escalations=left,
        label=LABEL,
    )

    assert left == ()
    assert memory["failures"] == {}
    assert record.closed == 1
    assert "the failure this issue reports is over" in platform.notes[0][1]
    assert not platform.issues(label=LABEL)


def test_a_check_that_did_not_run_keeps_its_streak_where_it_was() -> None:
    """Silence is not recovery. A run narrowed to one ecosystem must not reset a real outage, and
    must not count towards one either."""
    _, first = weigh((outcome(),), memory={}, run="run-1", when=WHEN)

    escalations, memory = again((outcome(CODE, result=Outcome.CLEAN, reason=None),), first)

    assert escalations == ()
    assert memory["failures"][f"{DEPS}:failure:unavailable"]["runs"] == 1


def test_exhaustion_escalates_with_the_budget_named() -> None:
    """`exhausted` has one likely cause a person can act on, so the issue says where to look."""
    exhausted = (outcome(result=Outcome.EXHAUSTED, reason=None),)
    _, first = weigh(exhausted, memory={}, run="run-1", when=WHEN)

    escalations, _ = again(exhausted, first)

    assert escalations[0].key == f"{DEPS}:failure:exhausted"
    assert "budget.scheduled" in escalations[0].body


def test_a_repeat_updates_the_issue_it_already_opened() -> None:
    """The third run says "three", in the issue that already exists rather than beside it."""
    platform = FakePlatform()
    _, first = weigh((outcome(),), memory={}, run="run-1", when=WHEN)
    twice, second = again((outcome(),), first)
    track_findings(
        platform,
        verdict=Verdict(result=RunResult.INCONCLUSIVE),
        outcomes=(outcome(),),
        head=HEAD,
        limit=5,
        escalations=twice,
        label=LABEL,
    )

    thrice, _ = weigh((outcome(),), memory=second, run="run-3", when=WHEN)
    record = track_findings(
        platform,
        verdict=Verdict(result=RunResult.INCONCLUSIVE),
        outcomes=(outcome(),),
        head=HEAD,
        limit=5,
        escalations=thrice,
        label=LABEL,
    )

    assert thrice[0].runs == 3
    assert record.raised == 0
    assert [item.what for item in record.posted] == ["updated"]
    assert len(platform.issues(label=LABEL)) == 1


def test_a_broken_check_is_told_about_even_when_the_new_issue_limit_is_spent() -> None:
    """The findings of the checks that work can wait a week. That one of them is blind cannot: every
    other issue this run leaves alone is only trustworthy if that is known."""
    platform = FakePlatform()
    _, first = weigh((outcome(),), memory={}, run="run-1", when=WHEN)
    escalations, _ = again((outcome(),), first)

    record = track_findings(
        platform,
        verdict=Verdict(result=RunResult.INCONCLUSIVE),
        outcomes=(outcome(),),
        head=HEAD,
        limit=0,
        escalations=escalations,
        label=LABEL,
    )

    assert record.raised == 1
    assert [item.what for item in record.posted] == ["raised"]


@pytest.mark.parametrize("stored", [{"failures": "nonsense"}, {}, {"failures": {"x": 5}}])
def test_a_memory_that_makes_no_sense_is_treated_as_no_memory(stored: dict[str, Any]) -> None:
    """The document is written by an earlier version of this agent, so it is read defensively: a
    crash here would turn a memory aid into a way to fail every scheduled run."""
    escalations, memory = weigh((outcome(),), memory=stored, run="run-1", when=WHEN)

    assert escalations == ()
    assert memory["failures"][f"{DEPS}:failure:unavailable"]["runs"] == 1
