"""Findings into a decision: keys, deduplication, the evidence ceiling, the run result."""

from __future__ import annotations

from agent.domain import Outcome, Reason, RunResult, Trigger
from agent.evidence import Reliability, Subject
from agent.findings import Action, Finding, Kind, Klass, Location, Severity, merge
from agent.policy import BlockingRules
from agent.report import render
from agent.verdict import TaskOutcome, decide, judge

TOOL = "advisories|ecosystems/python-uv|httpx|0.28.1|"
WEB = "latest-version|ecosystems/bsr|buf.build/acme/api||"
RELIABILITY = {TOOL: Reliability.REPRODUCIBLE, WEB: Reliability.HEURISTIC}
RULES = BlockingRules(
    blocking=frozenset(
        {
            (Klass.SECURITY, Severity.CRITICAL),
            (Klass.SECURITY, Severity.HIGH),
            (Klass.ROUTINE, Severity.CRITICAL),
        }
    ),
    forbidden_state_blocks=True,
    source="test:policy/verdicts",
)


def dependency(
    *,
    severity: Severity = Severity.HIGH,
    klass: Klass = Klass.SECURITY,
    evidence: tuple[str, ...] = (TOOL,),
    version: str = "0.28.1",
    advisory: str = "GHSA-xxxx",
) -> Finding:
    return Finding(
        capability="capabilities/deps-vuln",
        klass=klass,
        severity=severity,
        subject=Subject(ecosystem="ecosystems/python-uv", package="httpx", version=version),
        summary="httpx is affected by an advisory.",
        rationale="The advisory covers the pinned version.",
        evidence=evidence,
        advisory=advisory,
    )


def pin(
    *, kind: Kind, summary: str = "actions/checkout is pinned to a moving reference."
) -> Finding:
    return Finding(
        capability="capabilities/deps-outdated",
        klass=Klass.ROUTINE,
        severity=Severity.LOW,
        subject=Subject(ecosystem="ecosystems/github-actions", package="actions/checkout"),
        summary=summary,
        rationale="The reference does not name a released version.",
        evidence=(TOOL,),
        kind=kind,
    )


def code(*, line: int, summary: str = "Unchecked index access can panic.") -> Finding:
    return Finding(
        capability="capabilities/code-quality",
        klass=Klass.ROUTINE,
        severity=Severity.MEDIUM,
        subject=Subject(path="src/api.py"),
        summary=summary,
        rationale="The slice may be shorter than the index.",
        location=Location(path="src/api.py", line=line),
        symbol="handle",
    )


def clean(identifier: str) -> TaskOutcome:
    return TaskOutcome(
        id=identifier, capability=f"capabilities/{identifier}", required=True, outcome=Outcome.CLEAN
    )


def test_a_key_ignores_what_drifts_between_runs() -> None:
    assert dependency(version="0.28.1").key == dependency(version="0.29.0").key
    assert code(line=10).key == code(line=99).key
    assert code(line=10).key != code(line=10, summary="Something else entirely.").key


def test_a_pin_keeps_its_key_when_the_wording_changes() -> None:
    """The second live maintenance run reworded four summaries and raised four duplicate issues.

    One of them carried an approval a person had given, which no longer matched anything, so the
    agent asked them for it again.
    """
    first = pin(
        kind=Kind.QUARANTINE, summary="actions/checkout@v7 resolves to a quarantined v7.0.1."
    )
    reworded = pin(
        kind=Kind.QUARANTINE,
        summary="The actions/checkout@v7 pin resolves to v7.0.1, in quarantine.",
    )

    assert first.key == reworded.key
    assert first.key.endswith(":quarantine")


def test_two_problems_with_one_pin_stay_two_findings() -> None:
    """Being stable is not being coarse: a floating reference and a quarantined version are not the
    same problem, and merging them would silence whichever was reported second."""
    assert pin(kind=Kind.QUARANTINE).key != pin(kind=Kind.FLOATING).key


def test_one_problem_found_twice_is_judged_by_the_stricter_verdict() -> None:
    merged = merge((dependency(severity=Severity.MEDIUM), dependency(severity=Severity.CRITICAL)))

    assert len(merged) == 1
    assert merged[0].severity is Severity.CRITICAL
    assert merged[0].evidence == (TOOL,)


def test_reproducible_evidence_blocks_and_the_run_refuses() -> None:
    judged = judge((dependency(),), rules=RULES, reliabilities=RELIABILITY)
    verdict = decide((clean("deps-vuln"),), judged)

    assert judged[0].action is Action.BLOCK
    assert verdict.result is RunResult.BLOCKED


