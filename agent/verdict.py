"""From task outcomes and findings to one decision.

All of it is arithmetic over recorded facts. Nothing here asks a model, because the verdict is
what CI acts on: a gate that answers differently on a rerun of the same commit stops being
believed, and then it gets bypassed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agent.domain import Outcome, Reason, RunResult
from agent.evidence import Reliability
from agent.findings import Action, Finding
from agent.policy import BlockingRules


@dataclass(frozen=True, slots=True)
class Judged:
    """A finding with the action it is actually allowed to take, and why not more."""

    finding: Finding
    action: Action
    reliability: Reliability
    capped: bool
    """True when policy would have blocked but the evidence only permits a comment."""

    def as_json(self) -> dict[str, object]:
        return self.finding.as_json() | {
            "action": self.action.value,
            "reliability": self.reliability.value,
            "capped_by_evidence": self.capped,
        }


@dataclass(frozen=True, slots=True)
class TaskOutcome:
    """What the core concluded about one task, after its result was validated."""

    id: str
    capability: str
    required: bool
    outcome: Outcome
    reason: Reason | None = None

    @property
    def failed(self) -> bool:
        """A failure kind, as opposed to a documented gap.

        `no-tooling` is a limit the ecosystem document declares, so it does not poison the run;
        every other reason means something that was supposed to work did not.
        """
        if self.outcome is Outcome.EXHAUSTED:
            return True
        if self.outcome is Outcome.UNVERIFIED:
            return self.reason is None or self.reason.is_failure
        return False


@dataclass(frozen=True, slots=True)
class Verdict:
    result: RunResult
    judged: tuple[Judged, ...] = field(default_factory=tuple)
    blocking: tuple[Judged, ...] = field(default_factory=tuple)
    failed_tasks: tuple[TaskOutcome, ...] = field(default_factory=tuple)
    gaps: tuple[TaskOutcome, ...] = field(default_factory=tuple)

    def as_json(self) -> dict[str, object]:
        return {
            "result": self.result.value,
            "blocking": [item.finding.key for item in self.blocking],
            "failed_tasks": [item.id for item in self.failed_tasks],
            "gaps": [item.id for item in self.gaps],
        }


def judge(
    findings: tuple[Finding, ...],
    *,
    rules: BlockingRules,
    reliabilities: dict[str, Reliability],
) -> tuple[Judged, ...]:
    """Decide each finding's action: what policy allows, capped by what the evidence supports."""
    decided: list[Judged] = []
    for finding in findings:
        reliability = finding.reliability(reliabilities)
        permitted = rules.blocks(
            finding.klass, finding.severity, forbidden_state=finding.forbidden_state
        )
        demonstrated = reliability is Reliability.REPRODUCIBLE
        action = Action.BLOCK if permitted and demonstrated else Action.COMMENT
        decided.append(
            Judged(
                finding=finding,
                action=action,
                reliability=reliability,
                capped=permitted and not demonstrated,
            )
        )
    return tuple(decided)


def decide(tasks: tuple[TaskOutcome, ...], judged: tuple[Judged, ...]) -> Verdict:
    """The run's result.

    A blocking finding outranks a failed check when both are present. Both refuse the merge, so the
    choice is only about what the run says, and "this specific thing is wrong, here is the evidence"
    is more useful than "something did not run" — the failed checks are named in the report either
    way. The reverse order would hide a demonstrated problem behind an infrastructure complaint.
    """
    blocking = tuple(item for item in judged if item.action is Action.BLOCK)
    failed = tuple(task for task in tasks if task.required and task.failed)
    gaps = tuple(
        task
        for task in tasks
        if task.outcome is Outcome.UNVERIFIED and task.reason is Reason.NO_TOOLING
    )
    if blocking:
        result = RunResult.BLOCKED
    elif failed or not tasks:
        # No tasks at all is not a pass: absence of a result is not a result.
        result = RunResult.INCONCLUSIVE
    else:
        result = RunResult.PASS
    return Verdict(result=result, judged=judged, blocking=blocking, failed_tasks=failed, gaps=gaps)
