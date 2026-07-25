"""Whether this run may execute the code it is reading.

The question exists because of what a review job contains. It holds a credential for the hosting
platform and one for a model provider, and it looks at code somebody outside the project wrote. Any
command over that code — an install, a build, a linter that loads a plugin from the manifest — runs
that person's code beside those credentials. Scrubbing the environment handed to the command does
not close it: a child process runs under the same user and can read the parent's original
environment out of `/proc`, along with the checkout, `~/.ssh` and anything else that user can read.

There are only two real boundaries. Execute in a different security context — another user, a
container with no network — or do not execute at all. The second is free, and it is what this module
selects. What it costs is stated rather than hidden: a task that needed a command records a gap, no
fix is prepared and no patch is offered, and the report says the run did not run anything.

A maintenance run is the opposite case and is deliberately untouched. It works on the default
branch, which is code the project merged, and verifying a dependency bump means building the project
— there is no version of that check which executes nothing. Containing it needs the isolation above,
not a refusal.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from agent.scm import Platform, ScmError


class Head(StrEnum):
    """Where the code under review came from, as far as this run could establish."""

    OWN = "own"
    """This repository's own branch, or no change at all — a maintenance run on the default
    branch."""
    OUTSIDE = "outside"
    """A fork. Somebody else's repository, and nobody here approved what is in it."""
    UNKNOWN = "unknown"
    """The platform was not there to ask, or would not answer. Read as outside: a review that
    guesses in the permissive direction is one an attacker can arrange by breaking one API call."""


@dataclass(frozen=True, slots=True)
class Posture:
    """What this run is allowed to do with the code it reads, and why."""

    head: Head
    detail: str = ""

    @property
    def executes(self) -> bool:
        """Whether any command may be run over this checkout."""
        return self.head is Head.OWN

    @property
    def restraint(self) -> str:
        """What a reader has to be told before the findings, or nothing when all was permitted."""
        if self.executes:
            return ""
        return (
            f"Nothing in this change was executed: {self.detail}. Findings that need a command to "
            "establish are reported as gaps rather than as passes, and no fix was prepared"
        )

    @property
    def aside(self) -> str:
        """The same fact for somebody who asked a question and got prose back.

        Said because the absence is otherwise unexplained. A person who asks how to fix a remark on
        their own branch is offered the change; the same question on a fork gets a paragraph, and
        without this line the difference looks like the agent being unhelpful.
        """
        if self.executes:
            return ""
        return (
            "This change comes from outside the repository, so this run read its code and executed "
            "none of it. That is why no prepared change is offered here: one could not have been "
            "verified, and an unverified edit is not worth clicking."
        )

    def as_json(self) -> dict[str, Any]:
        return {"head": self.head.value, "executes": self.executes, "detail": self.detail}


def posture_for(
    *, change: int | None, platform: Platform | None, forced: bool = False
) -> tuple[Posture, str]:
    """The posture for this run, and the warning its record needs when it is not the ordinary one.

    Naming a change means the platform decides. It is the only party that knows which repository the
    head lives in, so a run that names a change and cannot ask has not established the one fact this
    decision rests on — and answers that with the restrained posture rather than with a guess.
    """
    if forced:
        return Posture(Head.OUTSIDE, "this run was told to treat the head as outside code"), ""
    if change is None:
        return Posture(Head.OWN, "this run works on the repository's own checkout"), ""
    if platform is None:
        posture = Posture(Head.UNKNOWN, "the platform could not be asked where the head lives")
        return posture, (
            "nothing was executed: this run names a change but could not ask the platform whether "
            "its head is this repository's own. An outside head is not distinguishable from a "
            "colleague's branch without that answer, so the run read the code and ran none of it"
        )
    try:
        proposed = platform.change(change)
    except ScmError as error:
        posture = Posture(
            Head.UNKNOWN, f"the platform would not say where the head lives ({error})"
        )
        return posture, (
            f"nothing was executed: where this change's head lives could not be established "
            f"({error}), and a head that cannot be shown to be this repository's own is treated as "
            "somebody else's"
        )
    if not proposed.elsewhere:
        return Posture(Head.OWN, "the head is a branch in this repository"), ""
    where = proposed.repository or "a repository the platform did not name"
    posture = Posture(Head.OUTSIDE, f"the head lives in {where}")
    return posture, (
        f"nothing was executed: this change comes from {where}, so its code was read and none of "
        "it was run. Checks that need a command report a gap, and no fix or patch was prepared"
    )
