"""Findings tracked as issues: one per finding, updated not duplicated, closed with proof."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from agent.absence import Absences
from agent.coverage import Coverage
from agent.domain import Outcome, RunResult
from agent.evidence import Evidence, Origin, Reliability, Subject
from agent.findings import Action, Finding, Klass, Location, Severity
from agent.issues import LABEL, Tracking, track_findings
from agent.scm.fake import FakePlatform
from agent.scm.marker import read
from agent.scm.port import Issue
from agent.verdict import Judged, TaskOutcome, Verdict

HEAD = "0f1e2d3c4b5a69788796a5b4c3d2e1f009182736"
WHEN = datetime(2026, 7, 25, 9, 0, tzinfo=UTC)
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


def covering(*packages: str) -> Coverage:
    """A run that examined exactly these packages of the ecosystem the findings here belong to."""
    return Coverage.of(
        Evidence.verified(
            question="latest-version",
            subject=Subject(ecosystem="ecosystems/python-uv", package=package),
            value="3.1.6",
            origin=Origin.API,
            source="https://pypi.org/",
            observed_at=WHEN,
            recipe=f"{CAPABILITY}@fetch",
        )
        for package in packages
    )


def track(
    platform: FakePlatform,
    verdict: Verdict,
    *,
    outcomes: tuple[TaskOutcome, ...] = (CLEAN,),
    limit: int = 10,
    memory: dict[str, Any] | None = None,
    coverage: Coverage | None = None,
) -> Tracking:
    """One run of the reconciliation, carrying the streaks forward when a test passes a memory.

    A test that passes none is a first run, which is what most of them are about; closing needs two,
    so a test about a closure is a test that keeps the memory between its calls.
    """
    carried = memory if memory is not None else {}
    counted = Absences.of(carried, outcomes=outcomes, run="run-1", when=WHEN, coverage=coverage)
    record = track_findings(
        platform, verdict=verdict, absences=counted, head=HEAD, limit=limit, label=LABEL
    )
    if memory is not None:
        memory.update(counted.document(carried))
    return record


def until_closed(platform: FakePlatform, verdict: Verdict, **rest: Any) -> Tracking:
    """The run in which an absent finding is finally closed, having been absent once before."""
    memory: dict[str, Any] = {}
    track(platform, verdict, memory=memory, **rest)
    return track(platform, verdict, memory=memory, **rest)


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

    cleared = until_closed(platform, verdict_of())

    assert what(cleared) == ["closed"]
    assert cleared.closed == 1
    assert not platform.tracked
    key, note = platform.notes[0]
    assert key == finding().key
    assert CAPABILITY in note
    assert HEAD[:12] in note


def test_one_complete_run_without_a_finding_is_not_yet_a_closure(platform: FakePlatform) -> None:
    """A closure is a claim nobody revisits, and a task is asked to be exhaustive rather than
    proved to be. One run of it costs a week of visibility; being wrong costs the tracker."""
    memory: dict[str, Any] = {}
    track(platform, verdict_of(judged(finding())), memory=memory)

    once = track(platform, verdict_of(), memory=memory)

    assert what(once) == ["kept-open"]
    assert "next run" in once.posted[0].detail
    assert len(platform.tracked) == 1
    assert not platform.notes


def test_a_finding_that_comes_back_starts_its_absence_over(platform: FakePlatform) -> None:
    """Otherwise two absences months apart, with the problem reported in between, close an issue
    about a problem that is still there."""
    memory: dict[str, Any] = {}
    track(platform, verdict_of(judged(finding())), memory=memory)
    track(platform, verdict_of(), memory=memory)
    track(platform, verdict_of(judged(finding())), memory=memory)

    again = track(platform, verdict_of(), memory=memory)

    assert what(again) == ["kept-open"]
    assert len(platform.tracked) == 1


def test_a_run_that_could_not_look_does_not_spend_the_absence(platform: FakePlatform) -> None:
    """A narrowed or broken run leaves the count exactly as it was. Treating "did not look" as one
    of the two would close everything in a repository whose runs alternate between ecosystems."""
    memory: dict[str, Any] = {}
    track(platform, verdict_of(judged(finding())), memory=memory)
    track(platform, verdict_of(), memory=memory)
    track(platform, verdict_of(), outcomes=(), memory=memory)

    third = track(platform, verdict_of(), outcomes=(UNVERIFIED,), memory=memory)

    assert what(third) == ["kept-open"]
    assert len(platform.tracked) == 1
    assert what(track(platform, verdict_of(), memory=memory)) == ["closed"]


def test_an_issue_a_person_is_reading_settles_on_the_first_answer(
    platform: FakePlatform,
) -> None:
    """The wait is for the issues nobody is looking at. Somebody who wrote on this one is told what
    the recheck found on it, and "come back next week" is the wrong reply to that."""
    memory: dict[str, Any] = {}
    track(platform, verdict_of(judged(finding())), memory=memory)
    asked = Absences.of(
        memory, outcomes=(CLEAN,), run="run-2", when=WHEN, asked=frozenset({finding().key})
    )

    record = track_findings(
        platform, verdict=verdict_of(), absences=asked, head=HEAD, limit=10, label=LABEL
    )

    assert what(record) == ["closed"]
    assert not platform.tracked


def test_a_quarantine_issue_is_not_closed_on_first_unlock_miss(
    platform: FakePlatform,
) -> None:
    """Unlock wake puts the key in `asked`, but a single recheck that omitted quarantine must not
    close a forbidden-state issue — that closed demo2 #4 while checkout was still in window."""
    from agent.findings import Kind

    pin = Finding(
        capability="capabilities/deps-outdated",
        klass=Klass.ROUTINE,
        severity=Severity.MEDIUM,
        subject=Subject(
            ecosystem="ecosystems/github-actions",
            package="actions/checkout",
            version="v7",
        ),
        summary="checkout tip still in quarantine",
        rationale="release date inside window",
        remediation="Pin to v7.0.0",
        target="v7.0.0",
        kind=Kind.QUARANTINE,
    )
    memory: dict[str, Any] = {}
    track(platform, verdict_of(judged(pin)), memory=memory)
    asked = Absences.of(
        memory,
        outcomes=(
            TaskOutcome(
                id="deps-outdated@github-actions",
                capability="capabilities/deps-outdated",
                required=True,
                outcome=Outcome.CLEAN,
            ),
        ),
        run="run-2",
        when=WHEN,
        asked=frozenset({pin.key}),
        coverage=Coverage.of(
            (
                Evidence.verified(
                    question="declared-pin",
                    subject=Subject(
                        ecosystem="ecosystems/github-actions", package="actions/checkout"
                    ),
                    value="v7",
                    origin=Origin.TOOL,
                    source="list",
                    observed_at=WHEN,
                    recipe="capabilities/deps-outdated@list_action_pins",
                ),
            )
        ),
    )

    record = track_findings(
        platform, verdict=verdict_of(), absences=asked, head=HEAD, limit=10, label=LABEL
    )

    assert what(record) == ["kept-open"]
    assert len(platform.tracked) == 1


