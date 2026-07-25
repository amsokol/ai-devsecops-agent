"""A platform that lives in memory, so publishing can be tested without a network.

It keeps state the way a real one does: a thread opened by one review is there for the next, with
the marker read out of the body exactly as the adapter reads it. That is what makes a test of
idempotency worth anything: an assertion that the second run posted nothing new means something only
if that run could see what the first one left.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace

from agent.scm import marker
from agent.scm.port import Change, Identity, NewThread, Review, ScmError, Stance, Thread


@dataclass(frozen=True, slots=True)
class Call:
    what: str
    key: str = ""
    detail: str = ""


@dataclass
class FakePlatform:
    head: str = "head"
    author: str = "somebody"
    login: str = "devsecops-agent[bot]"
    """A bot by default, because that is the only identity a run should publish under."""
    is_bot: bool = True
    known_identity: bool = True
    draft: bool = False
    refuse_own_review: bool = False
    """Behave like GitHub does on a change the credentials themselves opened: no review event at
    all, approving or otherwise, so the decision arrives as a plain comment."""
    fail: str = ""
    """When set, every call raises it, which is how a run's tolerance of an outage is tested."""
    opened: list[Thread] = field(default_factory=list)
    replies: list[tuple[str, str]] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)
    reviews: list[tuple[Stance, str]] = field(default_factory=list)

    @property
    def name(self) -> str:
        return "fake"

    def identity(self) -> Identity:
        self._check()
        return Identity(login=self.login, bot=self.is_bot, known=self.known_identity)

    def change(self, number: int) -> Change:
        self._check()
        return Change(number=number, head=self.head, author=self.author, draft=self.draft)

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
                    comment=f"comment-{len(self.opened) + 1}",
                    key=marker.read(item.body),
                    body=item.body,
                    number=number,
                )
            )
            self.calls.append(Call("thread", key=item.key, detail=f"{item.path}:{item.line}"))
        return Review(reference=f"fake://review/{len(self.reviews)}", stance=stance)

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
