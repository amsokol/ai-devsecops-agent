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
from agent.findings import Action
from agent.verdict import Judged, TaskOutcome, Verdict

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
) -> str:
    lines = [f"## {HEADLINES[verdict.result]}", ""]

    if verdict.result is RunResult.INCONCLUSIVE:
        lines += [
            "This says nothing about the code: the checks below did not run to completion, so the "
            "run cannot claim they passed.",
            "",
        ]

    if verdict.blocking:
        lines.append("### Blocking")
        lines.append("")
        lines += [_entry(item) for item in verdict.blocking]
        lines.append("")

    comments = tuple(item for item in verdict.judged if item.action is Action.COMMENT)
    if comments:
        lines.append("### Findings")
        lines.append("")
        lines += [_entry(item) for item in comments]
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

    if not verdict.judged and not verdict.failed_tasks:
        lines += ["Nothing to report.", ""]

    checked = ", ".join(sorted({task.capability.rsplit("/", 1)[-1] for task in tasks})) or "nothing"
    footer = f"Checked: {checked}. Knowledge library {library_version}, trigger `{trigger.value}`."
    if unverified_facts:
        footer += f" {unverified_facts} fact(s) could not be established."
    lines.append(footer)
    return "\n".join(lines) + "\n"


def _entry(item: Judged) -> str:
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