def test_a_sweep_that_did_not_reach_the_package_cannot_close_its_issue(
    platform: FakePlatform,
) -> None:
    """The live failure this guards: a check that completed, and got through part of the tree.

    Its report is a list of findings and looks no different from a thorough run's. What tells them
    apart is the evidence, which names what was actually examined.
    """
    memory: dict[str, Any] = {}
    track(platform, verdict_of(judged(finding())), memory=memory)

    short = track(platform, verdict_of(), memory=memory, coverage=covering("cryptography"))

    assert what(short) == ["kept-open"]
    assert "did not get to it this run" in short.posted[0].detail
    assert len(platform.tracked) == 1


def test_a_run_that_did_not_reach_a_package_does_not_spend_its_absence(
    platform: FakePlatform,
) -> None:
    """Same rule as a check that did not finish: only runs that looked are allowed to count."""
    memory: dict[str, Any] = {}
    track(platform, verdict_of(judged(finding())), memory=memory)
    track(platform, verdict_of(), memory=memory, coverage=covering("jinja2"))
    track(platform, verdict_of(), memory=memory, coverage=covering("cryptography"))

    third = track(platform, verdict_of(), memory=memory, coverage=covering("jinja2"))

    assert what(third) == ["closed"]


def test_a_check_that_did_not_finish_leaves_the_issue_exactly_as_it_was(
    platform: FakePlatform,
) -> None:
    """Silence is the correct output here. "Still present" would be as unfounded as a closure, and a
    weekly note that nothing is known is what teaches a team to mute the agent."""
    track(platform, verdict_of(judged(finding())))

    unproved = track(platform, verdict_of(), outcomes=(UNVERIFIED,))

    assert what(unproved) == ["kept-open"]
    assert "never got to the end" in unproved.posted[0].detail
    assert len(platform.tracked) == 1
    assert not platform.notes


def test_a_capability_that_never_ran_cannot_close_anything(platform: FakePlatform) -> None:
    track(platform, verdict_of(judged(finding())))

    narrowed = track(platform, verdict_of(), outcomes=())

    assert what(narrowed) == ["kept-open"]
    assert "did not run" in narrowed.posted[0].detail
    assert len(platform.tracked) == 1


def test_one_ecosystem_of_a_capability_cannot_close_another(platform: FakePlatform) -> None:
    """`--only deps-outdated@cargo` must not close a python-uv issue: same capability, different task."""
    cargo_cap = "capabilities/deps-outdated"
    cargo = Finding(
        capability=cargo_cap,
        klass=Klass.ROUTINE,
        severity=Severity.MEDIUM,
        subject=Subject(ecosystem="ecosystems/cargo", package="serde", version="1.0.0"),
        summary="serde is outdated",
        rationale="registry has a newer cleared release.",
        remediation="Bump serde.",
        location=Location(path="Cargo.toml", line=1),
    )
    track(
        platform,
        verdict_of(judged(cargo)),
        outcomes=(
            TaskOutcome(
                id="deps-outdated@cargo",
                capability=cargo_cap,
                required=True,
                outcome=Outcome.FINDINGS,
            ),
        ),
    )
    assert len(platform.tracked) == 1

    # A later run that only completes python-uv must not treat the cargo finding as absent-proven.
    only_uv = TaskOutcome(
        id="deps-outdated@python-uv",
        capability=cargo_cap,
        required=True,
        outcome=Outcome.CLEAN,
    )
    memory: dict[str, Any] = {}
    first = track(
        platform,
        verdict_of(),
        outcomes=(only_uv,),
        memory=memory,
    )
    second = track(
        platform,
        verdict_of(),
        outcomes=(only_uv,),
        memory=memory,
    )
    assert what(first) == ["kept-open"]
    assert "ecosystems/cargo" in first.posted[0].detail
    assert what(second) == ["kept-open"]
    assert len(platform.tracked) == 1
    assert not platform.closed


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
