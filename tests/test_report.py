"""The report a human reads: what it must say, and what it must not omit."""

from __future__ import annotations

from dataclasses import replace

from agent.domain import Outcome, Reason, RunResult, Trigger
from agent.evidence import Reliability, Subject
from agent.findings import Action, Finding, Klass, Severity
from agent.report import render
from agent.verdict import Judged, TaskOutcome, Verdict

TASK = TaskOutcome(
    id="deps-vuln@python-uv",
    capability="capabilities/deps-vuln",
    required=True,
    outcome=Outcome.FINDINGS,
)


def advisory_finding(identifier: str, *, severity: Severity = Severity.HIGH) -> Finding:
    return Finding(
        capability="capabilities/deps-vuln",
        klass=Klass.SECURITY,
        severity=severity,
        subject=Subject(ecosystem="ecosystems/python-uv", package="jinja2", version="3.1.3"),
        summary=f"jinja2 3.1.3 is affected by {identifier}",
        rationale="pip-audit reports it against the resolved pin.",
        evidence=("advisories|ecosystems/python-uv|jinja2|3.1.3|",),
        remediation="Bump jinja2 to 3.1.6.",
        advisory=identifier,
    )


def judged(finding: Finding, *, action: Action = Action.BLOCK) -> Judged:
    return Judged(
        finding=finding, action=action, reliability=Reliability.REPRODUCIBLE, capped=False
    )


def report_of(verdict: Verdict) -> str:
    return render(
        verdict,
        trigger=Trigger.CHANGE_OPENED,
        tasks=(TASK,),
        library_version="0.1.1",
    )


def test_advisories_against_one_pin_read_as_one_thing_to_do() -> None:
    items = tuple(judged(advisory_finding(name)) for name in ("PYSEC-1", "PYSEC-2", "PYSEC-3"))
    verdict = Verdict(result=RunResult.BLOCKED, judged=items, blocking=items)

    report = report_of(verdict)

    assert report.count("- **high**") == 1
    assert "Same subject: PYSEC-2, PYSEC-3." in report
    assert report.count("Remediation:") == 1


def test_the_worst_severity_leads_the_group() -> None:
    items = (
        judged(advisory_finding("PYSEC-1", severity=Severity.LOW)),
        judged(advisory_finding("PYSEC-2", severity=Severity.CRITICAL)),
    )
    verdict = Verdict(result=RunResult.BLOCKED, judged=items, blocking=items)

    assert "- **critical**" in report_of(verdict)


def test_separate_subjects_stay_separate() -> None:
    other = replace(advisory_finding("PYSEC-9"), subject=Subject(package="httpx", version="0.28.1"))
    items = (judged(advisory_finding("PYSEC-1")), judged(other))
    verdict = Verdict(result=RunResult.BLOCKED, judged=items, blocking=items)

    assert report_of(verdict).count("- **high**") == 2


def test_an_inconclusive_run_says_it_claims_nothing() -> None:
    failed = TaskOutcome(
        id="deps-vuln@python-uv",
        capability="capabilities/deps-vuln",
        required=True,
        outcome=Outcome.UNVERIFIED,
        reason=Reason.UNAVAILABLE,
    )
    verdict = Verdict(result=RunResult.INCONCLUSIVE, failed_tasks=(failed,))

    report = render(
        verdict, trigger=Trigger.CHANGE_OPENED, tasks=(failed,), library_version="0.1.1"
    )

    assert "says nothing about the code" in report
    assert "- `capabilities/deps-vuln` — unavailable" in report


def test_a_run_that_got_through_less_of_the_tree_says_so_beside_its_findings() -> None:
    """A short sweep and a thorough one produce the same shape of report, and the reader cannot tell
    them apart from the findings alone — so the qualification goes where the findings are."""
    noted = judged(advisory_finding("PYSEC-1"), action=Action.COMMENT)
    verdict = Verdict(result=RunResult.PASS, judged=(noted,))

    report = render(
        verdict,
        trigger=Trigger.MAINTAIN_SCHEDULED,
        tasks=(TASK,),
        library_version="0.1.1",
        shortfall=("deps-outdated examined 4 package(s), against 6 in the last run",),
    )

    assert "### Not examined this run" in report
    assert "against 6 in the last run" in report
    assert report.index("Not examined") > report.index("PYSEC-1")


def test_heuristic_evidence_explains_why_it_did_not_block() -> None:
    capped = Judged(
        finding=advisory_finding("PYSEC-1"),
        action=Action.COMMENT,
        reliability=Reliability.HEURISTIC,
        capped=True,
    )
    verdict = Verdict(result=RunResult.PASS, judged=(capped,))

    assert "Not blocking" in report_of(verdict)
