"""Proposing a prepared fix: pushed first, linked to its issues, and never closing them."""

from __future__ import annotations

from pathlib import Path

import pytest
from agent.domain import FixOutcome, PlannedTask, Role
from agent.evidence import Reliability, Subject
from agent.findings import Action, Finding, Klass, Severity
from agent.propose import propose_fixes
from agent.remediate import Fix, FixJob, branch_for
from agent.scm.fake import FakePlatform
from agent.scm.marker import read
from agent.verdict import Judged
from agent.verification import Ran, Verification

RUN = "run-20260725-000000"


def finding(*, advisory: str = "PYSEC-2026-1", package: str = "jinja2") -> Finding:
    return Finding(
        capability="capabilities/deps-vuln",
        klass=Klass.SECURITY,
        severity=Severity.HIGH,
        subject=Subject(ecosystem="ecosystems/python-uv", package=package, version="3.1.3"),
        summary=f"{package} 3.1.3 is affected by {advisory}",
        rationale="pip-audit reports it against the resolved pin.",
        remediation=f"Bump {package} to 3.1.6.",
        advisory=advisory,
    )


def judged(item: Finding) -> Judged:
    return Judged(
        finding=item, action=Action.BLOCK, reliability=Reliability.REPRODUCIBLE, capped=False
    )


def verified(*, pre_existing: tuple[tuple[str, ...], ...] = ()) -> Verification:
    return Verification(
        ran=(Ran(surface="python", command=("uv", "sync", "--frozen"), ok=True),),
        verified=("python",),
        passed=True,
        pre_existing=pre_existing,
    )


def fix(
    *,
    outcome: FixOutcome = FixOutcome.FIXED,
    also: tuple[Judged, ...] = (),
    verification: Verification | None = None,
    notes: str = "Bumped the pin and re-locked.",
    package: str = "jinja2",
) -> Fix:
    first = judged(finding(package=package))
    job = FixJob(
        task=PlannedTask(
            id=f"fix-security-{package}-abc123",
            capability="capabilities/deps-vuln",
            role=Role.FIXER,
            required=False,
        ),
        judged=first,
        also=also,
        branch=branch_for(first),
    )
    return Fix(
        job=job,
        outcome=outcome,
        notes=notes,
        branch=job.branch if outcome is FixOutcome.FIXED else "",
        changed=("pyproject.toml", "uv.lock"),
        verification=verification or verified(),
    )


@pytest.fixture
def platform() -> FakePlatform:
    return FakePlatform()


def test_a_verified_fix_is_pushed_and_then_proposed(platform: FakePlatform) -> None:
    prepared = fix()

    record = propose_fixes(
        platform, fixes=(prepared,), path=Path("/repo"), base="main", issues={}, run=RUN
    )

    assert [item.what for item in record.posted] == ["proposed"]
    assert record.opened == 1
    assert platform.pushed == [prepared.branch]
    assert [call.what for call in platform.calls] == ["push", "propose"]
    assert platform.proposed[0].head == prepared.branch


def test_a_branch_that_cannot_be_pushed_is_never_proposed(platform: FakePlatform) -> None:
    """A change request pointing at a branch nobody has is a broken link to investigate."""
    prepared = fix()
    platform.unpushable = (prepared.branch,)

    record = propose_fixes(
        platform, fixes=(prepared,), path=Path("/repo"), base="main", issues={}, run=RUN
    )

    assert [item.what for item in record.posted] == ["not-pushed"]
    assert not record.opened
    assert not platform.proposed


def test_one_branch_that_fails_does_not_cost_the_others(platform: FakePlatform) -> None:
    first, second = fix(), fix(package="urllib3")
    platform.unpushable = (first.branch,)

    record = propose_fixes(
        platform, fixes=(first, second), path=Path("/repo"), base="main", issues={}, run=RUN
    )

    assert [item.what for item in record.posted] == ["not-pushed", "proposed"]
    assert record.opened == 1


def test_a_fix_that_did_not_ship_is_not_proposed(platform: FakePlatform) -> None:
    record = propose_fixes(
        platform,
        fixes=(fix(outcome=FixOutcome.REFUSED),),
        path=Path("/repo"),
        base="main",
        issues={},
        run=RUN,
    )

    assert not record.posted
    assert not platform.pushed


def test_the_body_links_the_issues_it_answers_without_closing_them(platform: FakePlatform) -> None:
    """No closing keyword: a merge would close on the platform's word, and closing needs evidence
    that the check which owns the finding looked again and found nothing."""
    extra = judged(finding(advisory="PYSEC-2026-2"))
    prepared = fix(also=(extra,))

    propose_fixes(
        platform,
        fixes=(prepared,),
        path=Path("/repo"),
        base="main",
        issues={prepared.job.key: 41, extra.finding.key: 42},
        run=RUN,
    )

    _, body = platform.bodies[0]
    assert "remediates #41" in body
    assert "remediates #42" in body
    assert "fixes #" not in body.lower()
    assert "closes #" not in body.lower()
    assert read(body) == prepared.job.key


def test_the_body_states_what_ran_and_what_was_already_failing(platform: FakePlatform) -> None:
    prepared = fix(verification=verified(pre_existing=(("uv", "run", "ruff"),)))

    propose_fixes(platform, fixes=(prepared,), path=Path("/repo"), base="main", issues={}, run=RUN)

    _, body = platform.bodies[0]
    assert "`python`" in body
    assert "already failing on the base commit" in body
    assert "`uv run ruff`" in body
    assert "Bumped the pin" in body
    assert RUN in body


def test_a_title_reads_the_same_way_for_the_same_subject(platform: FakePlatform) -> None:
    """Not built from the fix task's prose: a title that moves hides that this is last week's
    change request's successor."""
    propose_fixes(platform, fixes=(fix(),), path=Path("/repo"), base="main", issues={}, run=RUN)
    first = next(call.detail for call in platform.calls if call.what == "propose")

    platform.proposed.clear()
    platform.pushed.clear()
    platform.calls.clear()
    propose_fixes(
        platform,
        fixes=(fix(notes="Different words entirely."),),
        path=Path("/repo"),
        base="main",
        issues={},
        run="run-later",
    )
    again = next(call.detail for call in platform.calls if call.what == "propose")

    assert first == again
    assert "jinja2" in first
    assert "3.1.3" not in first
