"""When a check keeps failing, say so once — to a person, not to a log nobody opens.

A scheduled run has nobody watching it. Its failures are therefore the ones most likely to go
unnoticed: the run ends inconclusive, the report lands in an artifact, and next week the same thing
happens. After a month the team believes the agent watches four things while it watches two.

The rule the library asks for, and the reason for each half of it:

*Not on the first failure.* A registry with a bad minute is not an outage, and an issue per blip is
how a tracker earns its mute button. One failure is recorded in the run and nowhere else.

*Once when it repeats.* The second failure of the same check for the same reason opens one issue,
keyed like everything else the agent tracks, so a third and fourth run update it instead of adding
to it. It closes as every other tracked thing does: the check ran to completion again.

Repetition is what needs the memory in `agent.state`, and it is counted precisely. A streak is kept
per check and reason, and it survives only until that check completes: a run in which the check
completed clears it, whatever it did last week. So a count of two means the check has not worked
since the first of them — not that two bad minutes happened to be remembered months apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from agent.domain import Outcome
from agent.scm import marker
from agent.verdict import TaskOutcome

THRESHOLD = 2
"""Consecutive failures before a person is told. Two, because one is noise and three is a month."""

FAILURES = "failures"


@dataclass(frozen=True, slots=True)
class Escalation:
    """One check that keeps failing, ready to be tracked like any other issue."""

    key: str
    title: str
    body: str
    runs: int

    def as_json(self) -> dict[str, Any]:
        return {"key": self.key, "runs": self.runs}


def weigh(
    outcomes: tuple[TaskOutcome, ...],
    *,
    memory: dict[str, Any],
    run: str,
    when: datetime,
    threshold: int = THRESHOLD,
) -> tuple[tuple[Escalation, ...], dict[str, Any]]:
    """This run's escalations and the memory to store, from what failed and what failed before.

    Three cases, and the third is easy to get wrong. A check that failed the same way extends its
    streak. A check that ran to completion clears every streak it has, whatever it did last week. A
    check that did not run at all keeps its streaks exactly as they were: a run narrowed to one
    ecosystem is no evidence that last week's outage is over, and treating it as evidence would
    reset the counter forever in a repository whose runs alternate.

    A check that failed for a different reason this time is the fourth case, and it falls out of the
    three: the new reason starts its own count, and the old one is neither extended nor cleared,
    because the check still has not completed. So a count never overstates anything — it reads as
    "this reason, this many times, in a check that has not worked since".
    """
    before = memory.get(FAILURES)
    streaks: dict[str, Any] = dict(before) if isinstance(before, dict) else {}

    failing = {_key(item): item for item in outcomes if item.failed}
    completed = {item.capability for item in outcomes if not item.failed}

    kept: dict[str, Any] = {}
    for key, entry in streaks.items():
        if key in failing or _capability(key) in completed:
            continue
        kept[key] = entry

    escalations: list[Escalation] = []
    for key, outcome in sorted(failing.items()):
        previous = streaks.get(key)
        runs = int(previous.get("runs", 0)) + 1 if isinstance(previous, dict) else 1
        since = (
            str(previous.get("since"))
            if isinstance(previous, dict) and previous.get("since")
            else when.isoformat()
        )
        kept[key] = {"runs": runs, "since": since, "last_run": run}
        if runs >= threshold:
            escalations.append(
                Escalation(
                    key=key,
                    title=_title(outcome),
                    body=_body(outcome, key=key, runs=runs, since=since, run=run),
                    runs=runs,
                )
            )
    return tuple(escalations), dict(memory) | {FAILURES: kept}


def _key(outcome: TaskOutcome) -> str:
    """`capability:failure:reason` — the capability first, because that is where every other part of
    the agent reads it from, and closing this issue obeys the same rule as closing a finding's."""
    reason = outcome.reason.value if outcome.reason is not None else outcome.outcome.value
    return f"{outcome.capability}:failure:{reason}"


def _capability(key: str) -> str:
    return key.split(":", 1)[0]


def _title(outcome: TaskOutcome) -> str:
    name = outcome.capability.rsplit("/", 1)[-1]
    return f"agent: the {name} check keeps failing"


def _body(outcome: TaskOutcome, *, key: str, runs: int, since: str, run: str) -> str:
    """What is broken, since when, and what it costs while it stays broken.

    The cost is the part worth writing down. A failing check does not merely produce no findings: it
    also stops the agent closing the issues it owns, so the tracker freezes in whatever state the
    last good run left it in. Somebody reading this issue should be able to tell how much of the
    picture is missing without reconstructing the rule from the code.
    """
    what = outcome.reason.value if outcome.reason is not None else outcome.outcome.value
    ended = "was exhausted" if outcome.outcome is Outcome.EXHAUSTED else "could not finish"
    lines = [
        f"`{outcome.capability}` {ended} in {runs} scheduled runs with the same reason `{what}`, "
        f"and has not run to completion since {since[:10]}. Most recently in run `{run}`.",
        "",
        "While this lasts, the agent knows nothing about what that check covers. It reports no "
        "findings from it, and it leaves the issues that check owns exactly as they are rather "
        "than closing them — absence is not evidence when the thing that looks is broken. Every "
        "scheduled run in this state ends inconclusive.",
        "",
        "This is the only issue the agent will open about this failure: later runs update it, and "
        "the run in which the check completes again closes it with a note saying so.",
    ]
    if outcome.outcome is Outcome.EXHAUSTED:
        lines += [
            "",
            "`exhausted` means a budget ran out rather than a tool being absent — wall clock, "
            "steps, or the run's token ceiling. Worth checking whether the repository grew or the "
            "`budget.scheduled` section of the overlay is now too tight for it.",
        ]
    return marker.stamp("\n".join(lines), key)
