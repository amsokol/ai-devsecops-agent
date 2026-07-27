"""Fix-branch lifecycle: open-PR notice on the issue, reclaim after a closed PR."""

from __future__ import annotations

from pathlib import Path

from agent.evidence import Reliability, Subject
from agent.findings import Action, Finding, Klass, Location, Severity
from agent.library import Library
from agent.lifecycle import OPEN_PR_MARK, notice_open_prs, reclaim_abandoned
from agent.overlay import Overlay
from agent.remediate import branch_for, plan_fixes
from agent.repo import Repository
from agent.scm.fake import FakePlatform
from agent.scm.port import Issue, Proposal
from agent.verdict import Judged


def _judged(*, package: str = "requests", target: str = "2.32.4") -> Judged:
    finding = Finding(
        capability="capabilities/deps-vuln",
        klass=Klass.SECURITY,
        severity=Severity.HIGH,
        subject=Subject(ecosystem="ecosystems/python-uv", package=package, version="2.31.0"),
        summary=f"{package} is affected.",
        rationale="Advisory covers the pin.",
        evidence=("advisories|ecosystems/python-uv|requests|2.31.0|",),
        remediation=f"Move the pin to {target}.",
        location=Location(path="pyproject.toml", line=12),
        advisory=f"PYSEC-{package}",
        target=target,
    )
    return Judged(
        finding=finding,
        action=Action.BLOCK,
        reliability=Reliability.REPRODUCIBLE,
        capped=False,
    )


def test_open_proposal_defers_and_lists_awaiting_review(
    library: Library, overlay: Overlay, git_repo: Path
) -> None:
    item = _judged()
    branch = branch_for(item)
    proposal = Proposal(number=42, head=branch, reference="fake://change/42")
    queue = plan_fixes(
        (item,),
        library=library,
        overlay=overlay,
        playbook="playbooks/maintain",
        repository=Repository.open(git_repo),
        max_open_fix_requests=5,
        open_proposals={branch: proposal},
    )
    assert queue.jobs == ()
    assert "already under review" in dict(queue.deferred)[item.finding.key]
    assert queue.awaiting_review == ((item.finding.key, proposal),)


def test_open_pr_notice_names_the_link_and_current_target_without_touching_the_pr(
    library: Library, overlay: Overlay, git_repo: Path
) -> None:
    del library, overlay, git_repo
    item = _judged(target="2.33.0")
    branch = branch_for(item)
    proposal = Proposal(number=42, head=branch, reference="https://example.test/pull/42")
    platform = FakePlatform(
        tracked=[
            Issue(
                number=7,
                key=item.finding.key,
                title="agent: deps-vuln — requests",
                body="body",
                reference="fake://issue/7",
            )
        ],
        labels={7: ("agent",)},
    )
    posted = notice_open_prs(
        platform,
        awaiting=((item.finding.key, proposal),),
        numbers={item.finding.key: 7},
        judged={item.finding.key: item},
    )
    assert [item.what for item in posted] == ["open-pr-notice"]
    assert len(platform.notes) == 1
    _, body = platform.notes[0]
    assert "https://example.test/pull/42" in body
    assert "`2.33.0`" in body
    assert "does **not** update" in body
    assert OPEN_PR_MARK.format(number=42) in body
    assert platform.pushed == []
    assert platform.proposed == []

    again = notice_open_prs(
        platform,
        awaiting=((item.finding.key, proposal),),
        numbers={item.finding.key: 7},
        judged={item.finding.key: item},
    )
    assert [item.what for item in again] == ["open-pr-noted"]
    assert len(platform.notes) == 1


def test_abandoned_branch_is_reclaimed_then_planned(
    library: Library, overlay: Overlay, git_repo: Path
) -> None:
    item = _judged()
    branch = branch_for(item)
    repository = Repository.open(git_repo)
    subprocess_run = __import__("subprocess").run
    subprocess_run(
        ["git", "branch", branch],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )
    assert repository.has_branch(branch)

    closed = Proposal(number=17, head=branch, reference="fake://change/17")
    platform = FakePlatform(
        closed_proposals=[closed],
        remote_branches={branch},
    )
    reclaimed = reclaim_abandoned(
        platform,
        repository,
        judged=(item,),
        open_heads=set(),
        approvals={},
        overlay=overlay,
        run="run-1",
    )
    assert reclaimed.branches == [branch]
    assert reclaimed.noted == [17]
    assert not repository.has_branch(branch)
    assert branch not in platform.remote_branches
    assert platform.change_notes and "new" in platform.change_notes[0][1].lower()

    queue = plan_fixes(
        (item,),
        library=library,
        overlay=overlay,
        playbook="playbooks/maintain",
        repository=repository,
        max_open_fix_requests=5,
    )
    assert [job.branch for job in queue.jobs] == [branch]


def test_open_pr_is_never_reclaimed(
    library: Library, overlay: Overlay, git_repo: Path
) -> None:
    item = _judged()
    branch = branch_for(item)
    repository = Repository.open(git_repo)
    __import__("subprocess").run(
        ["git", "branch", branch],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )
    platform = FakePlatform(remote_branches={branch})
    reclaimed = reclaim_abandoned(
        platform,
        repository,
        judged=(item,),
        open_heads={branch},
        approvals={},
        overlay=overlay,
        run="run-1",
    )
    assert reclaimed.branches == []
    assert repository.has_branch(branch)
    assert branch in platform.remote_branches
