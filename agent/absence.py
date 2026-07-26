"""How long a tracked finding has gone unreported, and when that is enough to close its issue.

Closing an issue is a claim: this is over. Nobody revisits a closed issue to check, which makes it
the one write in this agent where being wrong is quiet and permanent — the opposite of a review
thread, where the next push reopens what was resolved too early and the person watching sees it.

So the issue path waits for the claim to hold twice. The check that owns the finding must have
reached a complete answer, and must have done so twice in a row without listing the finding. One
complete answer would be enough if a task were exhaustive; it is asked to be and mostly is, but the
cost of the exception — an issue closed as fixed while the pin is still there — is a person trusting
a tracker that is wrong. The cost of waiting is that a genuinely fixed problem stays visible for one
more run, and a run of a repository nobody is watching happens weekly.

The streak is per finding key and lives in `agent/state.py`, for the same reason the failure streaks
do: a cache may be evicted, and "has this been gone for two runs?" answered from an evicted cache is
answered wrongly in whichever direction the eviction happens to push it. A finding that is reported
again forgets its streak entirely, so two counted runs always mean two consecutive ones.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from agent.escalate import about_a_failure
from agent.reconcile import unproven
from agent.verdict import TaskOutcome

ABSENCES = "absences"

THRESHOLD = 2
"""Complete runs without a finding before its issue is closed. One is a claim on a single answer."""


@dataclass(slots=True)
class Absences:
    """The streaks as they were, and as they will be stored once this run has been through them.

    Stateful on purpose: the reconciliation asks about one key at a time, in the order it walks the
    tracked set, and what to remember afterwards depends on every one of those answers.
    """

    outcomes: tuple[TaskOutcome, ...]
    run: str
    when: datetime
    before: dict[str, Any] = field(default_factory=dict)
    asked: frozenset[str] = frozenset()
    """Findings a person wrote about, waking this run. Their issues settle on the first answer.

    The wait exists because nobody looks at a closed issue. Here somebody is looking at this one,
    and is told on it what the recheck found — so "come back next week" is the wrong reply to a
    person who just approved something, and a closure they disagree with is one they can answer."""
    threshold: int = THRESHOLD
    kept: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def of(
        cls,
        memory: dict[str, Any],
        *,
        outcomes: tuple[TaskOutcome, ...],
        run: str,
        when: datetime,
        asked: frozenset[str] = frozenset(),
        threshold: int = THRESHOLD,
    ) -> Absences:
        stored = memory.get(ABSENCES)
        return cls(
            outcomes=outcomes,
            run=run,
            when=when,
            before=dict(stored) if isinstance(stored, dict) else {},
            asked=asked,
            threshold=threshold,
        )

    def reported(self, key: str) -> None:
        """This finding is on this run's list, so whatever it was doing last week is irrelevant."""
        self.before.pop(key, None)

    def settled(self, key: str) -> str | None:
        """`None` when this issue may be closed now, or why it is being left open for now.

        Two gates, in this order because they answer different things. Whether the run knows
        anything about the absence at all is `unproven`'s question, and it is not counted: a check
        that did not run leaves the streak exactly as it was, or a repository whose runs alternate
        between ecosystems would close everything on the strength of never having looked.
        """
        pending = unproven(key, self.outcomes)
        if pending is not None:
            entry = self.before.pop(key, None)
            if entry is not None:
                self.kept[key] = entry
            return pending

        if about_a_failure(key) or key in self.asked:
            # The failure case is not an absence at all: the issue claims the check has not worked,
            # and the check just worked. Waiting a second run would delay the only good news the
            # agent ever posts, and would break what that issue promises its reader.
            self.before.pop(key, None)
            return None

        previous = self.before.pop(key, None)
        runs = int(previous.get("runs", 0)) + 1 if isinstance(previous, dict) else 1
        since = (
            str(previous.get("since"))
            if isinstance(previous, dict) and previous.get("since")
            else self.when.isoformat()
        )
        if runs >= self.threshold:
            # Nothing is kept: the issue is being closed, and a streak for a key nobody tracks is a
            # document that grows for the lifetime of the repository. Should the closure fail, the
            # next run starts this count again, which delays a closure and claims nothing false.
            return None
        self.kept[key] = {"runs": runs, "since": since, "last_run": self.run}
        return (
            "not reported in this run, and one complete run is one answer — the next run that "
            f"also completes without it closes this issue (absent since {since[:10]})"
        )

    def document(self, memory: dict[str, Any]) -> dict[str, Any]:
        """The memory to store: the streaks this run touched, and nothing it did not ask about.

        Keys it never asked about are dropped rather than carried, because the tracked set is what
        it walked: a key missing from that set has no issue any more, and its streak would be a
        number nobody can act on, kept for the lifetime of the repository.
        """
        return dict(memory) | {ABSENCES: dict(self.kept)}
