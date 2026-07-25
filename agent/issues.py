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

from agent.findings import Action
from agent.reconcile import Posted, unproven
from agent.scm import marker
from agent.scm.port import NewIssue, Platform, ScmError
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
    label: str = LABEL,
) -> Tracking:
    """Reconcile this run's findings with the issues already open, and record every step.

    A platform failure is recorded rather than raised, for the same reason a review's is: the
    analysis is already paid for, and losing its verdict because an issue could not be edited would
    make the run less reliable than the tracker it writes to.
    """
    record = Tracking()
    try:
        return _track(platform, record, verdict, outcomes, head, limit, label)
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
    label: str,
) -> Tracking:
    existing = {item.key: item for item in platform.issues(label=label) if item.key}
    current = {item.finding.key: item for item in verdict.judged}

    for key, judged in sorted(current.items()):
        body = _body(judged)
        issue = existing.get(key)
        if issue is None:
            if record.raised >= limit:
                # Left for the next run rather than dropped or merged into one issue: a finding
                # squeezed into somebody else's issue is a finding that loses its own key, and one
                # dropped silently is one nobody knows was found.
                record.posted.append(
                    Posted("deferred", key, f"this run's limit of {limit} new issue(s) is reached")
                )
                continue
            opened = platform.raise_issue(
                NewIssue(key=key, title=_title(judged), body=body), label=label
            )
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
        if key in current:
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


def _body(judged: Judged) -> str:
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
    return marker.stamp("\n".join(lines), finding.key)


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
    return (
        f"`{capability}` ran to completion on {head[:12]} and this is no longer among its "
        "findings, so this issue is closed. If it returns, a later run opens a new issue with the "
        "same key."
    )
