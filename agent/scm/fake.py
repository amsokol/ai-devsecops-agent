"""A platform that lives in memory, so publishing can be tested without a network.

It keeps state the way a real one does: a thread opened by one review is there for the next, with
the marker read out of the body exactly as the adapter reads it. That is what makes a test of
idempotency worth anything: an assertion that the second run posted nothing new means something only
if that run could see what the first one left.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from agent.scm import marker
from agent.scm.port import (
    Authority,
    Change,
    Comment,
    Identity,
    Issue,
    NewChange,
    NewIssue,
    NewThread,
    Proposal,
    Review,
    ScmError,
    Stance,
    Thread,
)


@dataclass(frozen=True, slots=True)
class Call:
    what: str
    key: str = ""
    detail: str = ""


@dataclass
class FakePlatform:
    head: str = "head"
    author: str = "somebody"
    login: str = "ai-devsecops-agent[bot]"
    """Who published things are shown as. A bot by default, because that is the only identity a run
    should publish under."""
    is_bot: bool = True
    known_identity: bool = True
    nameless: bool = False
    """Report an integration without naming it, as an App's installation token does: it proves what
    the caller is and not which App, and the name only appears on what gets published."""
    token: str = ""
    """What `reading_token` answers: the credential a task's reads of the platform API may carry."""
    draft: bool = False
    fork: str = ""
    """When set, the head lives in that repository rather than this one, which is how a change from
    an outside contributor is tested: the run may read it and may execute nothing from it."""
    refuse_own_review: bool = False
    """Behave like GitHub does on a change the credentials themselves opened: no review event at
    all, approving or otherwise, so the decision arrives as a plain comment."""
    fail: str = ""
    """When set, every call raises it, which is how a run's tolerance of an outage is tested."""
    opened: list[Thread] = field(default_factory=list)
    replies: list[tuple[str, str]] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)
    reviews: list[tuple[Stance, str]] = field(default_factory=list)
    tracked: list[Issue] = field(default_factory=list)
    closed: list[Issue] = field(default_factory=list)
    notes: list[tuple[str, str]] = field(default_factory=list)
    labels: dict[int, tuple[str, ...]] = field(default_factory=dict)
    proposed: list[Proposal] = field(default_factory=list)
    pushed: list[str] = field(default_factory=list)
    bodies: list[tuple[str, str]] = field(default_factory=list)
    unpushable: tuple[str, ...] = ()
    """Branches the platform will refuse, which is what an earlier run's leftover branch does."""
    said: dict[int, Comment] = field(default_factory=dict)
    """Comments somebody left, by identifier: what a run woken by one of them reads."""
    writers: tuple[str, ...] = ()
    """Accounts with write access. Everyone else is a reader, and an account named in `strangers` is
    one the platform will not answer about at all."""
    strangers: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        return "fake"

    def reading_token(self) -> str:
        return self.token

    def identity(self) -> Identity:
        self._check()
        return Identity(
            login="" if self.nameless else self.login,
            bot=self.is_bot,
            known=self.known_identity,
        )

    def authority(self, login: str) -> Authority:
        self._check()
        if not login or login in self.strangers:
            return Authority(login=login, writes=False, known=False)
        return Authority(login=login, writes=login in self.writers)

    def change(self, number: int) -> Change:
        self._check()
        return Change(
            number=number,
            head=self.head,
            author=self.author,
            draft=self.draft,
            repository=self.fork or "product/repo",
            elsewhere=bool(self.fork),
        )

    def change_comment(self, number: int, comment: int) -> Comment:
        self._check()
        return self._said(comment)

    def issue_at(self, number: int) -> Issue | None:
        self._check()
        found = next((item for item in self.tracked if item.number == number), None)
        if found is None:
            raise ScmError(f"no issue {number}")
        return found if found.key else None

    def issue_comment(self, issue: int, comment: int) -> Comment:
        self._check()
        return self._said(comment)

    def _said(self, comment: int) -> Comment:
        found = self.said.get(comment)
        if found is None:
            raise ScmError(f"no comment {comment}")
        return found

    def threads(self, number: int) -> tuple[Thread, ...]:
        self._check()
        return tuple(item for item in self.opened if item.number == number)

    def review(
        self, number: int, *, body: str, stance: Stance, head: str, threads: Sequence[NewThread]
    ) -> Review:
        self._check()
        if self.refuse_own_review and stance is not Stance.COMMENT:
            stance = Stance.COMMENT
        self.reviews.append((stance, body))
        self.calls.append(Call("review", detail=stance.value))
        for item in threads:
            self.opened.append(
                Thread(
                    id=f"thread-{len(self.opened) + 1}",
                    # Numeric, as the platform's own is: a wake finds its thread by the identifier
                    # of the comment the marker lives in, and a fake that used a different shape
                    # here would let that lookup pass in tests and fail in life.
                    comment=str(len(self.opened) + 1),
                    key=marker.read(item.body),
                    body=item.body,
                    number=number,
                    path=item.path,
                    line=item.line,
                )
            )
            self.calls.append(Call("thread", key=item.key, detail=f"{item.path}:{item.line}"))
        return Review(
            reference=f"fake://review/{len(self.reviews)}", stance=stance, author=self.login
        )

    def edit(self, thread: Thread, body: str) -> None:
        self._check()
        self._change(thread, body=body)
        self.calls.append(Call("edit", key=thread.key))

    def reply(self, thread: Thread, note: str) -> None:
        self._check()
        # A reply is its own comment, and leaving the first one alone is the point: the marker lives
        # there, and a fake that rewrote it would hide a bug where the agent loses its own anchor.
        self.replies.append((thread.key, note))
        self.calls.append(Call("reply", key=thread.key, detail=note))

    def resolve(self, thread: Thread) -> None:
        self._check()
        self._change(thread, resolved=True)
        self.calls.append(Call("resolve", key=thread.key))

    def unresolve(self, thread: Thread) -> None:
        self._check()
        self._change(thread, resolved=False)
        self.calls.append(Call("unresolve", key=thread.key))

    def issues(self, *, label: str) -> tuple[Issue, ...]:
        self._check()
        return tuple(
            item for item in self.tracked if label in self.labels.get(item.number, ()) and item.key
        )

    def raise_issue(self, new: NewIssue, *, label: str) -> Issue:
        self._check()
        number = len(self.tracked) + len(self.closed) + 1
        issue = Issue(
            number=number,
            key=new.key,
            title=new.title,
            body=new.body,
            reference=f"fake://issue/{number}",
        )
        self.tracked.append(issue)
        self.labels[number] = (label,)
        self.calls.append(Call("raise_issue", key=new.key, detail=new.title))
        return issue

    def edit_issue(self, issue: Issue, body: str) -> None:
        self._check()
        self.tracked = [
            replace(item, body=body) if item.number == issue.number else item
            for item in self.tracked
        ]
        self.calls.append(Call("edit_issue", key=issue.key))

    def note(self, issue: Issue, body: str) -> None:
        self._check()
        self.notes.append((issue.key, body))
        self.calls.append(Call("note", key=issue.key, detail=body))

    def close_issue(self, issue: Issue) -> None:
        self._check()
        self.tracked = [item for item in self.tracked if item.number != issue.number]
        self.closed.append(issue)
        self.calls.append(Call("close_issue", key=issue.key))

    def proposals(self, *, prefix: str) -> tuple[Proposal, ...]:
        self._check()
        return tuple(item for item in self.proposed if item.head.startswith(prefix))

    def push(self, path: Path, *, source: str, target: str) -> None:
        self._check()
        name = target.removeprefix("refs/heads/")
        if name in self.unpushable:
            raise ScmError(f"pushing {name} failed: it is not a fast-forward")
        self.pushed.append(name)
        self.calls.append(Call("push", detail=name))

    def propose(self, new: NewChange) -> Proposal:
        self._check()
        if new.head not in self.pushed:
            # A change request for a branch nobody sent is the platform's error to give, and a fake
            # that allowed it would hide the ordering bug that causes it.
            raise ScmError(f"no branch named {new.head} on the platform")
        number = len(self.proposed) + 100
        opened = Proposal(
            number=number,
            head=new.head,
            reference=f"fake://change/{number}",
            author=self.login,
        )
        self.proposed.append(opened)
        self.bodies.append((new.head, new.body))
        self.calls.append(Call("propose", detail=new.title))
        return opened

    def _find(self, thread: Thread) -> Thread:
        return next(item for item in self.opened if item.id == thread.id)

    def _change(self, thread: Thread, **fields: object) -> None:
        self.opened = [
            replace(item, **fields) if item.id == thread.id else item  # type: ignore[arg-type]
            for item in self.opened
        ]

    def _check(self) -> None:
        if self.fail:
            raise ScmError(self.fail)
