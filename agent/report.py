"""The report body: what a human reads to decide whether to argue with the run.

Rendered from the verdict, in a fixed order, so two runs on one input produce identical text. That
is what makes the report diffable and what stops a reviewer from wondering whether something
changed.

A gap and a failure are always named. A report that quietly omits what could not be checked converts
incompleteness into apparent approval, which is the failure mode this whole design is built against.
"""

from __future__ import annotations

from agent.domain import RunResult, Trigger
from agent.evidence import Reliability
from agent.findings import Action, Severity
from agent.remediate import Fix
from agent.verdict import Judged, TaskOutcome, Verdict

SEVERITY_ORDER = (Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL)

HEADLINES = {
    RunResult.BLOCKED: "Changes requested",
    RunResult.INCONCLUSIVE: "Inconclusive — the check did not complete",
    RunResult.PASS: "No blocking findings",
}


def render(
    verdict: Verdict,
    *,
    trigger: Trigger,
    tasks: tuple[TaskOutcome, ...],
    library_version: str,
    unverified_facts: int = 0,
    fixes: tuple[Fix, ...] = (),
    notice: str = "",
) -> str:
    lines = [f"## {HEADLINES[verdict.result]}", ""]

    if notice:
        # Above the findings, because it says which rules produced them. A reader who learns that
        # after the verdict has already read the verdict as something it is not.
        lines += [f"> {notice}.", ""]

    if verdict.result is RunResult.INCONCLUSIVE:
        lines += [
            "This says nothing about the code: the checks below did not run to completion, so the "
            "run cannot claim they passed.",
            "",
        ]

    if verdict.blocking:
        lines.append("### Blocking")
        lines.append("")
        lines += _entries(verdict.blocking)
        lines.append("")

    comments = tuple(item for item in verdict.judged if item.action is Action.COMMENT)
    if comments:
        lines.append("### Findings")
        lines.append("")
        lines += _entries(comments)
        lines.append("")

    if verdict.failed_tasks:
        lines.append("### Did not run")
        lines.append("")
        lines += [
            f"- `{task.capability}` — {task.reason.value if task.reason else 'no result'}"
            for task in verdict.failed_tasks
        ]
        lines.append("")

    if verdict.gaps:
        lines.append("### Known gaps")
        lines.append("")
        lines += [
            f"- `{task.capability}` — the ecosystem declares this unobtainable, so it was not "
            "attempted"
            for task in verdict.gaps
        ]
        lines.append("")

    lines += _fixes(fixes)

    if not verdict.judged and not verdict.failed_tasks:
        lines += ["Nothing to report.", ""]

    checked = ", ".join(sorted({task.capability.rsplit("/", 1)[-1] for task in tasks})) or "nothing"
    footer = f"Checked: {checked}. Knowledge library {library_version}, trigger `{trigger.value}`."
    if unverified_facts:
        footer += f" {unverified_facts} fact(s) could not be established."
    lines.append(footer)
    return "\n".join(lines) + "\n"


def _fixes(fixes: tuple[Fix, ...]) -> list[str]:
    """Prepared branches, and the fixes that were deliberately not shipped.

    Both halves matter. A branch is what a human acts on; a refusal is what stops them from assuming
    the agent quietly handled it. An empty section is omitted entirely rather than announced.
    """
    if not fixes:
        return []
    lines = ["### Fixes prepared", ""]
    for fix in sorted(fixes, key=lambda item: item.job.task.id):
        finding = fix.job.judged.finding
        named = finding.subject.package or finding.subject.path or finding.capability
        if fix.outcome.shipped:
            surfaces = ", ".join(fix.verification.verified) if fix.verification else ""
            lines.append(f"- `{fix.branch}` — {named}: {fix.notes}")
            if surfaces:
                lines.append(f"  Verified: {surfaces}.")
        else:
            lines.append(f"- Not shipped — {named}: {fix.detail or fix.outcome.value}")
    lines.append("")
    return lines + _red_base(fixes)


def _red_base(fixes: tuple[Fix, ...]) -> list[str]:
    """Say it once, plainly, when the product's own checks were failing before any fix was tried.

    Otherwise a week of refusals reads as the agent being unable to bump a dependency, and the team
    would be debugging the wrong thing. Named per command rather than per fix: it is one problem in
    the repository, however many fixes it stopped.
    """
    inherited = {
        " ".join(command)
        for fix in fixes
        if fix.verification is not None
        for command in fix.verification.pre_existing
    }
    if not inherited:
        return []
    listed = ", ".join(f"`{command}`" for command in sorted(inherited))
    return [
        "Some verification was already failing before any fix was attempted, so no fix on those "
        f"surfaces could be proved safe: {listed}. Fixing that unblocks the automated ones.",
        "",
    ]


def _entries(items: tuple[Judged, ...]) -> list[str]:
    """One entry per subject, not per advisory.

    Four advisories against one pin are one thing to do, and four near-identical paragraphs asking
    for the same bump read as noise. The manifest keeps them separate, because each is its own
    finding for the purpose of not reopening what a human already answered.
    """
    grouped: dict[str, list[Judged]] = {}
    for item in items:
        grouped.setdefault(item.finding.subject.key(), []).append(item)
    return [_entry(group) for group in grouped.values()]


def _entry(group: list[Judged]) -> str:
    item = max(group, key=lambda judged: SEVERITY_ORDER.index(judged.finding.severity))
    finding = item.finding
    subject = finding.subject
    named = subject.package or subject.path or "—"
    if subject.version:
        named += f" {subject.version}"
    where = ""
    if finding.location:
        where = f" ({finding.location.path}"
        where += f":{finding.location.line})" if finding.location.line else ")"
    headline = f"**{finding.severity.value}** `{finding.klass.value}` {named}{where}"
    text = f"- {headline} — {finding.summary}"
    text += f"\n  {finding.rationale}"
    others = sorted(
        {
            other.finding.advisory
            for other in group
            if other.finding.advisory and other.finding.advisory != finding.advisory
        }
    )
    if others:
        text += f"\n  Same subject: {', '.join(others)}."
    if finding.remediation:
        text += f"\n  Remediation: {finding.remediation}"
    if item.capped:
        # Saying why it did not block is the point: a human can promote it in seconds, and silence
        # here would look like the finding was judged unimportant.
        text += (
            "\n  Not blocking: the evidence behind it is heuristic, so it is reported for "
            "confirmation rather than enforced."
        )
    elif item.reliability is Reliability.HEURISTIC:
        text += "\n  Evidence: heuristic."
    return text
