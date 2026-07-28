"""Human-visible notes when a fix was attempted and did not ship."""

from __future__ import annotations

from agent.answer import failure_on_issue
from agent.domain import FixOutcome, PlannedTask, Role
from agent.evidence import Reliability, Subject
from agent.findings import Action, Finding, Klass, Location, Severity
from agent.orchestrator import _note_unshipped
from agent.remediate import Fix, FixJob
from agent.scm.fake import FakePlatform
from agent.scm.port import Issue
from agent.verdict import Judged


def _judged(package: str = "buffa") -> Judged:
    finding = Finding(
        capability="capabilities/deps-outdated",
        klass=Klass.ROUTINE,
        severity=Severity.LOW,
        subject=Subject(ecosystem="ecosystems/cargo", package=package, version="0.8.1"),
        summary=f"{package} is outdated",
        rationale="cleared newer release exists.",
        remediation="Bump it.",
        location=Location(path="Cargo.toml", line=12),
    )
    return Judged(
        finding=finding, action=Action.COMMENT, reliability=Reliability.REPRODUCIBLE, capped=False
    )


def _fix(judged: Judged, *, outcome: FixOutcome, detail: str = "") -> Fix:
    return Fix(
        job=FixJob(
            task=PlannedTask(
                id="fix",
                capability="capabilities/deps-outdated",
                role=Role.WRITER,
                required=False,
            ),
            judged=judged,
        ),
        outcome=outcome,
        detail=detail,
    )


def test_failure_on_issue_states_the_reason() -> None:
    text = failure_on_issue(
        key="capabilities/deps-outdated:ecosystems/cargo:buffa:outdated",
        detail="connectrpc requires buffa ^0.8.1",
        run="run-1",
    )
    assert "could not ship a fix" in text
    assert "connectrpc requires buffa ^0.8.1" in text
    assert "run-1" in text


def test_note_unshipped_comments_on_refused_fix() -> None:
    platform = FakePlatform()
    judged = _judged()
    key = judged.finding.key
    platform.tracked.append(Issue(number=13, key=key, title="agent: buffa", body="finding"))

    posted = _note_unshipped(
        platform,
        fixes=(_fix(judged, outcome=FixOutcome.REFUSED, detail="Cannot ship: connectrpc"),),
        proposals={},
        numbers={key: 13},
        run="run-test",
    )
    assert [item.what for item in posted] == ["noted-failure"]
    assert platform.notes
    assert "Cannot ship: connectrpc" in platform.notes[0][1]


def test_note_unshipped_silent_when_proposed() -> None:
    platform = FakePlatform()
    judged = _judged()
    key = judged.finding.key
    posted = _note_unshipped(
        platform,
        fixes=(_fix(judged, outcome=FixOutcome.FIXED),),
        proposals={key: ("proposed", "https://example/pr/1")},
        numbers={key: 1},
        run="run-test",
    )
    assert posted == []
    assert not platform.notes
