"""Findings that wait for a person, and what a person's approval releases.

The property under test is a refusal, which makes it easy to get wrong in the invisible direction: a
bug here raises nothing, it ships a breaking change nobody was asked about. So the holds are tested
from both ends — nothing moves without an approval, and everything moves once there is one — and so
is the thing between them, which is that the question is asked exactly once.

The classifier is scripted. What a model would make of "approved, do it" is not what these prove;
what the agent does once something has read it that way is.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from agent.absence import Absences
from agent.backends.fake import FakeBackend, Scripted
from agent.backends.port import Brief
from agent.domain import Outcome, Reason, Role, RunResult, Trigger
from agent.evidence import Reliability, Subject
from agent.findings import Action, Finding, Kind, Klass, Location, Severity
from agent.issues import track_findings
from agent.library import Library
from agent.orchestrator import Request, run
from agent.overlay import Overlay
from agent.remediate import plan_fixes
from agent.repo import Repository
from agent.scm import marker
from agent.scm.fake import FakePlatform
from agent.scm.port import Comment, Issue
from agent.unlock import Approval, granted, held, read, stamped, waiting
from agent.verdict import Judged, TaskOutcome, Verdict
from agent.wake import Wake

WHEN = datetime(2026, 7, 25, 9, 0, tzinfo=UTC)
PERSON = "amsokol"
COMMENT = 11
SUMMARY = "jinja2 is a major line behind."
KEY = "capabilities/deps-outdated:ecosystems/python-uv:jinja2:outdated"
VERIFY = ("uv", "--version")
APPROVAL = Approval(by=PERSON, comment=COMMENT, at="2026-07-25")


def a_finding(
    *,
    klass: Klass = Klass.ROUTINE,
    package: str = "jinja2",
    version: str = "2.11.3",
    target: str = "3.1.4",
    needs_unlock: bool = False,
    remediation: str = "Move the pin to 3.1.4.",
) -> Finding:
    return Finding(
        capability="capabilities/deps-outdated",
        klass=klass,
        severity=Severity.MEDIUM,
        subject=Subject(ecosystem="ecosystems/python-uv", package=package, version=version),
        summary=SUMMARY,
        rationale="The pinned line stopped receiving fixes.",
        evidence=("latest-version|ecosystems/python-uv|jinja2||",),
        remediation=remediation,
        location=Location(path="pyproject.toml", line=2),
        target=target,
        needs_unlock=needs_unlock,
        kind=Kind.OUTDATED,
    )


def branches(platform: FakePlatform) -> list[str]:
    """What the run pushed as work, leaving out the ref it keeps its own memory in.

    A maintenance run counts how long each tracked finding has gone unreported, and that count is a
    push like any other. It is not a proposal, and a test about whether a change was shipped has no
    business seeing it.
    """
    return [ref for ref in platform.pushed if not ref.startswith("refs/")]


def counting(outcomes: tuple[TaskOutcome, ...]) -> Absences:
    """A first run's worth of absence counting: nothing remembered, nothing yet closable."""
    return Absences.of({}, outcomes=outcomes, run="run-1", when=WHEN)


def a_judged(finding: Finding | None = None) -> Judged:
    return Judged(
        finding=finding or a_finding(),
        action=Action.COMMENT,
        reliability=Reliability.REPRODUCIBLE,
        capped=False,
    )


def test_a_major_move_is_held_whether_or_not_anybody_declared_it() -> None:
    """The half the agent can prove. A task that forgets the flag must not switch policy off: the
    failure would be silent and would arrive as a shipped change request nobody asked for."""
    reason = held(a_finding(target="3.1.4"))
    assert "major move" in reason
    assert "2.11.3 to 3.1.4" in reason

    assert not held(a_finding(version="3.1.2", target="3.1.4"))
    assert not held(a_finding(version="3.1.2", target="3.2.0"))
    # Nothing to measure is not a reason to hold: half a lock file would be waiting for somebody.
    assert not held(a_finding(target=""))
    assert not held(a_finding(version="from-git", target="also-not-a-version"))


def test_a_declared_hold_covers_the_majors_no_comparison_can_see() -> None:
    """`@v5` to `@v7` in a workflow pin is a major by policy and a patch bump as strings. That
    judgement is the task's, and the agent's arithmetic never overrules it in either direction."""
    pin = Finding(
        capability="capabilities/deps-outdated",
        klass=Klass.ROUTINE,
        severity=Severity.LOW,
        subject=Subject(ecosystem="ecosystems/github-actions", path=".github/workflows/ci.yml"),
        summary="actions/checkout is pinned to v5 and v7 is current.",
        rationale="The action's major line moved twice.",
        remediation="Move the pin to v7.",
        needs_unlock=True,
    )
    assert "reports that this needs a person's approval" in held(pin)


