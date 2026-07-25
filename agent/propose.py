"""Sending the fix branches out, and writing the change request that carries each one.

Everything a reviewer needs is derived from the record rather than from what the fix task said about
itself. The task's own words are quoted, clearly as prose; what the change is, which findings it
answers, and what was actually run to prove it are read from the ledger the agent kept.

Two rules keep this honest.

*Nothing is proposed that was not pushed.* The order matters more than it looks: a change request
for a branch that never arrived is a broken link somebody has to investigate, and a branch that
fails to push is usually one an earlier run left behind — a reason to stop, not to force.

*A change request links the issues it answers without closing them.* No closing keyword, on purpose.
A merge would close the issue on the platform's word alone, while the rule for closing is the one
every other part of the run obeys: the check that owns the finding looked again and found nothing.
The next maintenance run does exactly that, and closes with the evidence.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent.domain import FixOutcome
from agent.reconcile import Posted
from agent.remediate import Fix
from agent.scm import marker
from agent.scm.port import NewChange, Platform, ScmError


@dataclass(slots=True)
class Proposed:
    """What was sent out, what was not, and why."""

    posted: list[Posted] = field(default_factory=list)
    opened: int = 0
    failure: str = ""

    def as_json(self) -> dict[str, Any]:
        return {
            "posted": [item.as_json() for item in self.posted],
            "opened": self.opened,
            "failure": self.failure,
        }


def propose_fixes(
    platform: Platform,
    *,
    fixes: tuple[Fix, ...],
    path: Path,
    base: str,
    issues: Mapping[str, int],
    run: str,
) -> Proposed:
    """Push each shipped branch and open its change request, one failure at a time.

    A branch that cannot be pushed or proposed is recorded and the next one is attempted: three
    verified fixes should not be lost because the first of them collided with a leftover branch.
    """
    record = Proposed()
    for fix in fixes:
        if fix.outcome is not FixOutcome.FIXED or not fix.branch:
            continue
        try:
            platform.push(path, fix.branch)
        except ScmError as error:
            record.posted.append(Posted("not-pushed", fix.job.key, str(error)))
            continue
        try:
            opened = platform.propose(
                NewChange(
                    head=fix.branch,
                    base=base,
                    title=_title(fix),
                    body=_body(fix, issues=issues, run=run),
                )
            )
        except ScmError as error:
            record.posted.append(Posted("not-proposed", fix.job.key, str(error)))
            continue
        record.opened += 1
        record.posted.append(Posted("proposed", fix.job.key, opened.reference))
    return record


def _title(fix: Fix) -> str:
    """Built from the class and the subject, so the same subject reads the same way every time.

    Not from the fix task's notes: a title assembled from prose changes with the wording, and a
    reviewer who saw last week's change request would not recognise this one as its successor.
    """
    finding = fix.job.judged.finding
    subject = finding.subject
    what = subject.package or subject.path or finding.capability.rsplit("/", 1)[-1]
    count = len(fix.job.group)
    tail = f" ({count} findings)" if count > 1 else ""
    return f"agent: {finding.klass.value} fix for {what}{tail}"


def _body(fix: Fix, *, issues: Mapping[str, int], run: str) -> str:
    """What a reviewer needs before reading the diff, in the order they need it.

    Verification is the part that earns the change request, so it is stated as the record has it:
    the surfaces that ran in full, and any command that was already failing before this change. A
    fix called verified without saying what ran is one a reviewer must establish from scratch.
    """
    finding = fix.job.judged.finding
    lines = [
        f"**{finding.severity.value}** `{finding.klass.value}` — {finding.summary}",
        "",
        f"**Remediation.** {finding.remediation}",
    ]
    if fix.notes:
        lines += ["", "**What the fix task reports.**", "", f"> {fix.notes}"]
    lines += ["", "**Findings this change answers.**", ""]
    for item in fix.job.group:
        number = issues.get(item.finding.key)
        where = f" — remediates #{number}" if number else ""
        lines.append(f"- `{item.finding.key}`{where}")
    if fix.verification is not None:
        surfaces = ", ".join(f"`{name}`" for name in fix.verification.verified)
        lines += ["", f"**Verified.** {surfaces or 'nothing was recorded as run'}"]
        if fix.verification.pre_existing:
            already = ", ".join(
                f"`{' '.join(command)}`" for command in fix.verification.pre_existing
            )
            lines.append(
                f"These were already failing on the base commit, before this change: {already}. "
                "They are the product's own to fix; this change neither works around them nor "
                "touches them."
            )
    if fix.changed:
        lines += ["", "**Files.** " + ", ".join(f"`{name}`" for name in fix.changed)]
    lines += [
        "",
        f"Prepared by the DevSecOps agent in run `{run}`. Merging is a human's decision: the agent "
        "has no merge authority and does not ask for one.",
    ]
    return marker.stamp("\n".join(lines), fix.job.key)
