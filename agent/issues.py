"""Tracking findings as issues on the default branch, one issue per finding, across runs.

A maintenance run has no conversation to write in: there is no change request whose diff a reviewer
is reading, so a finding that is not tracked somewhere is a finding nobody will see. Issues are that
somewhere, and they are the part of the agent a team lives with week after week — which makes
restraint the whole design:

*One issue per finding, found again by its key.* Not by title, which is prose and gets edited, and
not by the label, which anybody can apply. A second issue for one problem is how a tracker becomes a
place people stop looking.

*A closure states its evidence.* An issue is closed only when the capability that owns the finding
finished clean and the finding is gone from this run — the same rule a review thread is resolved by.
Closing on absence alone would turn the first scanner outage into a week of imaginary fixes.

*Silence when nothing is proved.* A finding whose owning check failed leaves its issue exactly as it
was: no comment, no label change, nothing. "Still present" would be as unfounded as a closure, and a
weekly reminder that nothing is known is what teaches people to mute the agent.

The open set is read once, before anything is written. That order is not a matter of style: GitHub's
label listing is a secondary index, and it took five seconds to admit a new issue when this path was
first driven against a real repository. Reading it up front makes a run's own writes irrelevant to
what it sees, and "one run at a time" keeps two runs from racing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent.escalate import Escalation
from agent.findings import Action, Finding
from agent.reconcile import Posted, unproven
from agent.scm import marker
from agent.scm.port import Issue, NewIssue, Platform, ScmError
from agent.unlock import Approval, held, read, render, stamped
from agent.verdict import Judged, TaskOutcome, Verdict

LABEL = "agent"
"""One label for everything the agent tracks, so a team can find, query or mute the whole set."""


@dataclass(slots=True)
class Tracking:
    """What became of the tracked set: what was opened, brought up to date, closed or left alone."""

    posted: list[Posted] = field(default_factory=list)
    raised: int = 0
    closed: int = 0
    failure: str = ""
    numbers: dict[str, int] = field(default_factory=dict)
    """Issue number per finding key, so a change request can name the issue it answers."""

    def as_json(self) -> dict[str, Any]:
        return {
            "posted": [item.as_json() for item in self.posted],
            "raised": self.raised,
            "closed": self.closed,
            "failure": self.failure,
            "tracked": dict(sorted(self.numbers.items())),
        }


def track_findings(
    platform: Platform,
    *,
    verdict: Verdict,
    outcomes: tuple[TaskOutcome, ...],
    head: str,
    limit: int,
    escalations: tuple[Escalation, ...] = (),
    label: str = LABEL,
    known: tuple[Issue, ...] | None = None,
    approvals: dict[str, Approval] | None = None,
) -> Tracking:
    """Reconcile this run's findings with the issues already open, and record every step.

    A platform failure is recorded rather than raised, for the same reason a review's is: the
    analysis is already paid for, and losing its verdict because an issue could not be edited would
    make the run less reliable than the tracker it writes to.

    `known` is the open set when the run already read it — a run that plans fixes has to, because
    which holds a person released decides what it may ship. Listing it twice would ask the platform
    the same question either side of the work and let the two answers differ.
    """
    record = Tracking()
    try:
        return _track(
            platform,
            record,
            verdict,
            outcomes,
            head,
            limit,
            escalations,
            label,
            known,
            approvals or {},
        )
    except ScmError as error:
        record.failure = str(error)
        return record


def _track(
    platform: Platform,
    record: Tracking,
    verdict: Verdict,
    outcomes: tuple[TaskOutcome, ...],
    head: str,
    limit: int,
    escalations: tuple[Escalation, ...],
    label: str,
    known: tuple[Issue, ...] | None,
    approvals: dict[str, Approval],
) -> Tracking:
    listed = platform.issues(label=label) if known is None else known
    existing = {item.key: item for item in listed if item.key}
    # Findings and escalations are reconciled by one loop because they are the same kind of thing to
    # a reader: one issue, found again by its key, closed when the check that owns it says so.
    wanted = {
        item.finding.key: (_title(item), _body(item, approvals.get(item.finding.key)))
        for item in verdict.judged
    }
    wanted |= {item.key: (item.title, item.body) for item in escalations}
    _keep_approvals(platform, record, existing, approvals, rewritten=frozenset(wanted))
    # A broken check hides everything it would have found, so the news that it is broken does not
    # queue behind the findings of the checks that still work.
    exempt = {item.key for item in escalations}

    for key, (title, body) in sorted(wanted.items()):
        issue = existing.get(key)
        if issue is None:
            if record.raised >= limit and key not in exempt:
                # Left for the next run rather than dropped or merged into one issue: a finding
                # squeezed into somebody else's issue is a finding that loses its own key, and one
                # dropped silently is one nobody knows was found.
                record.posted.append(
                    Posted("deferred", key, f"this run's limit of {limit} new issue(s) is reached")
                )
                continue
            opened = platform.raise_issue(NewIssue(key=key, title=title, body=body), label=label)
            record.raised += 1
            record.numbers[key] = opened.number
            record.posted.append(Posted("raised", key, opened.reference))
            continue
        record.numbers[key] = issue.number
        if issue.body.strip() != body.strip():
            platform.edit_issue(issue, body)
            record.posted.append(Posted("updated", key, "the finding changed"))
        else:
            record.posted.append(Posted("unchanged", key))

    for key, issue in sorted(existing.items()):
        if key in wanted:
            continue
        reason = unproven(key, outcomes)
        if reason is not None:
            record.posted.append(Posted("kept-open", key, reason))
            continue
        platform.note(issue, _closing_note(key, head))
        platform.close_issue(issue)
        record.closed += 1
        record.posted.append(Posted("closed", key))
    return record


def _keep_approvals(
    platform: Platform,
    record: Tracking,
    existing: dict[str, Issue],
    approvals: dict[str, Approval],
    *,
    rewritten: frozenset[str],
) -> None:
    """Write down an approval on an issue this run is not otherwise rewriting.

    The ordinary path carries a fresh approval into the body along with everything else the finding
    says. This is for the run where the check that owns it did not finish: the issue keeps its old
    body and is left alone, and without this the grant would exist only in that run's record. The
    next run would find no stamp and ask the person again for permission they already gave, which
    the knowledge names as a defect in its own right.
    """
    for key, approval in sorted(approvals.items()):
        issue = existing.get(key)
        if issue is None or key in rewritten or read(issue.body) == approval:
            continue
        platform.edit_issue(issue, stamped(issue.body, approval))
        record.posted.append(Posted("approved", key, approval.sentence))


def _title(judged: Judged) -> str:
    """A title built from the parts that identify the problem and none that drift.

    No version and no advisory identifier: both change while the problem stays the same, and a title
    that changes is a title somebody's saved search stops matching. The key in the body is what the
    agent itself reads; this is for the human scanning a list.
    """
    finding = judged.finding
    subject = finding.subject
    what = subject.package or subject.path or finding.capability.rsplit("/", 1)[-1]
    return f"agent: {finding.capability.rsplit('/', 1)[-1]} — {what}"


def _body(judged: Judged, approval: Approval | None = None) -> str:
    """The finding, its evidence, and what to do about it — then the marker.

    Written to be read on its own, because an issue is found weeks later by somebody who never saw
    the run: what it is, why it matters, what would fix it, and how sure the agent is.
    """
    finding = judged.finding
    lines = [
        f"**{finding.severity.value}** `{finding.klass.value}` — {finding.summary}",
        "",
        finding.rationale,
    ]
    if finding.remediation:
        lines += ["", f"**Remediation.** {finding.remediation}"]
    facts = [
        ("Capability", f"`{finding.capability}`"),
        ("Subject", _subject(judged)),
        ("Moves to", finding.target),
        ("Advisory", finding.advisory),
        ("Where", _where(judged)),
        ("Evidence", judged.reliability.value),
    ]
    lines += ["", *[f"- {name}: {value}" for name, value in facts if value]]
    if judged.action is Action.COMMENT and judged.capped:
        lines += [
            "",
            "This is reported rather than blocking: the evidence behind it is heuristic, and "
            "policy only lets demonstrated findings block.",
        ]
    lines += _decision(finding, approval)
    return marker.stamp("\n".join(lines), finding.key)


def _decision(finding: Finding, approval: Approval | None) -> list[str]:
    """The one paragraph a person is here to act on, when the finding waits for them.

    Both halves are written for somebody arriving at this issue cold: what is being asked, and what
    saying yes will cause. An approval, once given, is stated in words and stamped in a comment the
    agent reads on later runs, so the question is asked exactly once.
    """
    if approval is not None:
        return [
            "",
            f"**{approval.sentence}** A run will prepare the change, verify it and open it for "
            "review; this issue stays open until that is merged.",
            "",
            render(approval),
        ]
    hold = held(finding)
    if not hold:
        return []
    return [
        "",
        f"**Waiting for a person.** This will not be changed automatically, because {hold}.",
        "",
        "Comment here to approve it — plain words, no phrase to match — and the next run prepares "
        "the change, verifies it against this product's own commands and opens it for review. "
        "Until then every run reports it and leaves the code alone.",
    ]


def _subject(judged: Judged) -> str:
    subject = judged.finding.subject
    parts = [subject.ecosystem, subject.package, subject.version]
    return " ".join(f"`{part}`" for part in parts if part)


def _where(judged: Judged) -> str:
    location = judged.finding.location
    if location is None:
        return ""
    return f"`{location.path}`" + (f" line {location.line}" if location.line else "")


def _closing_note(key: str, head: str) -> str:
    """Why this issue is being closed, stated before it happens and naming what looked.

    A closure with no evidence cannot be told from one made because a scanner broke, and the
    difference matters most to whoever reads the issue a month later.
    """
    capability = key.split(":", 1)[0]
    if ":failure:" in key:
        return (
            f"`{capability}` ran to completion on {head[:12]}, so the failure this issue reports "
            "is over and it is closed. What that check covers is watched again from this run on."
        )
    return (
        f"`{capability}` ran to completion on {head[:12]} and this is no longer among its "
        "findings, so this issue is closed. If it returns, a later run opens a new issue with the "
        "same key."
    )