def test_a_security_remediation_is_not_parked_behind_a_question_nobody_asked() -> None:
    """Waiting is the greater risk on an advisory, and the knowledge says so. A hold the agent
    invented would keep a fix off the branch while the advisory stays exploitable."""
    assert not held(a_finding(klass=Klass.SECURITY, target="3.1.4"))
    # Declared, though, it stands: on security that declaration is the quarantine exception.
    reason = held(a_finding(klass=Klass.SECURITY, needs_unlock=True))
    assert "quarantine window" in reason
    assert "security exception" in reason


def test_routine_quarantine_refuses_an_unlock_comment() -> None:
    from agent.unlock import refuse_unlock

    key = "capabilities/deps-outdated:ecosystems/github-actions:actions/checkout:quarantine"
    assert "cannot waive" in (refuse_unlock(key) or "")
    assert refuse_unlock(KEY) is None


def test_a_quarantine_issue_waits_on_the_clock_not_a_person() -> None:
    """Empty verification surfaces used to paste the human-only footer onto quarantine pins."""
    platform = FakePlatform()
    pin = Finding(
        capability="capabilities/deps-outdated",
        klass=Klass.ROUTINE,
        severity=Severity.MEDIUM,
        subject=Subject(
            ecosystem="ecosystems/github-actions",
            package="actions/checkout",
            version="v7.0.1",
        ),
        summary="actions/checkout@v7.0.1 is still inside quarantine.",
        rationale="The pin on main has not cleared the product window.",
        remediation="Wait until the quarantine window clears.",
        kind=Kind.QUARANTINE,
        forbidden_state=True,
    )
    track_findings(
        platform,
        verdict=a_verdict(a_judged(pin)),
        absences=counting((reported(),)),
        head="abc123",
        limit=5,
        surfaces={},
    )
    body = platform.tracked[0].body
    assert "Waiting for quarantine" in body
    assert "Waiting for a person" not in body
    assert "ask for a pull request" not in body.lower()


def test_a_security_quarantine_exception_offers_unlock() -> None:
    platform = FakePlatform()
    pin = Finding(
        capability="capabilities/deps-vuln",
        klass=Klass.SECURITY,
        severity=Severity.HIGH,
        subject=Subject(ecosystem="ecosystems/python-uv", package="jinja2", version="2.11.3"),
        summary="jinja2 is vulnerable; the only fix is still in quarantine.",
        rationale="Advisory requires 3.1.6, which has not cleared N.",
        remediation="Move to 3.1.6 as a security exception.",
        evidence=("advisory|PYSEC-2026-1|",),
        advisory="PYSEC-2026-1",
        target="3.1.6",
        needs_unlock=True,
        kind=Kind.VULNERABLE,
    )
    track_findings(
        platform,
        verdict=a_verdict(a_judged(pin)),
        absences=counting((reported(),)),
        head="abc123",
        limit=5,
        surfaces={},
    )
    body = platform.tracked[0].body
    assert "Waiting for a person" in body
    assert "outweighs quarantine" in body
    assert "unlock a pull request" in body


def test_an_approval_does_not_queue_a_routine_quarantine_fix(
    library: Library, overlay: Overlay, overlay_root: Path, git_repo: Path
) -> None:
    from tests.test_fix import without_verification

    pin = Finding(
        capability="capabilities/deps-outdated",
        klass=Klass.ROUTINE,
        severity=Severity.MEDIUM,
        subject=Subject(
            ecosystem="ecosystems/github-actions",
            package="actions/checkout",
            version="v7.0.1",
        ),
        summary="actions/checkout@v7.0.1 is still inside quarantine.",
        rationale="The pin on main has not cleared the product window.",
        remediation="Wait until the quarantine window clears.",
        target="v7.0.1",
        kind=Kind.QUARANTINE,
    )
    item = a_judged(pin)
    bare = without_verification(overlay_root, library, overlay)
    queue = plan_fixes(
        (item,),
        library=library,
        overlay=bare,
        playbook="playbooks/maintain",
        repository=Repository.open(git_repo),
        max_open_fix_requests=5,
        approvals={item.finding.key: APPROVAL},
    )
    assert not queue.jobs
    assert "quarantine window" in dict(queue.deferred)[item.finding.key]


