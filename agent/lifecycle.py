"""Fix-branch lifecycle: tell the issue about an open PR, reclaim an abandoned one after close.

The stable branch name per subject means two runs can collide with the same tip. The playbook's
rules are exact: an open change request is left alone and pointed at from the issue; a closed one
may be recreated from the default branch after a note on the old request — never by force-pushing
over the old tip, and never by silently retargeting an open PR when a newer version appears.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from agent.errors import ConfigError
from agent.overlay import Overlay
from agent.reconcile import Posted
from agent.remediate import BRANCH_PREFIX, _unfixable, branch_for
from agent.repo import Repository
from agent.scm.port import Issue, Platform, Proposal, ScmError
from agent.unlock import Approval
from agent.verdict import Judged

OPEN_PR_MARK = "<!-- agent:open-pr={number} -->"
"""Idempotency: one notice per open pull request number on the issue, not one per weekly run."""

RECREATE_MARK = "<!-- agent:recreate={run} -->"


@dataclass(slots=True)
class Reclaimed:
    """What reclaim did in this run — for the manifest and for tests."""

    branches: list[str] = field(default_factory=list)
    noted: list[int] = field(default_factory=list)
    """Closed change-request numbers that received the recreate announcement."""
    failure: str = ""

    def as_json(self) -> dict[str, object]:
        return {
            "branches": list(self.branches),
            "noted": list(self.noted),
            "failure": self.failure,
        }


def reclaim_abandoned(
    platform: Platform,
    repository: Repository,
    *,
    judged: tuple[Judged, ...],
    open_heads: set[str],
    approvals: Mapping[str, Approval],
    overlay: Overlay,
    run: str,
) -> Reclaimed:
    """Delete abandoned agent branches so plan_fixes can prepare from the current default tip.

    Only for findings that would otherwise be fixable, and only when no open change request still
    carries that branch. A failure to talk to the platform is recorded and reclaim stops for that
    branch — better to defer than to push over unknown state.
    """
    record = Reclaimed()
    seen: set[str] = set()
    granted = dict(approvals)
    for item in judged:
        if _unfixable(item, granted, overlay.verification) is not None:
            continue
        branch = branch_for(item)
        if not branch.startswith(BRANCH_PREFIX) or branch in open_heads or branch in seen:
            continue
        seen.add(branch)
        try:
            remote = platform.has_remote_branch(branch)
        except ScmError as error:
            record.failure = str(error)
            continue
        local = repository.has_branch(branch)
        if not remote and not local:
            continue
        try:
            closed = platform.closed_on(branch)
            if closed is not None:
                platform.note_change(closed.number, _recreate_note(run))
                record.noted.append(closed.number)
            if remote:
                platform.delete_branch(branch)
            if local:
                repository.delete_branch(branch)
        except (ScmError, ConfigError) as error:
            record.failure = str(error)
            continue
        record.branches.append(branch)
    return record


def notice_open_prs(
    platform: Platform,
    *,
    awaiting: tuple[tuple[str, Proposal], ...],
    numbers: Mapping[str, int],
    judged: Mapping[str, Judged],
) -> list[Posted]:
    """Point each issue at the open pull request that already carries its fix.

    Does not touch that pull request: if the finding's target has moved on, the comment says so and
    leaves the PR for a person. Idempotent per open PR number on that issue.
    """
    posted: list[Posted] = []
    for key, proposal in awaiting:
        number = numbers.get(key)
        if number is None:
            continue
        mark = OPEN_PR_MARK.format(number=proposal.number)
        try:
            existing = platform.issue_comment_bodies(number)
        except ScmError as error:
            posted.append(Posted("open-pr-notice-failed", key, str(error)))
            continue
        if any(mark in body for body in existing):
            posted.append(Posted("open-pr-noted", key, proposal.reference or str(proposal.number)))
            continue
        item = judged.get(key)
        body = _open_pr_note(proposal, target=item.finding.target if item else "")
        try:
            platform.note(
                Issue(number=number, key=key, title="", body="", reference=""),
                body,
            )
        except ScmError as error:
            posted.append(Posted("open-pr-notice-failed", key, str(error)))
            continue
        posted.append(Posted("open-pr-notice", key, proposal.reference or str(proposal.number)))
    return posted


def _open_pr_note(proposal: Proposal, *, target: str) -> str:
    link = proposal.reference or f"#{proposal.number}"
    lines = [
        f"An open pull request already carries a fix for this finding: {link}",
        "",
    ]
    if target:
        lines += [
            f"The finding currently moves to `{target}`. This run does **not** update that pull "
            "request to chase a newer target — merge it, close it, or say what you want next.",
            "",
        ]
    else:
        lines += [
            "This run does not prepare a second branch, and does not rewrite that pull request. "
            "Merge it, close it, or say what you want next.",
            "",
        ]
    lines.append(OPEN_PR_MARK.format(number=proposal.number))
    return "\n".join(lines)


def _recreate_note(run: str) -> str:
    return "\n".join(
        [
            "This finding is still open. A later run will prepare a **new** change from the "
            "current default branch; this pull request stays closed and its branch tip will be "
            "removed so the next attempt is not stacked on the old commits.",
            "",
            RECREATE_MARK.format(run=run),
        ]
    )
