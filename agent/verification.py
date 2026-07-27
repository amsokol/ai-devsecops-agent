"""Whether a fix was actually verified, decided from the run's own record.

Which surfaces a change needs is judgement, and it lives in the library: a Rust-only pin move does
not need the Go suite, and a lock that feeds a meta build system needs a surface that never appears
in the changed paths. Whether those commands ran is not judgement, and it is not taken from the
model's word either — it is matched against the calls the task actually made.

The asymmetry is the point. A model that skipped verification and reported success is the one case
where believing the report costs the most: the branch looks ready, a human merges it, and the check
that would have caught the problem never ran. Reading it from the ledger makes the cheapest way to
report `fixed` be to actually verify.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from agent.toolkit import Call

VERIFICATION_TOOL = "run_command"


@dataclass(frozen=True, slots=True)
class Ran:
    surface: str
    command: tuple[str, ...]
    ok: bool

    def as_json(self) -> dict[str, Any]:
        return {"surface": self.surface, "command": list(self.command), "ok": self.ok}


@dataclass(frozen=True, slots=True)
class Verification:
    """What verification the task ran, and whether that is enough to ship."""

    ran: tuple[Ran, ...]
    verified: tuple[str, ...]
    """Surfaces whose every command ran and passed. A partly run surface is not one of them."""
    passed: bool
    detail: str = ""
    pre_existing: tuple[tuple[str, ...], ...] = ()
    """Failing commands that fail on the unchanged head too, so the fix did not cause them."""
    awaiting_ci: bool = False
    """A person authorised a pull request with no local surface; CI on that PR is the proof."""

    @property
    def attempted(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item.surface for item in self.ran))

    @property
    def failed(self) -> tuple[tuple[str, ...], ...]:
        return tuple(item.command for item in self.ran if not item.ok)

    @property
    def blocked_by_base(self) -> bool:
        """Every failure was already there: nothing this task did could have made it green."""
        return bool(self.failed) and len(self.pre_existing) == len(self.failed)

    def as_json(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "verified": list(self.verified),
            "attempted": list(self.attempted),
            "ran": [item.as_json() for item in self.ran],
            "pre_existing": [list(command) for command in self.pre_existing],
            "detail": self.detail,
            "awaiting_ci": self.awaiting_ci,
        }

    def against_base(self, pre_existing: tuple[tuple[str, ...], ...]) -> Verification:
        """The same record, with the failures that predate the change named as such.

        Still not passing: a branch whose surface is red is a branch a human distrusts, whoever made
        it red. What changes is the reason a team is given, and it is the difference between "the
        agent cannot fix your dependencies" and "your checks were failing before it tried".
        """
        if not pre_existing:
            return self
        caused = [command for command in self.failed if command not in pre_existing]
        inherited = ", ".join(" ".join(command) for command in pre_existing)
        if not caused:
            detail = (
                f"verification failed for reasons that predate this change: {inherited} fails on "
                "the unchanged head too. Nothing can be shown safe on this surface until that is "
                "fixed"
            )
        else:
            detail = (
                f"verification failed: {', '.join(' '.join(command) for command in caused)}"
                f" (and {inherited}, which fails without this change too)"
            )
        return replace(self, detail=detail, pre_existing=pre_existing)


Surfaces = dict[str, tuple[tuple[str, ...], ...]]


def check(surfaces: Surfaces, calls: tuple[Call, ...]) -> Verification:
    """Match the overlay's verification commands against what this task ran.

    Passing needs a whole surface: every command the overlay lists for it ran, and none of the
    commands that ran anywhere failed. "At least one command" was the first rule here and it did not
    survive its first live run — three sessions made the same change, two ran the cheapest command
    of a five-command surface and shipped, the third ran all five, found a failure and refused. Same
    change, three verdicts, decided by how thorough each session felt like being.

    Which surfaces to run is still judgement and still the task's. What a surface *consists of* is
    the product's own statement, and a caller does not get to take the first line of it.
    """
    if not surfaces:
        return Verification(
            ran=(),
            verified=(),
            passed=False,
            detail=(
                "the overlay names no verification commands, so no fix can be shown to be safe. "
                "Add a `verification` section reusing the commands this product's CI already runs"
            ),
        )
    executed = {call.source: call for call in calls if call.tool == VERIFICATION_TOOL}
    ran: list[Ran] = []
    verified: list[str] = []
    missing: list[str] = []
    for surface, commands in sorted(surfaces.items()):
        found = [(command, executed.get(" ".join(command))) for command in commands]
        ran += [
            Ran(surface=surface, command=command, ok=call.ok)
            for command, call in found
            if call is not None
        ]
        absent = [command for command, call in found if call is None]
        if not absent and all(call is not None and call.ok for _, call in found):
            verified.append(surface)
        elif len(absent) < len(commands):
            missing += [f"{surface}: {' '.join(command)}" for command in absent]
    failed = [item for item in ran if not item.ok]
    if failed:
        listed = ", ".join(" ".join(item.command) for item in failed)
        return Verification(
            ran=tuple(ran), verified=(), passed=False, detail=f"verification failed: {listed}"
        )
    if verified:
        return Verification(ran=tuple(ran), verified=tuple(verified), passed=True)
    if missing:
        return Verification(
            ran=tuple(ran),
            verified=(),
            passed=False,
            detail=(
                "no verification surface was run in full; a surface counts only when every command "
                f"the overlay lists for it has run. Not run: {', '.join(missing)}"
            ),
        )
    listed = ", ".join(" ".join(command) for commands in surfaces.values() for command in commands)
    return Verification(
        ran=(),
        verified=(),
        passed=False,
        detail=f"none of the overlay's verification commands ran in this task ({listed})",
    )
