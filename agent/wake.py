"""Being woken by somebody's comment: what was said, who said it, and whether to act at all.

Everything here happens before the first model call, and all of it is the agent's own arithmetic. A
comment is the one input the agent cannot check the shape of — it is a person writing to a machine —
so what a comment is *allowed to cause* is decided by code, from facts the platform states:

*Whose comment it was.* A bot's does not wake the agent, and neither does the agent's own: a run
that answers itself answers forever, and each turn of that loop costs a model.

*Whether they may write here.* A comment is a way to spend somebody else's budget and, later, to
grant permission. Without this check anybody who can type in an issue decides both. An answer the
platform will not give is treated as "no", because the permissive mistake here cannot be taken back.

*Whether the conversation is the agent's own.* Found by the marker, never by the label or the
author — the same rule publishing uses. A thread with no marker is somebody else's, and there is no
finding key in it to recheck or unlock.

A refusal is not silence: the run is recorded, says which rule stopped it, and exits as a success.
"declined to answer its own comment" is a property that has to be visible to be believed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent.scm.port import Comment, Identity, Issue, Platform, ScmError, Thread

BOT_SUFFIX = "[bot]"


@dataclass(frozen=True, slots=True)
class Wake:
    """What the event said: who acted, which comment, and where it was left.

    Both places are one type because everything the agent does with a wake is the same either way,
    down to reading the marker; only the endpoints differ.
    """

    actor: str
    comment: int
    issue: int | None = None
    change: int | None = None

    @property
    def where(self) -> str:
        if self.issue is not None:
            return f"issue #{self.issue}"
        return f"change #{self.change}"

    def as_json(self) -> dict[str, Any]:
        return {
            "actor": self.actor,
            "comment": self.comment,
            "issue": self.issue,
            "change": self.change,
        }


@dataclass(frozen=True, slots=True)
class Woken:
    """The conversation this run was woken in, as the platform has it."""

    wake: Wake
    key: str
    """The finding the conversation is about, read from the marker."""
    remark: str
    """What the agent itself said there — the issue's body, or the first comment of the thread. The
    person's words only make sense next to it, and a classifier given one without the other has to
    guess what "this" refers to."""
    said: Comment
    issue: Issue | None = None
    thread: Thread | None = None

    @property
    def capability(self) -> str:
        """The check the finding belongs to, which is the part of the key that never drifts."""
        return self.key.split(":", 1)[0]

    def as_json(self) -> dict[str, Any]:
        return self.wake.as_json() | {
            "where": self.wake.where,
            "finding": self.key,
            "capability": self.capability,
            "said": self.said.as_json(),
        }


def admit(wake: Wake, *, platform: Platform | None, identity: Identity | None) -> Woken | str:
    """The conversation to work in, or the reason this wake is refused.

    Ordered by cost: the name the event carried is judged first, because a bot's comment can be
    refused without asking anybody anything. Then the platform is asked what that account may do,
    and only then is the conversation read. Nothing is spent on a model before all three pass.
    """
    refusal = _who(wake, identity=identity)
    if refusal:
        return refusal
    if platform is None:
        return (
            "declined: a run woken by a comment has to read that comment, and this one has no "
            "credential for the platform. Nothing was answered"
        )
    try:
        return _conversation(wake, platform=platform)
    except ScmError as error:
        return (
            f"declined: {wake.where} could not be read ({error}), so this run does not know "
            "what it was woken about"
        )


def _who(wake: Wake, *, identity: Identity | None) -> str:
    if not wake.actor:
        return (
            "declined: nothing said whose comment woke this run, and an unattributed wake "
            "cannot be told from the agent's own comment"
        )
    if wake.actor.endswith(BOT_SUFFIX):
        return (
            f"declined: {wake.actor} is a bot, and a machine's comment does not wake the agent. "
            "A run per comment between two machines is a bill with no reader"
        )
    if identity is not None and identity.login and identity.login == wake.actor:
        return (
            f"declined: this run was woken by {wake.actor}, which is the account the agent "
            "publishes as. Answering its own comment is a loop, and each turn of it costs a model"
        )
    return ""


def _conversation(wake: Wake, *, platform: Platform) -> Woken | str:
    """Read the comment and the thread it is in, and refuse anything that is not the agent's own."""
    allowed = platform.authority(wake.actor)
    if not allowed.known:
        return (
            f"declined: whether {wake.actor} may write to this repository could not be "
            "established, and a comment from an account with no stated write access is not an "
            "instruction. Grant the credential permission to read collaborators, or start the "
            "run yourself"
        )
    if not allowed.writes:
        return (
            f"declined: {wake.actor} has no write access to this repository, so their comment does "
            "not start a run. Anybody who could comment would otherwise be spending the budget and "
            "granting the permissions"
        )
    if wake.issue is not None:
        return _on_issue(wake, platform=platform)
    return _on_change(wake, platform=platform)


def _on_issue(wake: Wake, *, platform: Platform) -> Woken | str:
    assert wake.issue is not None  # noqa: S101 - the caller dispatched on it
    issue = platform.issue_at(wake.issue)
    if issue is None:
        return (
            f"declined: issue #{wake.issue} is not one the agent raised — it carries no marker — "
            "so there is no finding of its own to recheck or unlock"
        )
    said = platform.issue_comment(wake.issue, wake.comment)
    refusal = _said(wake, said)
    if refusal:
        return refusal
    return Woken(wake=wake, key=issue.key, remark=issue.body, said=said, issue=issue)


def _on_change(wake: Wake, *, platform: Platform) -> Woken | str:
    assert wake.change is not None  # noqa: S101 - the caller dispatched on it
    said = platform.change_comment(wake.change, wake.comment)
    refusal = _said(wake, said)
    if refusal:
        return refusal
    # The thread is found by the comment the marker lives in: a reply's own body carries nothing,
    # and the first comment of the thread is what the agent wrote and can recognise.
    anchor = str(said.parent or said.id)
    thread = next(
        (item for item in platform.threads(wake.change) if item.comment == anchor and item.key),
        None,
    )
    if thread is None:
        return (
            f"declined: comment {wake.comment} on change #{wake.change} is not in one of the "
            "agent's own threads, so there is no finding it is about. A remark the agent never "
            "made is not one it can answer for"
        )
    return Woken(wake=wake, key=thread.key, remark=thread.body, said=said, thread=thread)


def _said(wake: Wake, said: Comment) -> str:
    if said.bot:
        return (
            f"declined: comment {wake.comment} was written by a machine ({said.author}), whatever "
            "the event said, and a machine's comment does not wake the agent"
        )
    if said.author and wake.actor and said.author != wake.actor:
        # The two arrive from one event and normally agree. When they do not, something is passing
        # one person's authority together with another person's words, and the words are what would
        # be acted on.
        return (
            f"declined: this run was told {wake.actor} woke it, but comment {wake.comment} was "
            f"written by {said.author}. The account that is checked has to be the one that spoke"
        )
    if not said.body.strip():
        return f"declined: comment {wake.comment} has no text, so there is nothing to act on"
    return ""
