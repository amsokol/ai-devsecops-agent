"""Vocabulary shared by the whole run: triggers, roles, outcomes, planned tasks.

Names here mirror the library contract. When the contract renames something, this module is the
only place that has to follow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from agent.errors import ExitCode


class Trigger(StrEnum):
    CHANGE_OPENED = "change-opened"
    CHANGE_UPDATED = "change-updated"
    COMMENT_ON_CHANGE = "comment-on-change"
    """Somebody replied in one of the agent's threads on a change: a remark on a line, answered.

    A wake has two questions, and they are answered by different things. *Where* the comment is
    decides which playbook applies — this one is the review's business, a comment on an issue is
    maintenance — and that is known from the event, before any model runs. *What it says* decides
    only the course, and that is the one place a model reads free text.
    """
    COMMENT_ON_ISSUE = "comment-on-issue"
    """Somebody answered one of the agent's issues, which is maintenance work with a person
    waiting."""
    MAINTAIN_REQUESTED = "maintain-requested"
    MAINTAIN_SCHEDULED = "maintain-scheduled"

    @property
    def is_maintenance(self) -> bool:
        return self in {
            Trigger.COMMENT_ON_ISSUE,
            Trigger.MAINTAIN_REQUESTED,
            Trigger.MAINTAIN_SCHEDULED,
        }

    @property
    def is_scheduled(self) -> bool:
        return self is Trigger.MAINTAIN_SCHEDULED

    @property
    def is_woken(self) -> bool:
        """Somebody's words started this run, which is the one case where who they were matters."""
        return self in {Trigger.COMMENT_ON_CHANGE, Trigger.COMMENT_ON_ISSUE}


class Role(StrEnum):
    INTENT = "intent"
    ANALYST = "analyst"
    FIXER = "fixer"
    WRITER = "writer"


class Outcome(StrEnum):
    """How a task ended. Absence of a result is never success, so there is no default."""

    FINDINGS = "findings"
    CLEAN = "clean"
    UNVERIFIED = "unverified"
    EXHAUSTED = "exhausted"


class FixOutcome(StrEnum):
    """How a fix task ended. A separate vocabulary because it answers a different question.

    An analysis task says what is true about the code; a fix task says whether a change was made and
    proved safe. Refusing is a correct answer here: a branch that ships on a hope costs more than
    one that never appeared, because the next person has to establish whether it is safe anyway.
    """

    FIXED = "fixed"
    REFUSED = "refused"
    UNVERIFIED = "unverified"
    EXHAUSTED = "exhausted"

    @property
    def shipped(self) -> bool:
        return self is FixOutcome.FIXED


class Intent(StrEnum):
    """What somebody's comment asks the agent for.

    The one vocabulary a model assigns to free text. It is deliberately small and deliberately not
    an action: what each intent causes is a table in `agent.intent`, so a misread comment costs a
    wasted session and never a permission the person did not give.
    """

    UNLOCK = "unlock"
    """Permission for the change the agent said it was holding back."""
    FIX = "fix"
    """How to fix it, or a fix prepared."""
    QUESTION = "question"
    """An explanation, which is answered without touching anything."""
    RECHECK = "recheck"
    """The facts have changed — look again."""
    UNRELATED = "unrelated"
    """Nothing the agent does. A run that stops here is the cheapest correct outcome there is."""


class AnswerOutcome(StrEnum):
    """How an answering session ended. Its own vocabulary, because it produces no findings."""

    ANSWERED = "answered"
    UNVERIFIED = "unverified"
    EXHAUSTED = "exhausted"


class Reason(StrEnum):
    """Why a fact could not be established.

    The split between an expected gap and a failure decides whether the run may still pass, so it
    is derived from the reason rather than chosen per case.
    """

    NO_TOOLING = "no-tooling"
    UNAVAILABLE = "unavailable"
    UNEXPECTED_SHAPE = "unexpected-shape"
    NOT_PERMITTED = "not-permitted"
    EXHAUSTED = "exhausted"
    INVALID_RESULT = "invalid-result"
    NOT_IMPLEMENTED = "not-implemented"

    @property
    def is_expected_gap(self) -> bool:
        return self is Reason.NO_TOOLING

    @property
    def is_failure(self) -> bool:
        return not self.is_expected_gap


class RunResult(StrEnum):
    PASS = "pass"  # noqa: S105 - a verdict, not a credential
    BLOCKED = "blocked"
    INCONCLUSIVE = "inconclusive"

    @property
    def exit_code(self) -> ExitCode:
        match self:
            case RunResult.PASS:
                return ExitCode.OK
            case RunResult.BLOCKED:
                return ExitCode.BLOCKED
            case RunResult.INCONCLUSIVE:
                return ExitCode.INCONCLUSIVE


@dataclass(frozen=True, slots=True)
class PlannedTask:
    """One unit of work handed to one subagent.

    `id` is stable for the same input, because it ends up in the manifest and in comparisons
    between runs.
    """

    id: str
    capability: str
    role: Role
    required: bool
    ecosystem: str | None = None
    scope: tuple[str, ...] = field(default_factory=tuple)
    knowledge: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class Plan:
    playbook: str
    trigger: Trigger
    tasks: tuple[PlannedTask, ...]
    skipped: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    """Capabilities that did not become tasks, each with the reason, so the report can say N/A."""
