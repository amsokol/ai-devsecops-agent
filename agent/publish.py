"""Publishing a decision, and keeping one thread per finding across runs.

Everything here is decided by code from recorded facts: which findings get an inline thread, which
existing thread is updated, which one may be resolved, and which stance the review carries. No model
is consulted, because a comment is the part of a run a human reacts to — one that moves around, or
duplicates itself on every push, teaches a team to filter the agent out of their notifications.

Three rules carry most of the weight:

*A thread is found again by its finding key*, never by author, position or wording. Everything else
about a comment moves between runs.

*A thread is resolved only when the task that owned it looked and found nothing.* Absence in a run
that failed to check is not a fix, and resolving on it would quietly retract a real problem.

*Nothing is published for a commit the run did not analyse.* If the change moved while the run was
working, its comments would point at lines nobody proposed, so the run says so and posts nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from agent.domain import Outcome
from agent.errors import ConfigError
from agent.findings import Action
from agent.repo import ChangeView
from agent.scm import marker
from agent.scm.port import Identity, NewThread, Platform, ScmError, Stance
from agent.verdict import Judged, TaskOutcome, Verdict

RESOLVED_NOTE = (
    "This is no longer present on the current head, and the check that owns it ran to completion. "
    "Resolving; it will come back on this thread if it returns."
)

RETURNED_NOTE = (
    "This is present again on the current head, so this thread is open once more. The first "
    "comment has the current detail."
)


@dataclass(frozen=True, slots=True)
class Posted:
    """One thing that happened on the platform, or was deliberately not attempted."""

    what: str
    key: str = ""
    detail: str = ""

    def as_json(self) -> dict[str, Any]:
        return {"what": self.what, "key": self.key, "detail": self.detail}


@dataclass(slots=True)
class Publication:
    """What a run published, as whom, what it left alone, and why it stopped if it did."""

    published: bool = False
    stance: Stance | None = None
    reference: str = ""
    identity: Identity | None = None
    posted: list[Posted] = field(default_factory=list)
    withheld: str = ""
    """Why nothing was published. Empty when the run did publish."""
    caution: str = ""
    """Published, but with something the team needs to know about how it will be read."""
    failure: str = ""

    def as_json(self) -> dict[str, Any]:
        return {
            "published": self.published,
            "stance": self.stance.value if self.stance else None,
            "reference": self.reference,
            "identity": self.identity.as_json() if self.identity else None,
            "posted": [item.as_json() for item in self.posted],
            "withheld": self.withheld,
            "caution": self.caution,
            "failure": self.failure,
        }


def publish_review(
    platform: Platform,
    *,
    number: int,
    verdict: Verdict,
    report: str,
    head: str,
    outcomes: tuple[TaskOutcome, ...],
    change: ChangeView | None,
) -> Publication:
    """Post one review, reconcile the threads, and record every step.

    A platform failure is caught and recorded rather than raised. The verdict is already decided and
    the exit code already carries it; losing that because a comment could not be posted would make
    the gate less reliable than the platform it talks to.
    """
    record = Publication()
    try:
        return _publish(platform, record, number, verdict, report, head, outcomes, change)
    except ScmError as error:
        record.failure = str(error)
        return record


def _publish(
    platform: Platform,
    record: Publication,
    number: int,
    verdict: Verdict,
    report: str,
    head: str,
    outcomes: tuple[TaskOutcome, ...],
    change: ChangeView | None,
) -> Publication:
    record.identity = platform.identity()
    record.caution = caution_for(record.identity)
    proposed = platform.change(number)
    if proposed.draft:
        record.withheld = "the change is a draft, so the run reports without publishing"
        return record
    if proposed.head != head:
        record.withheld = (
            f"the change moved while this run was working: it analysed {head[:12]} and the "
            f"platform now has {proposed.head[:12]}. A newer run covers the newer commit"
        )
        return record

    # Only threads carrying a marker: everything else on the change belongs to a human, and an agent
    # that edits or resolves a human's comment is one a team removes.
    existing = {item.key: item for item in platform.threads(number) if item.key}
    current = {item.finding.key: item for item in verdict.judged}

    fresh: list[NewThread] = []
    for key, judged in current.items():
        body = _thread_body(judged)
        thread = existing.get(key)
        if thread is None:
            attachable = _attachable(judged, change)
            if attachable is None:
                # Kept in the review body instead: a comment on a line the change did not touch is
                # refused by the platform, and one on a moved line would blame the wrong author.
                record.posted.append(Posted("in-body", key, "no line of this change to attach to"))
                continue
            path, line = attachable
            fresh.append(NewThread(key=key, body=body, path=path, line=line))
            continue
        if thread.body.strip() != body.strip():
            platform.edit(thread, body)
            record.posted.append(Posted("updated", key, "the finding changed"))
        elif not thread.resolved:
            record.posted.append(Posted("unchanged", key))
        if thread.resolved:
            # A problem that came back to a thread somebody resolved is a problem nobody will see.
            platform.reply(thread, RETURNED_NOTE)
            platform.unresolve(thread)
            record.posted.append(Posted("reopened", key, "the finding is back"))

    for key, thread in sorted(existing.items()):
        if key in current or thread.resolved:
            continue
        reason = _resolvable(key, outcomes)
        if reason is not None:
            record.posted.append(Posted("kept-open", key, reason))
            continue
        platform.reply(thread, RESOLVED_NOTE)
        platform.resolve(thread)
        record.posted.append(Posted("resolved", key))

    asked = Stance.of(verdict.result)
    review = platform.review(number, body=report, stance=asked, head=head, threads=fresh)
    record.posted += [Posted("thread", item.key, f"{item.path}:{item.line}") for item in fresh]
    if review.stance is not asked:
        # Recorded, because a run that claimed the stance it asked for would say it requested
        # changes on a pull request showing none, and a reader of both would trust neither.
        record.posted.append(
            Posted(
                "commented-instead",
                detail=f"the platform refused to record this as {asked.value}",
            )
        )
    record.reference = review.reference
    record.stance = review.stance
    record.published = True
    if review.author and record.identity is not None and review.author != record.identity.login:
        # The name from the thing that was actually published, which beats the credential's own
        # account of itself: an App's token proves it is an integration and never says which one.
        record.identity = replace(record.identity, login=review.author)
        record.caution = caution_for(record.identity)
    return record


def caution_for(identity: Identity) -> str:
    """Whether these comments will read — and behave — as a person's.

    Not a refusal, because a dedicated machine account is a legitimate way to run this and the
    platform shows it as an ordinary user. It is said out loud, once, because the consequence is not
    cosmetic: a workflow that starts a run on human comments and filters bots cannot filter an
    ordinary user, so the agent's own comment wakes the agent, which comments again.
    """
    if identity.trustworthy:
        return ""
    if not identity.known:
        return (
            "the credential's account could not be read, so it is not known whether these comments "
            "will be shown as a machine's. If the workflow wakes on comments, filter by the login "
            "that posts them"
        )
    return (
        f"published as {identity.login}, which the platform shows as a person rather than a bot. A "
        "machine's judgement under a human name is hard to tell from a colleague's opinion, and a "
        f"workflow that filters bot comments will not filter {identity.login}: filter that login "
        "explicitly, or publish with a bot credential"
    )


def _thread_body(judged: Judged) -> str:
    """One finding, on the line it concerns, in the order a reader needs it.

    What, then why, then what to do — and the marker last. The action is stated because a comment
    that reads like a blocker while the run only commented is a comment that starts an argument.
    """
    finding = judged.finding
    lines = [
        f"**{finding.severity.value}** `{finding.klass.value}` — {finding.summary}",
        "",
        finding.rationale,
    ]
    if finding.remediation:
        lines += ["", f"**Remediation.** {finding.remediation}"]
    if judged.action is Action.COMMENT:
        lines += [
            "",
            "This does not block the merge"
            + (
                ": the evidence behind it is heuristic, and policy only lets demonstrated findings "
                "block."
                if judged.capped
                else "."
            ),
        ]
    return marker.stamp("\n".join(lines), judged.finding.key)


def _attachable(judged: Judged, change: ChangeView | None) -> tuple[str, int] | None:
    """The line to comment on, when the change itself contains one.

    Read from git rather than trusted from the finding: a line number is the most volatile thing a
    task reports, and the platform rejects a review comment outside the diff outright.
    """
    location = judged.finding.location
    if change is None or location is None or location.line is None:
        return None
    try:
        touched = change.lines(location.path)
    except ConfigError, OSError:
        # A path git will not diff — outside the tree, or gone — is simply not attachable.
        return None
    if any(line.line == location.line for line in touched.added):
        return touched.path, location.line
    return None


def _resolvable(key: str, outcomes: tuple[TaskOutcome, ...]) -> str | None:
    """Why a thread whose finding is gone must stay open, or `None` when it may be resolved.

    The owning capability is read from the head of the key, which is where it is by construction.
    Requiring that capability's own task to have finished `clean` is the whole rule: a task that was
    exhausted, or that never ran because the run was narrowed, says nothing about the finding.
    """
    capability = key.split(":", 1)[0]
    owning = [item for item in outcomes if item.capability == capability]
    if not owning:
        return f"{capability} did not run in this run, so its absence proves nothing"
    if any(item.outcome is not Outcome.CLEAN for item in owning):
        states = ", ".join(sorted({item.outcome.value for item in owning}))
        return f"{capability} finished {states} rather than clean"
    return None
