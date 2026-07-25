"""Findings tracked as issues: one per finding, updated not duplicated, closed with proof."""

from __future__ import annotations

import pytest
from agent.domain import Outcome, RunResult
from agent.evidence import Reliability, Subject
from agent.findings import Action, Finding, Klass, Location, Severity
from agent.issues import LABEL, Tracking, track_findings
from agent.scm.fake import FakePlatform
from agent.scm.marker import read
from agent.scm.port import Issue
from agent.verdict import Judged, TaskOutcome, Verdict

HEAD = "0f1e2d3c4b5a69788796a5b4c3d2e1f009182736"
CAPABILITY = "capabilities/deps-vuln"
CLEAN = TaskOutcome(
    id="deps-vuln@python-uv", capability=CAPABILITY, required=True, outcome=Outcome.CLEAN
)
UNVERIFIED = TaskOutcome(
    id="deps-vuln@python-uv", capability=CAPABILITY, required=True, outcome=Outcome.UNVERIFIED
)


def finding(*, advisory: str = "PYSEC-2026-1", version: str = "3.1.3") -> Finding:
    return Finding(
        capability=CAPABILITY,
        klass=Klass.SECURITY,
        severity=Severity.HIGH,
        subject=Subject(ecosystem="ecosystems/python-uv", package="jinja2", version=version),
        summary=f"jinja2 {version} is affected by {advisory}",
        rationale="pip-audit reports it against the resolved pin.",
        remediation="Bump jinja2 to 3.1.6.",
        advisory=advisory,
        location=Location(path="pyproject.toml", line=12),
    )


def judged(item: Finding, *, capped: bool = False) -> Judged:
    return Judged(
        finding=item, action=Action.BLOCK, reliability=Reliability.REPRODUCIBLE, capped=capped
    )


def verdict_of(*items: Judged) -> Verdict:
    return Verdict(result=RunResult.BLOCKED, judged=items, blocking=items)


def track(
    platform: FakePlatform,
    verdict: Verdict,
    *,
    outcomes: tuple[TaskOutcome, ...] = (CLEAN,),
    limit: int = 10,
) -> Tracking:
    return track_findings(
        platform, verdict=verdict, outcomes=outcomes, head=HEAD, limit=limit, label=LABEL
    )


def what(record: Tracking) -> list[str]:
    return [item.what for item in record.posted]


@pytest.fixture
def platform() -> FakePlatform:
    return FakePlatform()


def test_a_new_finding_becomes_one_labelled_issue_carrying_its_key(platform: FakePlatform) -> None:
    record = track(platform, verdict_of(judged(finding())))

    assert what(record) == ["raised"]
    assert record.raised == 1
    tracked = platform.tracked[0]
    assert read(tracked.body) == finding().key
    assert platform.labels[tracked.number] == (LABEL,)
    assert "jinja2" in tracked.title


def test_the_same_finding_next_week_is_not_a_second_issue(platform: FakePlatform) -> None:
    """The whole promise of tracking by key: a weekly run on an unfixed problem writes nothing."""
    track(platform, verdict_of(judged(finding())))

    again = track(platform, verdict_of(judged(finding())))

    assert what(again) == ["unchanged"]
    assert len(platform.tracked) == 1
    assert not platform.notes


def test_a_finding_that_changed_updates_the_issue_it_already_has(platform: FakePlatform) -> None:
    track(platform, verdict_of(judged(finding())))

    moved = track(platform, verdict_of(judged(finding(version="3.1.4"))))

    assert what(moved) == ["updated"]
    assert len(platform.tracked) == 1
    assert "3.1.4" in platform.tracked[0].body


def test_a_title_leaves_out_what_drifts_so_a_saved_search_keeps_matching(
    platform: FakePlatform,
) -> None:
    track(platform, verdict_of(judged(finding())))
    first = platform.tracked[0].title

    track(platform, verdict_of(judged(finding(version="3.1.4"))))

    assert platform.tracked[0].title == first
    assert "3.1.3" not in first
    assert "PYSEC" not in first


def test_a_finding_that_is_gone_is_closed_with_the_evidence_that_settles_it(
    platform: FakePlatform,
) -> None:
    track(platform, verdict_of(judged(finding())))

    cleared = track(platform, verdict_of())

    assert what(cleared) == ["closed"]
    assert cleared.closed == 1
    assert not platform.tracked
    key, note = platform.notes[0]
    assert key == finding().key
    assert CAPABILITY in note
    assert HEAD[:12] in note


def test_a_check_that_did_not_finish_leaves_the_issue_exactly_as_it_was(
    platform: FakePlatform,
) -> None:
    """Silence is the correct output here. "Still present" would be as unfounded as a closure, and a
    weekly note that nothing is known is what teaches a team to mute the agent."""
    track(platform, verdict_of(judged(finding())))

    unproved = track(platform, verdict_of(), outcomes=(UNVERIFIED,))

    assert what(unproved) == ["kept-open"]
    assert "rather than clean" in unproved.posted[0].detail
    assert len(platform.tracked) == 1
    assert not platform.notes


def test_a_capability_that_never_ran_cannot_close_anything(platform: FakePlatform) -> None:
    track(platform, verdict_of(judged(finding())))

    narrowed = track(platform, verdict_of(), outcomes=())

    assert what(narrowed) == ["kept-open"]
    assert "did not run" in narrowed.posted[0].detail
    assert len(platform.tracked) == 1


def test_a_run_stays_within_the_new_issues_it_is_allowed(platform: FakePlatform) -> None:
    """Left for the next run rather than dropped or squeezed into one issue: a finding without its
    own key is a finding nobody can reconcile later."""
    findings = verdict_of(
        judged(finding(advisory="PYSEC-2026-1")),
        judged(finding(advisory="PYSEC-2026-2")),
        judged(finding(advisory="PYSEC-2026-3")),
    )

    record = track(platform, findings, limit=2)

    assert what(record) == ["raised", "raised", "deferred"]
    assert record.raised == 2
    assert "limit of 2" in record.posted[-1].detail


def test_an_issue_nobody_marked_is_not_the_agent_s_to_touch(platform: FakePlatform) -> None:
    """A label is not authorship: anyone can apply one, and closing a human's issue is unrecoverable
    in the only sense that matters — they stop trusting the thing that did it."""
    platform.tracked.append(
        Issue(
            number=99,
            key="",
            title="Please look at the login flow",
            body="No marker here, so this belongs to whoever wrote it.",
        )
    )
    platform.labels[99] = (LABEL,)

    record = track(platform, verdict_of())

    assert not record.posted
    assert len(platform.tracked) == 1


def test_a_platform_failure_costs_the_issues_and_not_the_run(platform: FakePlatform) -> None:
    platform.fail = "the token cannot see this repository"

    record = track(platform, verdict_of(judged(finding())))

    assert "token cannot see" in record.failure
    assert not record.posted
