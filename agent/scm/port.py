"""What the agent needs from a hosting platform, and nothing else.

The port is narrow on purpose. A run publishes a decision about a change, keeps one thread per
finding alive across runs, and resolves a thread when the problem is demonstrably gone. Everything
else a platform offers — projects, milestones, releases, merging — is either none of the agent's
business or out of reach, and an interface naming it would invite a later slice to use it.

Nothing here is available to a model. Publishing runs after the decision is made, from the agent's
own code, which is what makes a repeated run update a thread instead of opening a second one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from agent.domain import RunResult


class ScmError(Exception):
    """A platform call the agent could not complete.

    Always recoverable at the run level: a review that could not be published is still a review, its
    verdict still decides the exit code, and the run says what it failed to post. Losing the verdict
    because a comment failed would make the gate less trustworthy than the platform it talks to.
    """


class Stance(StrEnum):
    """The decision as the platform understands it."""

    APPROVE = "approve"
    REQUEST_CHANGES = "request-changes"
    COMMENT = "comment"

    @classmethod
    def of(cls, result: RunResult) -> Stance:
        """A run result as a review stance.

        `inconclusive` deliberately comments rather than requesting changes. The merge is refused
        either way — that is the check's job — but "changes requested" tells an author their code is
        wrong, and a run that could not complete has not earned that claim.
        """
        return {
            RunResult.PASS: cls.APPROVE,
            RunResult.BLOCKED: cls.REQUEST_CHANGES,
            RunResult.INCONCLUSIVE: cls.COMMENT,
        }[result]


@dataclass(frozen=True, slots=True)
class Change:
    """The change under review, as the platform sees it."""

    number: int
    head: str
    """The commit the platform believes is under review. A run whose HEAD differs analysed other
    code, and comments derived from it would point at lines nobody proposed."""
    author: str
    draft: bool = False

    def as_json(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "head": self.head,
            "author": self.author,
            "draft": self.draft,
        }


@dataclass(frozen=True, slots=True)
class Thread:
    """One of the agent's own inline threads, found again by the marker in its first comment."""

    id: str
    """The platform's handle for the thread, used to resolve it."""
    comment: str
    """The platform's handle for the first comment, used to edit it in place."""
    key: str
    """The finding this thread belongs to, read from its marker."""
    body: str
    number: int = 0
    """The change this thread lives on, because replying needs it."""
    resolved: bool = False

    def as_json(self) -> dict[str, Any]:
        return {"thread": self.id, "key": self.key, "resolved": self.resolved}


@dataclass(frozen=True, slots=True)
class Identity:
    """Who the platform thinks is speaking.

    Worth asking about, because the answer changes what the comments mean and what they cause. A
    decision published under a person's account is a machine's judgement wearing somebody's name —
    and, worse, a workflow that starts a run on human comments and filters bots is happily woken by
    the agent's own comment, runs again, comments again.
    """

    login: str
    bot: bool
    known: bool = True

    @property
    def trustworthy(self) -> bool:
        """True when the platform will show this as a machine, so a bot filter can see it."""
        return self.known and self.bot

    @property
    def description(self) -> str:
        """Readable even when the name is not known yet, which is the ordinary case for an App: its
        credential proves it is an integration without saying which one."""
        if self.login:
            return self.login
        return "an app installation" if self.trustworthy else "an unreadable account"

    def as_json(self) -> dict[str, Any]:
        return {"login": self.login, "bot": self.bot, "known": self.known}


@dataclass(frozen=True, slots=True)
class Review:
    """A published review, as the platform recorded it."""

    reference: str
    stance: Stance
    """What the platform accepted, which is not always what was asked for: it refuses any review
    event on a change the credentials opened, and then the decision arrives as a comment. A run that
    recorded its intention instead of the outcome would claim to have requested changes on a pull
    request that shows none."""
    author: str = ""
    """Who the platform says wrote it. The only source that cannot be wrong about the name — an
    App's credential proves it is an integration but not which one, so the bot's own name is read
    back off the first thing it published."""

    def as_json(self) -> dict[str, Any]:
        return {"reference": self.reference, "stance": self.stance.value, "author": self.author}


@dataclass(frozen=True, slots=True)
class NewThread:
    """A thread to open with the review that carries it."""

    key: str
    body: str
    path: str
    line: int


class Platform(Protocol):
    """A hosting platform, as far as publishing a review is concerned."""

    @property
    def name(self) -> str: ...

    def identity(self) -> Identity:
        """Whose account the credential speaks for."""

    def change(self, number: int) -> Change: ...

    def threads(self, number: int) -> tuple[Thread, ...]:
        """The agent's own threads on this change, whatever their state."""

    def review(
        self, number: int, *, body: str, stance: Stance, head: str, threads: Sequence[NewThread]
    ) -> Review:
        """Publish one review body with its new threads, and return the platform's reference.

        One call for both, because a review body that arrived without its threads — or threads with
        no body explaining them — is the state a reader must make sense of when a run dies halfway.
        """

    def edit(self, thread: Thread, body: str) -> None:
        """Rewrite the first comment of an existing thread."""

    def reply(self, thread: Thread, note: str) -> None:
        """Add to the conversation. Every state change says why before it happens."""

    def resolve(self, thread: Thread) -> None: ...

    def unresolve(self, thread: Thread) -> None:
        """Bring a thread back: a problem that returned to a resolved thread is one nobody sees."""
