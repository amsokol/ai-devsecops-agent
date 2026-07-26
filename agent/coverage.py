"""What a check actually looked at this run, read out of the evidence it recorded.

A task says what it found. Until now nothing said what it *examined*, and the two are not the same
answer: a sweep that stopped after four of a repository's six action pins produced a report
identical to one that went through all six. Two consecutive live runs did exactly that, and the
shorter of them ran first, so its silence about the other two pins was the only record of them.

Silence is the dangerous part, because the tracker now acts on it. An issue closes when the check
that owns its finding completes twice without listing it (`agent/absence.py`), and "did not list it"
is indistinguishable from "did not reach it" in a report that only carries findings. Two short runs
in a row would therefore close a live issue as fixed — the one write this agent makes that nobody
ever revisits to check.

The examined set is not asked for. `record_fact` already refuses a fact that does not cite a call
the task actually made, so the subjects of a run's verified evidence are a list of what it did,
demonstrated by the calls behind it, and no prompt or contract field can inflate it. Reading it here
costs nothing and cannot be gamed by a session that would rather look thorough than be thorough.

What this can speak about is deliberately narrow. A dependency check is asked to enumerate every pin
and record a fact per pin, including the pins that turn out to be fine, so absence of a fact there
means absence of a look. A code check is asked no such thing — nobody records "this file is clean"
about every file in a repository — so coverage says nothing about findings that name a path, and the
outcome gate remains the only test for those.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from agent.evidence import Evidence

COVERAGE = "coverage"

ECOSYSTEM = "ecosystems/"
"""How an ecosystem's document id begins, which is how a package key is told from a path key.

Both shapes of finding key start with the capability; the second segment is an ecosystem for a
package and a repository path for everything else. Library document ids carry their directory, so
the two are distinguishable without asking anybody which shape this key is.
"""

NAMED = 6
"""How many missing subjects a shortfall names before it starts counting them instead."""


@dataclass(frozen=True, slots=True)
class Coverage:
    """The packages each check examined, grouped by the check and the ecosystem it examined them in.

    Grouped that way because a run can be narrowed to one ecosystem — `--only` does it, and so does
    an overlay that enables three of eight. Comparing a capability's whole examined set against last
    week's would then read every narrowed run as a collapse in coverage, and a warning that fires on
    a supported way of running the agent is a warning people learn to scroll past.
    """

    examined: Mapping[str, frozenset[str]]

    @classmethod
    def of(cls, records: Iterable[Evidence]) -> Coverage:
        """Read the examined set from a run's evidence.

        Only verified records count. A recorded gap means the check tried and could not find out,
        which is an honest answer to the question it asked and no answer at all to whether the pin
        is still there — so it must not license a closure.
        """
        gathered: dict[str, set[str]] = {}
        for record in records:
            if not record.is_verified:
                continue
            capability = record.recipe.split("@", 1)[0]
            subject = record.subject
            if not capability or not subject.ecosystem or not subject.package:
                continue
            gathered.setdefault(f"{capability}:{subject.ecosystem}", set()).add(subject.package)
        return cls(examined={bucket: frozenset(names) for bucket, names in gathered.items()})

    def looked_at(self, key: str) -> bool | None:
        """Whether this run examined what the key is about, or `None` when it cannot say.

        `None` for anything that is not a package — a code finding, an escalation about a failing
        check — because nobody records a fact per file and requiring one would freeze those issues
        open forever. Those are answered by the outcome alone, as they were before this existed.

        `None` too when the check recorded nothing at all about this ecosystem, which is the same
        restraint by a different route. Reading an empty bucket as "examined nothing" would be wrong
        in the one case that matters: an ecosystem whose last pin was removed has no facts to record
        and its leftover issue is exactly the one that ought to close. So the gate calibrates itself
        against what the check demonstrably does — once a bucket has anything in it, this check
        records a fact per package here, and a package missing from the list is a package it did not
        reach. That is the shape of the failure this was built for: four of six pins, not none.
        """
        parts = key.split(":")
        if len(parts) < 3 or not parts[1].startswith(ECOSYSTEM):
            return None
        known = self.examined.get(f"{parts[0]}:{parts[1]}")
        if known is None:
            return None
        return parts[2] in known

    def shortfall(self, before: Mapping[str, Any]) -> tuple[str, ...]:
        """What an earlier run examined and this one did not, in the buckets both of them entered.

        Only those buckets. A capability or ecosystem missing from this run was not checked at all,
        which the verdict already says in a louder voice, and repeating it here as a coverage
        complaint would bury the cases this is for: the run that checked the same ecosystem as last
        week and quietly got through less of it.
        """
        said: list[str] = []
        for bucket, names in sorted(self.examined.items()):
            previous = before.get(bucket)
            if not isinstance(previous, list):
                continue
            missing = sorted({str(name) for name in previous} - names)
            if not missing:
                continue
            shown = ", ".join(missing[:NAMED])
            if len(missing) > NAMED:
                shown += f", and {len(missing) - NAMED} more"
            capability, _, ecosystem = bucket.partition(":")
            said.append(
                f"{capability} examined {len(names)} package(s) in {ecosystem}, against "
                f"{len(previous)} in the last run: {shown} were not looked at this time. Nothing "
                "is claimed about them, and their issues stay open"
            )
        return tuple(said)

    def document(self, memory: dict[str, Any]) -> dict[str, Any]:
        """The memory to store, merged over what an earlier run left.

        Merged rather than replaced, because a narrowed run knows nothing about the buckets it did
        not enter and overwriting them with nothing would erase the record a full run left. The
        price is that a bucket whose ecosystem is switched off in the overlay stays in the document
        until somebody prunes it, which costs a few hundred bytes in a git ref.
        """
        stored = memory.get(COVERAGE)
        merged = dict(stored) if isinstance(stored, dict) else {}
        merged |= {bucket: sorted(names) for bucket, names in self.examined.items()}
        return dict(memory) | {COVERAGE: merged}

    def as_json(self) -> dict[str, Any]:
        return {bucket: sorted(names) for bucket, names in sorted(self.examined.items())}


def previous(memory: Mapping[str, Any]) -> Mapping[str, Any]:
    """The examined sets an earlier run stored, or nothing when there is none to compare with."""
    stored = memory.get(COVERAGE)
    return stored if isinstance(stored, dict) else {}
