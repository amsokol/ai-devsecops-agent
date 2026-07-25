"""What the agent needs from a hosting platform, and nothing else.

The port is narrow on purpose. A run publishes a decision about a change, keeps one thread or issue
per finding alive across runs, settles it when the problem is demonstrably gone, and proposes the
branches it prepared. Everything else a platform offers — projects, milestones, releases, merging —
is either none of the agent's business or out of reach, and an interface naming it would invite a
later slice to use it.

Nothing here is available to a model. Publishing runs after the decision is made, from the agent's
own code, which is what makes a repeated run update a thread instead of opening a second one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
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
    repository: str = ""
    """Which repository the head branch lives in, as the platform names it. Empty when it would not
    say, which is what a deleted fork looks like."""
    elsewhere: bool = False
    """True when that is not this repository — a fork.

    The one fact that decides whether this run may execute anything it reads. A command run over a
    fork's head runs a stranger's code next to this run's credentials, and there is no environment
    hygiene that prevents a child process from reading them out of `/proc` under the same user.
    """

    def as_json(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "head": self.head,
            "author": self.author,
            "draft": self.draft,
            "repository": self.repository,
            "elsewhere": self.elsewhere,
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
    path: str = ""
    line: int = 0
    start_line: int = 0
    """Where the thread hangs: the file, the last line it covers, and the first when it covers
    several. Zero and empty mean the platform would not say — which is the ordinary answer for a
    thread whose lines the change has since moved past.

    Read because a `suggestion` block is applied to exactly these lines and to no others. A patch
    that changes anything else cannot be offered as one, however well it reads: the person would
    click "commit" and get a different edit from the one that was verified.
    """

    @property
    def anchor(self) -> tuple[int, int]:
        """The inclusive line range this thread covers, or `(0, 0)` when it is not known."""
        if not self.line:
            return (0, 0)
        return (self.start_line or self.line, self.line)

    def as_json(self) -> dict[str, Any]:
        return {"thread": self.id, "key": self.key, "resolved": self.resolved}


@dataclass(frozen=True, slots=True)
class Comment:
    """One comment, as the platform has it.

    `body` is the only untrusted text the agent reads. It is somebody's words, so it is data: it is
    quoted into a classifier's prompt and never obeyed as an instruction, and what the run then does
    is chosen from a fixed table in code.
    """

    id: int
    author: str
    bot: bool
    body: str
    parent: int = 0
    """For a comment in a review thread, the first comment of that thread — which is the handle the
    thread is found by. Zero when this comment *is* the first one, or when it is on an issue."""
    reference: str = ""

    def as_json(self) -> dict[str, Any]:
        return {"id": self.id, "author": self.author, "bot": self.bot, "reference": self.reference}


@dataclass(frozen=True, slots=True)
class Authority:
    """Whether an account may write to this repository, and whether that could be established.

    Asked because a comment is a way to make the agent spend money and grant permission. Without the
    question, anybody who can comment on an issue decides both. `known` is false when the platform
    would not answer, and an unknown answer is treated as a refusal: guessing in the permissive
    direction is the one mistake here that cannot be taken back.
    """

    login: str
    writes: bool
    known: bool = True

    def as_json(self) -> dict[str, Any]:
        return {"login": self.login, "writes": self.writes, "known": self.known}


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


@dataclass(frozen=True, slots=True)
class Issue:
    """One of the agent's own tracked findings, as the platform has it now."""

    number: int
    key: str
    """Read from the marker in the body, not from the title: a title is prose and gets edited."""
    title: str
    body: str
    reference: str = ""

    def as_json(self) -> dict[str, Any]:
        return {"number": self.number, "key": self.key, "reference": self.reference}


@dataclass(frozen=True, slots=True)
class NewIssue:
    """A finding to start tracking."""

    key: str
    title: str
    body: str


@dataclass(frozen=True, slots=True)
class Proposal:
    """One of the agent's own open change requests, found by the branch it carries."""

    number: int
    head: str
    reference: str = ""
    author: str = ""
    """Who the platform recorded as opening it, the only account that proves whose it is."""

    def as_json(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "head": self.head,
            "reference": self.reference,
            "author": self.author,
        }


@dataclass(frozen=True, slots=True)
class NewChange:
    """A prepared branch to propose."""

    head: str
    base: str
    title: str
    body: str


class Platform(Protocol):
    """A hosting platform, as far as publishing a review is concerned."""

    @property
    def name(self) -> str: ...

    def identity(self) -> Identity:
        """Whose account the credential speaks for."""

    def reading_token(self) -> str:
        """The credential a task's reads of this platform's API may carry, when there is one.

        Only for the agent's own HTTP tool, which is GET-only and confined to allowlisted hosts. Not
        for commands: those are given an environment with none, because a command may be executing
        code that arrived in the change under review. Empty when the platform has no credential, and
        acquisition then falls back to anonymous access and its far lower rate limit.
        """

    def authority(self, login: str) -> Authority:
        """Whether this account may write to the repository."""

    def change(self, number: int) -> Change: ...

    def change_comment(self, number: int, comment: int) -> Comment:
        """One comment in a review conversation, with the thread it belongs to."""

    def issue_at(self, number: int) -> Issue | None:
        """One issue by number, or `None` when it carries no marker.

        `None` means "not the agent's to act on". A run woken by a comment on somebody else's issue
        has nothing it can do there: no finding key, so nothing to recheck and nothing to unlock.
        """

    def issue_comment(self, issue: int, comment: int) -> Comment:
        """One comment on an issue."""

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

    def issues(self, *, label: str) -> tuple[Issue, ...]:
        """The agent's own open issues, carrying a marker. Somebody else's issue is not the agent's
        to edit or close, and the label alone is not proof of authorship."""

    def raise_issue(self, new: NewIssue, *, label: str) -> Issue:
        """Start tracking a finding, labelled so a team can find, mute or query the whole set."""

    def edit_issue(self, issue: Issue, body: str) -> None:
        """Bring an existing issue up to date, rather than opening a second one for one problem."""

    def note(self, issue: Issue, body: str) -> None:
        """Say something on the issue. Every closure says why before it happens."""

    def close_issue(self, issue: Issue) -> None: ...

    def proposals(self, *, prefix: str) -> tuple[Proposal, ...]:
        """Open change requests whose branch starts with the agent's own prefix.

        Asked before any fix is prepared. A run that cannot see what it already has open is a run
        that opens a second change request carrying the same edit.
        """

    def push(self, path: Path, *, source: str, target: str) -> None:
        """Send one prepared ref to the hosting platform, under the agent's own credential.

        `source` is a branch ref or a commit that already exists locally; `target` is the ref to
        create or move there. Both are named rather than derived, because the two callers mean
        different things: a fix branch people will read, and the ref the agent keeps its own memory
        in.
        """

    def propose(self, new: NewChange) -> Proposal:
        """Open a change request for a branch that is already pushed."""