def test_an_approval_is_recorded_once_and_read_back_exactly() -> None:
    """The stamp is the whole memory of a grant, so it has to survive being rewritten by later runs
    and must never accumulate: two stamps on one issue is two answers to one question."""
    body = marker.stamp("jinja2 is behind.", KEY)
    once = stamped(body, APPROVAL)
    assert read(once) == APPROVAL
    assert f"Approved by @{PERSON} on 2026-07-25" in once

    later = stamped(once, Approval(by="someone-else", comment=12, at="2026-08-01"))
    assert later.count("agent:unlocked") == 1
    assert read(later) == Approval(by="someone-else", comment=12, at="2026-08-01")

    assert read(body) is None
    issue = Issue(number=7, key=KEY, title="agent: deps-outdated — jinja2", body=once)
    assert granted((issue,)) == {KEY: APPROVAL}


def test_a_held_finding_is_deferred_every_run_until_somebody_answers(
    library: Library, overlay: Overlay, git_repo: Path
) -> None:
    """The refusal itself, and the wording of it. "Waiting for a person" has to read differently
    from "this could not be fixed": one is the agent working as designed, the other is a problem."""
    item = a_judged()
    queue = plan_fixes(
        (item,),
        library=library,
        overlay=overlay,
        playbook="playbooks/maintain",
        repository=Repository.open(git_repo),
        max_open_fix_requests=5,
    )
    assert not queue.jobs
    assert "major move" in dict(queue.deferred)[KEY]

    released = plan_fixes(
        (item,),
        library=library,
        overlay=overlay,
        playbook="playbooks/maintain",
        repository=Repository.open(git_repo),
        max_open_fix_requests=5,
        approvals={KEY: APPROVAL},
    )
    assert [job.key for job in released.jobs] == [KEY]
    assert not released.deferred
    assert not waiting(item.finding, {KEY: APPROVAL})


def reported(outcome: Outcome = Outcome.FINDINGS, reason: Reason | None = None) -> TaskOutcome:
    return TaskOutcome(
        id="deps-outdated@python-uv",
        capability="capabilities/deps-outdated",
        required=True,
        outcome=outcome,
        reason=reason,
    )


def a_verdict(item: Judged | None = None) -> Verdict:
    return Verdict(result=RunResult.PASS, judged=(item or a_judged(),))


def test_the_issue_asks_for_the_one_thing_a_person_can_do_about_it() -> None:
    """An issue is read weeks later by somebody who never saw the run. If it does not say that it is
    waiting, and what saying yes will cause, the hold looks like the agent giving up quietly."""
    platform = FakePlatform()
    track_findings(
        platform, verdict=a_verdict(), absences=counting((reported(),)), head="abc123", limit=5
    )

    body = platform.tracked[0].body
    assert "Waiting for a person" in body
    assert "major move" in body
    assert "Comment here to approve it" in body
    assert "Moves to: 3.1.4" in body
    assert read(body) is None


def test_an_approved_issue_states_who_said_so_and_stops_asking() -> None:
    """The question is asked once. A run that regenerated the body without the grant would ask again
    every week, which the knowledge names as a defect in its own right."""
    platform = FakePlatform()
    track_findings(
        platform,
        verdict=a_verdict(),
        absences=counting((reported(),)),
        head="abc123",
        limit=5,
        approvals={KEY: APPROVAL},
    )
    body = platform.tracked[0].body
    assert f"Approved by @{PERSON}" in body
    assert "Waiting for a person" not in body
    assert read(body) == APPROVAL

    # The next run, with the grant read back off that body, changes nothing at all.
    again = track_findings(
        platform,
        verdict=a_verdict(),
        absences=counting((reported(),)),
        head="abc123",
        limit=5,
        known=tuple(platform.tracked),
        approvals=granted(tuple(platform.tracked)),
    )
    assert [item.what for item in again.posted] == ["unchanged"]


def test_an_approval_is_written_down_even_when_the_check_did_not_finish() -> None:
    """The awkward run: somebody approves, and the analysis that would confirm the finding breaks.

    The grant still has to land on the issue. Otherwise it exists only in that run's record, and the
    next run finds no stamp and asks the person for permission they already gave.
    """
    issue = Issue(
        number=7,
        key=KEY,
        title="agent: deps-outdated — jinja2",
        body=marker.stamp("jinja2 is behind.", KEY),
        reference="fake://issue/7",
    )
    platform = FakePlatform(tracked=[issue], labels={7: ("agent",)})
    record = track_findings(
        platform,
        verdict=Verdict(result=RunResult.INCONCLUSIVE),
        absences=counting((reported(Outcome.UNVERIFIED, Reason.UNAVAILABLE),)),
        head="abc123",
        limit=5,
        approvals={KEY: APPROVAL},
    )

    assert read(platform.tracked[0].body) == APPROVAL
    assert [item.what for item in record.posted] == ["approved", "kept-open"]
    assert not platform.closed