def test_heuristic_evidence_comments_and_says_why() -> None:
    judged = judge((dependency(evidence=(WEB,)),), rules=RULES, reliabilities=RELIABILITY)
    verdict = decide((clean("deps-vuln"),), judged)
    body = render(
        verdict,
        trigger=Trigger.CHANGE_OPENED,
        tasks=(clean("deps-vuln"),),
        library_version="0.1.1",
    )

    assert judged[0].action is Action.COMMENT
    assert judged[0].capped
    assert verdict.result is RunResult.PASS
    assert "Not blocking" in body


def test_a_finding_with_no_evidence_cannot_block() -> None:
    judged = judge((dependency(evidence=()),), rules=RULES, reliabilities=RELIABILITY)

    assert judged[0].action is Action.COMMENT
    assert judged[0].reliability is Reliability.HEURISTIC


def test_mixed_evidence_falls_to_the_weakest() -> None:
    judged = judge((dependency(evidence=(TOOL, WEB)),), rules=RULES, reliabilities=RELIABILITY)

    assert judged[0].reliability is Reliability.HEURISTIC


def test_a_forbidden_state_blocks_at_any_severity() -> None:
    forbidden = Finding(
        capability="capabilities/deps-outdated",
        klass=Klass.ROUTINE,
        severity=Severity.LOW,
        subject=Subject(ecosystem="ecosystems/npm", package="left-pad", version="1.3.0"),
        summary="left-pad 1.3.0 is inside the quarantine window.",
        rationale="Published two days ago; the window is seven.",
        evidence=(TOOL,),
        forbidden_state=True,
    )

    judged = judge((forbidden,), rules=RULES, reliabilities=RELIABILITY)

    assert judged[0].action is Action.BLOCK


def test_a_failed_required_task_makes_the_run_inconclusive() -> None:
    failed = TaskOutcome(
        id="deps-vuln",
        capability="capabilities/deps-vuln",
        required=True,
        outcome=Outcome.UNVERIFIED,
        reason=Reason.UNAVAILABLE,
    )

    verdict = decide((clean("code-vuln"), failed), ())
    body = render(
        verdict,
        trigger=Trigger.CHANGE_OPENED,
        tasks=(clean("code-vuln"), failed),
        library_version="0.1.1",
    )

    assert verdict.result is RunResult.INCONCLUSIVE
    assert "Did not run" in body
    assert "says nothing about the code" in body


def test_a_documented_gap_still_allows_a_pass() -> None:
    gap = TaskOutcome(
        id="deps-vuln",
        capability="capabilities/deps-vuln",
        required=True,
        outcome=Outcome.UNVERIFIED,
        reason=Reason.NO_TOOLING,
    )

    verdict = decide((gap,), ())
    body = render(verdict, trigger=Trigger.CHANGE_OPENED, tasks=(gap,), library_version="0.1.1")

    assert verdict.result is RunResult.PASS
    assert "Known gaps" in body


def test_an_exhausted_task_is_a_failure_even_without_a_reason() -> None:
    exhausted = TaskOutcome(
        id="code-vuln",
        capability="capabilities/code-vuln",
        required=True,
        outcome=Outcome.EXHAUSTED,
    )

    assert decide((exhausted,), ()).result is RunResult.INCONCLUSIVE


def test_an_optional_task_that_failed_does_not_poison_the_run() -> None:
    optional = TaskOutcome(
        id="code-quality",
        capability="capabilities/code-quality",
        required=False,
        outcome=Outcome.UNVERIFIED,
        reason=Reason.UNAVAILABLE,
    )

    assert decide((clean("code-vuln"), optional), ()).result is RunResult.PASS


def test_a_demonstrated_block_outranks_a_broken_check() -> None:
    failed = TaskOutcome(
        id="code-vuln",
        capability="capabilities/code-vuln",
        required=True,
        outcome=Outcome.UNVERIFIED,
        reason=Reason.UNAVAILABLE,
    )
    judged = judge((dependency(),), rules=RULES, reliabilities=RELIABILITY)

    verdict = decide((clean("deps-vuln"), failed), judged)
    body = render(
        verdict,
        trigger=Trigger.CHANGE_OPENED,
        tasks=(clean("deps-vuln"), failed),
        library_version="0.1.1",
    )

    assert verdict.result is RunResult.BLOCKED
    assert "Did not run" in body


def test_a_run_with_no_tasks_claims_nothing() -> None:
    assert decide((), ()).result is RunResult.INCONCLUSIVE


def test_the_report_is_byte_identical_for_one_input() -> None:
    judged = judge(merge((dependency(), code(line=12))), rules=RULES, reliabilities=RELIABILITY)
    verdict = decide((clean("deps-vuln"), clean("code-quality")), judged)
    tasks = (clean("deps-vuln"), clean("code-quality"))

    first = render(verdict, trigger=Trigger.CHANGE_OPENED, tasks=tasks, library_version="0.1.1")
    second = render(verdict, trigger=Trigger.CHANGE_OPENED, tasks=tasks, library_version="0.1.1")

    assert first == second
    assert "### Blocking" in first
    assert "### Findings" in first
