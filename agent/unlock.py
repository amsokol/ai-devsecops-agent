"""Findings that wait for a person, and the approvals that release them.

Some changes are not the agent's to make alone. A major move breaks callers by definition, and the
library says one ships only after somebody with write access says so on its issue. Until now that
sentence was addressed to a model, which means the guarantee was "the session read the document and
agreed" — the same class of promise as "never force-push" would be if there were a push tool.

So the hold is arithmetic here, and it has two halves that fail in opposite directions:

*Declared.* A task marks a finding as needing approval. It has to be the one who says so for the
cases no comparison can see — a floating action pin moving `@v5` to `@v7`, a raised toolchain floor,
a runtime image tag. Those are major by policy and identical to a patch bump as strings.

*Proven.* The agent holds a routine move it can show is a semantic-version major, whatever the task
said. A session that forgets to declare one would otherwise switch the policy off by omission, and
the failure would be silent: a shipped change request nobody was asked about.

The approval itself lives in the issue body, as a stamp next to the words that granted it. A ref or
a database would work as well mechanically and worse for a person: the grant would be somewhere the
people governed by it cannot see, and "who approved this" would be a question for whoever has shell
access rather than for whoever reads the issue.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from agent.findings import Finding, Kind, Klass
from agent.scm.port import Issue
from agent.tools.versions import Step, compare_versions

PREFIX = "<!-- agent:unlocked "
SUFFIX = " -->"
_STAMP = re.compile(
    r"<!--\s*agent:unlocked\s+by=(?P<by>\S+)\s+comment=(?P<comment>\d+)\s+at=(?P<at>\S+)\s*-->"
)


@dataclass(frozen=True, slots=True)
class Approval:
    """Somebody with write access said go ahead, and where they said it."""

    by: str
    comment: int
    at: str
    """The day it was granted, as a date. A time would suggest a precision the record does not need
    and would make the issue body differ between runs that mean the same thing."""

    @property
    def sentence(self) -> str:
        return f"Approved by @{self.by} on {self.at} (comment {self.comment})."

    def as_json(self) -> dict[str, Any]:
        return {"by": self.by, "comment": self.comment, "at": self.at}


def render(approval: Approval) -> str:
    return f"{PREFIX}by={approval.by} comment={approval.comment} at={approval.at}{SUFFIX}"


def read(body: str) -> Approval | None:
    """The approval a body records, or `None`.

    Parsed rather than inferred. "Somebody wrote approved in a comment somewhere on this issue" is
    exactly the reading that lets a quoted sentence, a bot, or a person without write access grant
    permission; the stamp is only ever written by the agent after it checked all three.
    """
    found = _STAMP.search(body)
    if found is None:
        return None
    return Approval(by=found.group("by"), comment=int(found.group("comment")), at=found.group("at"))


def stamped(body: str, approval: Approval) -> str:
    """The body with this approval recorded, replacing an earlier one rather than adding another."""
    if _STAMP.search(body):
        return _STAMP.sub(render(approval), body, count=1)
    return f"{body.rstrip()}\n\n{approval.sentence}\n\n{render(approval)}\n"


def granted(issues: tuple[Issue, ...]) -> dict[str, Approval]:
    """Every approval the open issues carry, by finding key.

    Read once, before anything is planned. An approval given last month has to work the same as one
    given a minute ago: the person said yes on that issue, and nothing since has asked them again.
    """
    found: dict[str, Approval] = {}
    for issue in issues:
        approval = read(issue.body)
        if issue.key and approval is not None:
            found[issue.key] = approval
    return found


def is_routine_quarantine(finding: Finding) -> bool:
    """Whether this finding waits on the quarantine clock, not on a person.

    The knowledge forbids mixing the human-only unlock footer onto these: a comment cannot waive a
    routine window. Detection is by `kind`, which is also the last segment of the finding key when
    there is no advisory — so a wake that only has the key can refuse the same way.
    """
    return finding.kind is Kind.QUARANTINE


def is_routine_quarantine_key(key: str) -> bool:
    """Key-shaped half of `is_routine_quarantine`, for a wake that has not re-loaded the finding."""
    return bool(key) and key.rsplit(":", 1)[-1] == "quarantine"


def refuse_unlock(key: str) -> str | None:
    """Why an unlock comment must not be granted, or `None` when it may.

    Routine quarantine is the clock. Granting a stamp would either break the window on the next
    prepare or leave a person thinking they were ignored. Refusal is the only honest answer.
    """
    if not is_routine_quarantine_key(key):
        return None
    return (
        "No. This finding is waiting for the quarantine window to clear. A comment cannot waive a "
        "routine quarantine wait — that is the clock, not a hold a person releases. When the "
        "window clears, a later run will act without needing this approval."
    )


def held(finding: Finding) -> str:
    """Why this finding may not ship without a person, or an empty string when it may.

    A security remediation is never held by the arithmetic. The library allows one to carry a major
    move precisely because waiting is the greater risk there, and a hold the agent invented would
    park an advisory fix behind a question nobody was asked to answer. A task may still declare one:
    that is somebody's judgement about this repository, and it is honoured — and on a security
    finding that declaration is the quarantine exception the knowledge requires when the only fixed
    version is still inside the window.
    """
    if finding.needs_unlock:
        if finding.klass is Klass.SECURITY:
            return (
                "the only fixed version is still inside the quarantine window, and adopting it "
                "needs a person's security exception — fixing the advisory outweighs waiting"
            )
        return "the check that found it reports that this needs a person's approval before it ships"
    if finding.klass is Klass.SECURITY:
        return ""
    if _major(finding):
        return (
            f"it moves {finding.subject.package or finding.subject.path} from "
            f"{finding.subject.version} to {finding.target}, which is a major move, and a major "
            "move ships only after a person approves it"
        )
    return ""


def _major(finding: Finding) -> bool:
    """Whether this is demonstrably an upgrade across a major line.

    Everything unclear answers no, and the declared half of the hold is what covers those. A
    comparison that cannot order the two strings says nothing about the size of the step, and
    treating "I could not parse this" as "it is a major" would hold half a lock file for approval.
    """
    subject = finding.subject
    if not (subject.ecosystem and subject.version and finding.target):
        return False
    comparison = compare_versions(subject.ecosystem, subject.version, finding.target)
    return comparison.order == -1 and comparison.step is Step.MAJOR


def waiting(finding: Finding, approvals: dict[str, Approval]) -> str:
    """The hold still in force on this finding, or an empty string once it is approved."""
    if finding.key in approvals:
        return ""
    return held(finding)