def pinned(repo: Path) -> None:
    """A product pinned to the old major, committed, so a fix has something to change."""
    (repo / "pyproject.toml").write_text(
        '[project]\ndependencies = ["jinja2==2.11.3"]\n', encoding="utf-8"
    )
    (repo / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    environment = {
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "PATH": "/usr/bin:/bin",
        "HOME": str(repo.parent),
    }
    for arguments in (("add", "--all"), ("commit", "--quiet", "-m", "pin jinja2")):
        subprocess.run(["git", "-C", str(repo), *arguments], check=True, env=environment)


def verifiable(overlay_root: Path) -> Path:
    """The fixture overlay with a verification command that exists and passes."""
    path = overlay_root / "agent.yaml"
    values = yaml.safe_load(path.read_text(encoding="utf-8"))
    values["verification"] = {"python-uv": [list(VERIFY)]}
    path.write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")
    return overlay_root


def finder(intent: str = "unlock", *, fixes: bool = True) -> FakeBackend:
    """An analyst that reports the held finding from a fact it recorded, and a fixer that moves it.

    The result file is written by the session rather than scripted flat, because the evidence key
    only exists once the fact has been recorded — which is the rule the agent enforces on findings.
    """

    def act(brief: Brief) -> None:
        if brief.task.role is Role.FIXER and not fixes:
            return
        if brief.task.role is Role.ANALYST:
            call = brief.toolkit.call("run_command", {"command": list(VERIFY)})
            fact = brief.toolkit.call(
                "record_fact",
                {
                    "question": "latest-version",
                    "subject": {"ecosystem": "ecosystems/python-uv", "package": "jinja2"},
                    "value": "3.1.4",
                    "calls": [call["call"]],
                },
            )
            brief.result_path.parent.mkdir(parents=True, exist_ok=True)
            brief.result_path.write_text(json.dumps(_reported(fact["key"])), encoding="utf-8")
        elif brief.task.role is Role.FIXER:
            brief.toolkit.call(
                "edit_file",
                {"path": "pyproject.toml", "find": "2.11.3", "replace": "3.1.4"},
            )
            brief.toolkit.call("run_command", {"command": list(VERIFY)})

    return FakeBackend(
        answers={
            "wake-intent": Scripted(
                result={"intent": intent, "confident": True, "gist": "approves the major move"}
            ),
            "deps-outdated@python-uv": Scripted(result=None),
        },
        default=Scripted(
            result={"outcome": "fixed", "notes": "Moved the pin to 3.1.4."}
            if fixes
            else {"outcome": "refused", "notes": "3.1.4 needs a Python this product does not run."}
        ),
        on_execute=act,
    )


def _reported(evidence: str) -> dict[str, Any]:
    return {
        "outcome": "findings",
        "findings": [
            {
                "class": "routine",
                "severity": "medium",
                "subject": {"package": "jinja2", "version": "2.11.3"},
                "location": {"path": "pyproject.toml", "line": 2},
                "summary": SUMMARY,
                "rationale": "The pinned line stopped receiving fixes.",
                "evidence": [evidence],
                "remediation": "Move the pin to 3.1.4.",
                "target": "3.1.4",
                "kind": "outdated",
            }
        ],
    }


def waiting_issue() -> Issue:
    return Issue(
        number=7,
        key=KEY,
        title="agent: deps-outdated — jinja2",
        body=marker.stamp(f"{SUMMARY}\n\n**Waiting for a person.**", KEY),
        reference="fake://issue/7",
    )


def maintaining(
    repo: Path,
    library_root: Path,
    overlay_root: Path,
    config_dir: Path,
    tmp_path: Path,
    *,
    wake: Wake | None = None,
) -> Request:
    return Request(
        trigger=Trigger.COMMENT_ON_ISSUE if wake else Trigger.MAINTAIN_REQUESTED,
        repository=repo,
        library_path=library_root,
        overlay_path=overlay_root,
        run_dir=tmp_path / "runs",
        config_dir=config_dir,
        wake=wake,
        publish=True,
    )


def test_a_major_move_is_reported_and_the_code_is_left_alone(
    git_repo: Path, library_root: Path, overlay_root: Path, config_dir: Path, tmp_path: Path
) -> None:
    """A whole maintenance run over a product a major behind: an issue, and nothing else.

    No branch, no change request, no edit to the tree. The bump would have verified — the same
    session ships it in the test below — so what stops it is the hold and only the hold.
    """
    pinned(git_repo)
    platform = FakePlatform()
    record = run(
        maintaining(git_repo, library_root, verifiable(overlay_root), config_dir, tmp_path),
        platform=platform,
        backend=finder(),
    )

    assert not branches(platform) and not platform.proposed
    assert "2.11.3" in (git_repo / "pyproject.toml").read_text(encoding="utf-8")
    deferred = {item["finding"]: item["reason"] for item in record.manifest.remediation["deferred"]}
    assert "major move" in deferred[KEY]
    assert "Waiting for a person" in platform.tracked[0].body
    assert "Waiting for approval" in record.report


def test_an_approval_on_the_issue_ships_the_change_it_was_holding(
    git_repo: Path, library_root: Path, overlay_root: Path, config_dir: Path, tmp_path: Path
) -> None:
    """The second scenario this mechanism exists for, end to end.

    Somebody writes "approved, do it" on the issue the agent opened. The run that follows reads the
    comment, records the permission on the issue, re-establishes the finding, prepares the bump in
    an isolated worktree, verifies it with the product's own command, pushes the branch and opens a
    change request that names the issue. The person is told all of that where they asked.
    """
    pinned(git_repo)
    issue = waiting_issue()
    platform = FakePlatform(
        tracked=[issue],
        labels={7: ("agent",)},
        said={
            COMMENT: Comment(
                id=COMMENT,
                author=PERSON,
                bot=False,
                body="approved. do it.",
                reference="fake://comment/11",
            )
        },
        writers=(PERSON,),
    )
    record = run(
        maintaining(
            git_repo,
            library_root,
            verifiable(overlay_root),
            config_dir,
            tmp_path,
            wake=Wake(actor=PERSON, comment=COMMENT, issue=7),
        ),
        platform=platform,
        backend=finder(),
    )

    assert record.manifest.wake["intent"] == "unlock"
    assert record.manifest.wake["unlocked"]["by"] == PERSON
    assert [job["findings"] for job in record.manifest.remediation["jobs"]] == [[KEY]]
    assert [fix["outcome"] for fix in record.manifest.fixes] == ["fixed"]
    assert branches(platform) and branches(platform)[0].startswith("agent/routine/")
    assert platform.proposed
    proposal = dict(platform.bodies)[branches(platform)[0]]
    assert "remediates #7" in proposal
    # The permission is on the issue from now on, so next week's run does not ask again.
    assert read(platform.tracked[0].body) == Approval(
        by=PERSON, comment=COMMENT, at=record.manifest.started_at[:10]
    )
    status = next(body for key, body in platform.notes if "You asked for this" in body)
    assert f"Approved by @{PERSON}" in status
    assert "waiting for review" in status
    # The branch under maintenance never moved: the change is on the fix branch and nowhere else.
    assert "2.11.3" in (git_repo / "pyproject.toml").read_text(encoding="utf-8")


def test_an_approval_outlives_the_fix_that_failed_under_it(
    git_repo: Path, library_root: Path, overlay_root: Path, config_dir: Path, tmp_path: Path
) -> None:
    """Permission was given for the move, not for one attempt at it.

    A fix that will not verify is the agent's problem to retry, and asking the person to approve the
    same thing again every week would train them to approve without reading — which is the failure
    mode that makes an approval gate worth nothing.
    """
    pinned(git_repo)
    platform = FakePlatform(
        tracked=[waiting_issue()],
        labels={7: ("agent",)},
        said={
            COMMENT: Comment(
                id=COMMENT, author=PERSON, bot=False, body="approved, go ahead", parent=0
            )
        },
        writers=(PERSON,),
    )
    run(
        maintaining(
            git_repo,
            library_root,
            verifiable(overlay_root),
            config_dir,
            tmp_path,
            wake=Wake(actor=PERSON, comment=COMMENT, issue=7),
        ),
        platform=platform,
        backend=finder(fixes=False),
    )

    assert not branches(platform)
    assert read(platform.tracked[0].body) is not None
    status = next(body for key, body in platform.notes if "You asked for this" in body)
    assert "The approval stands" in status


def test_a_run_that_cannot_read_the_approvals_ships_nothing_that_waits(
    git_repo: Path, library_root: Path, overlay_root: Path, config_dir: Path, tmp_path: Path
) -> None:
    """Fail closed. An issue listing that errors is indistinguishable from "nobody approved
    anything", and the safe reading of the two is the same: report it again and change nothing."""
    pinned(git_repo)
    platform = FakePlatform(fail="the issues endpoint is having a day")
    record = run(
        maintaining(git_repo, library_root, verifiable(overlay_root), config_dir, tmp_path),
        platform=platform,
        backend=finder(),
    )

    assert not branches(platform)
    assert any("approved could not be read" in warning for warning in record.manifest.warnings)
