"""Turning findings into verified fix branches.

Two decisions live here, and both are the agent's rather than a model's.

Which findings get a fix at all: only a demonstrated problem with a stated remedy, in a fixed order,
within the queue the product allows. Selection by a model would make the same repository produce a
different set of branches every week, and a maintenance run has to be boring to be trusted.

Whether a fix ships: the worktree has to differ, verification has to have run, and nothing that ran
may have failed. Only then does the agent commit. The subagent's own claim is prose for the human
who reads the branch; it never decides the outcome by itself.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent.backends.port import Budget, Failure
from agent.backends.select import Roster
from agent.brief import FIX_RESULT_SHAPE, compose, knowledge_for, role_instructions
from agent.budget import Ledger
from agent.domain import FixOutcome, PlannedTask, Reason, Role
from agent.errors import ConfigError
from agent.evidence import Reliability
from agent.executor import Attempt, run_attempts
from agent.findings import slug
from agent.library import Library
from agent.overlay import Overlay
from agent.repo import Repository, Worktree
from agent.results import FixResult, read_fix_result
from agent.toolkit import Toolkits
from agent.tools import NotPermitted
from agent.verdict import Judged
from agent.verification import Surfaces, Verification, check

VERIFICATION_POLICY = "policy/verification"
"""Named explicitly in a fix task's slice: deciding which surfaces to run is the whole judgement."""

BRANCH_PREFIX = "agent/"
"""Every branch the agent creates starts here, which is how a run recognises its own work."""


@dataclass(frozen=True, slots=True)
class FixJob:
    """One subject's findings, the branch they get, and the task that will do the work."""

    task: PlannedTask
    judged: Judged
    """The strictest finding of the group: what the branch and the commit are named after."""
    also: tuple[Judged, ...] = field(default_factory=tuple)
    """The rest of the group — same class, same subject, one remediation."""
    branch: str = ""

    @property
    def key(self) -> str:
        return self.judged.finding.key

    @property
    def group(self) -> tuple[Judged, ...]:
        return (self.judged, *self.also)

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(item.finding.key for item in self.group)


@dataclass(slots=True)
class Fix:
    """What became of one fix job."""

    job: FixJob
    outcome: FixOutcome
    detail: str = ""
    """Why it did not ship, in the agent's words, when the outcome is not `fixed`."""
    notes: str = ""
    """What the task said it did, or why it refused. Prose, for the human reading the branch."""
    reason: Reason | None = None
    changed: tuple[str, ...] = field(default_factory=tuple)
    commit: str = ""
    branch: str = ""
    """Set only when a branch was kept, so a reader cannot mistake an abandoned one for shipped."""
    verification: Verification | None = None
    attempts: list[Attempt] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.job.task.id,
            "finding": self.job.key,
            "findings": list(self.job.keys),
            "class": self.job.judged.finding.klass.value,
            "severity": self.job.judged.finding.severity.value,
            "outcome": self.outcome.value,
            "reason": self.reason.value if self.reason else None,
            "detail": self.detail,
            "notes": self.notes,
            "branch": self.branch,
            "commit": self.commit,
            "changed": list(self.changed),
            "verification": self.verification.as_json() if self.verification else None,
            "attempts": [attempt.as_json() for attempt in self.attempts],
            "calls": self.calls,
        }


@dataclass(frozen=True, slots=True)
class Queue:
    """The fix work this run will attempt, and what it deliberately left for the next one."""

    jobs: tuple[FixJob, ...]
    deferred: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    """Findings that will not be fixed now, each with the reason, so nothing disappears silently."""

    def as_json(self) -> dict[str, Any]:
        return {
            "jobs": [
                {"id": job.task.id, "findings": list(job.keys), "branch": job.branch}
                for job in self.jobs
            ],
            "deferred": [{"finding": key, "reason": reason} for key, reason in self.deferred],
        }


def branch_for(judged: Judged) -> str:
    """A branch name derived from the class and the subject, never from the run or the advisory.

    Not from the finding key: the group on this branch is every finding about one subject, and which
    of them is the strictest changes as advisories appear and get fixed. A branch named after one of
    them would be renamed by that, and next week's run would open a second branch for the same pin.

    The readable part is trimmed because a key is long and contains characters git would rather not
    see; the digest of the exact subject is what keeps two trimmed names from colliding.
    """
    finding = judged.finding
    subject = finding.subject.key()
    readable = slug(finding.subject.package or finding.subject.path or finding.capability)
    tail = hashlib.sha256(f"{finding.klass.value}:{subject}".encode()).hexdigest()[:8]
    return f"{BRANCH_PREFIX}{finding.klass.value}/{readable[:60].strip('-')}-{tail}"


def plan_fixes(
    judged: tuple[Judged, ...],
    *,
    library: Library,
    overlay: Overlay,
    playbook: str,
    repository: Repository,
    max_open_fix_requests: int,
    proposed: tuple[str, ...] = (),
) -> Queue:
    """Which findings this run will try to fix, in the order it will ship them.

    Findings about one subject in one class become one job. Three advisories against one pin are one
    bump: the first live run of this phase opened three branches with the same edit on them and paid
    for three sessions to produce it, which is how a maintenance run turns into noise a team learns
    to ignore. The cost of grouping is that a change request can carry more than one finding, and a
    reviewer of that file or that pin wanted them together anyway.

    Order is fixed rather than convenient: class `security` first, then by severity, then by key. A
    backlog of routine bumps can otherwise crowd out an advisory simply by being reported first.
    """
    if not overlay.verification:
        # Named per finding rather than once: the run's report is where a team learns that the
        # missing `verification` section is why nothing was fixed this week.
        return Queue(
            jobs=(),
            deferred=tuple(
                (
                    item.finding.key,
                    "the overlay names no verification commands, so no fix could be shown to be "
                    "safe",
                )
                for item in _ordered(judged)
                if _unfixable(item) is None
            ),
        )
    jobs: list[FixJob] = []
    deferred: list[tuple[str, str]] = []
    # The subject's branch is stable, so a change request already carrying it is this same fix under
    # review. Reopening it as a second one is the noise this whole phase exists to avoid, and the
    # queue counts what is open rather than what this run adds: the limit is on a team's attention.
    open_now = {name for name in proposed if name.startswith(BRANCH_PREFIX)}
    room = max(0, max_open_fix_requests - len(open_now))
    for group in _grouped(judged, deferred):
        first, rest = group[0], group[1:]
        branch = branch_for(first)
        if branch in open_now:
            _defer(deferred, group, f"branch {branch} is already under review")
            continue
        if repository.has_branch(branch):
            # An abandoned branch from an earlier run in this same checkout. Leaving it alone never
            # destroys work, and pushing over it would be a force-push by another name.
            _defer(deferred, group, f"branch {branch} already exists")
            continue
        if len(jobs) >= room:
            _defer(
                deferred,
                group,
                f"the queue allows {max_open_fix_requests} open fix request(s) and "
                f"{len(open_now)} are open; the next run will take this one",
            )
            continue
        jobs.append(
            FixJob(
                task=_task(first, library=library, playbook=playbook),
                judged=first,
                also=rest,
                branch=branch,
            )
        )
    return Queue(jobs=tuple(jobs), deferred=tuple(deferred))


def _grouped(
    judged: tuple[Judged, ...], deferred: list[tuple[str, str]]
) -> tuple[tuple[Judged, ...], ...]:
    """Fixable findings, one group per class and subject, in the order the run will ship them."""
    groups: dict[tuple[str, str], list[Judged]] = {}
    for item in _ordered(judged):
        reason = _unfixable(item)
        if reason is not None:
            deferred.append((item.finding.key, reason))
            continue
        groups.setdefault((item.finding.klass.value, item.finding.subject.key()), []).append(item)
    return tuple(tuple(group) for group in groups.values())


def _defer(deferred: list[tuple[str, str]], group: tuple[Judged, ...], reason: str) -> None:
    deferred += [(item.finding.key, reason) for item in group]


def _ordered(judged: tuple[Judged, ...]) -> tuple[Judged, ...]:
    return tuple(
        sorted(
            judged,
            key=lambda item: (
                -item.finding.klass.rank,
                -item.finding.severity.rank,
                item.finding.key,
            ),
        )
    )


def _unfixable(item: Judged) -> str | None:
    """Why a finding is not a candidate for an automated fix.

    Reproducible evidence is required for the same reason it is required to block: acting on "looks
    like" is worse when the action is a change to shipping code than when it is a comment. A finding
    with no stated remedy has nothing to act on either — a fix task would be asked to invent one.
    """
    if item.reliability is not Reliability.REPRODUCIBLE:
        return "the evidence behind it is heuristic, so a code change would rest on a guess"
    if not item.finding.remediation:
        return "it states no remediation, so there is nothing to apply"
    return None


def _task(item: Judged, *, library: Library, playbook: str) -> PlannedTask:
    finding = item.finding
    subject = finding.subject.package or finding.subject.path or "code"
    tail = hashlib.sha256(finding.key.encode("utf-8")).hexdigest()[:6]
    roots: tuple[str, ...] = (playbook, finding.capability, VERIFICATION_POLICY)
    if finding.subject.ecosystem:
        roots += (finding.subject.ecosystem,)
    return PlannedTask(
        id=f"fix-{finding.klass.value}-{slug(subject)[:32].strip('-')}-{tail}",
        capability=finding.capability,
        role=Role.FIXER,
        required=False,
        ecosystem=finding.subject.ecosystem,
        scope=(finding.location.path,) if finding.location else (),
        knowledge=library.closure(roots),
    )


async def apply(
    queue: Queue,
    *,
    repository: Repository,
    roster: Roster,
    library: Library,
    notes: str,
    surfaces: Surfaces,
    trees_dir: Path,
    tasks_dir: Path,
    budget: Budget,
    toolkits: Toolkits,
    ledger: Ledger,
    run: str,
) -> list[Fix]:
    """Prepare each fix in its own worktree, then keep only the ones that verified.

    Worktrees are created and finalised one at a time while the sessions themselves overlap. Git
    serialises its own bookkeeping anyway, and two concurrent `worktree add` calls in one repository
    race over the same index for no benefit; the sessions are where the time goes.
    """
    if not queue.jobs:
        return []
    prepared: list[tuple[FixJob, Worktree]] = []
    fixes: list[Fix] = []
    for job in queue.jobs:
        try:
            prepared.append(
                (job, Worktree.create(repository, branch=job.branch, at=trees_dir / job.task.id))
            )
        except ConfigError as error:
            fixes.append(
                Fix(
                    job=job,
                    outcome=FixOutcome.UNVERIFIED,
                    reason=Reason.UNAVAILABLE,
                    detail=f"no worktree could be created: {error}",
                )
            )

    slots = asyncio.Semaphore(ledger.budget.max_parallel)
    results: list[Fix | None] = [None] * len(prepared)

    async def run_one(index: int, job: FixJob, tree: Worktree) -> None:
        async with slots:
            if not await ledger.may_start():
                results[index] = Fix(
                    job=job,
                    outcome=FixOutcome.EXHAUSTED,
                    reason=Reason.EXHAUSTED,
                    detail=ledger.exhausted_detail(),
                )
                return
            results[index] = await _one(
                job,
                tree=tree,
                roster=roster,
                library=library,
                notes=notes,
                surfaces=surfaces,
                tasks_dir=tasks_dir,
                budget=budget,
                toolkits=toolkits,
                ledger=ledger,
            )

    await asyncio.gather(*(run_one(index, job, tree) for index, (job, tree) in enumerate(prepared)))

    for (_, tree), fix in zip(prepared, results, strict=True):
        if fix is None:
            continue
        _settle(fix, tree=tree, run=run)
        fixes.append(fix)
    return fixes


async def _one(
    job: FixJob,
    *,
    tree: Worktree,
    roster: Roster,
    library: Library,
    notes: str,
    surfaces: Surfaces,
    tasks_dir: Path,
    budget: Budget,
    toolkits: Toolkits,
    ledger: Ledger,
) -> Fix:
    task = job.task
    toolkit = toolkits.for_task(task, step_limit=budget.steps, worktree=tree.path)
    instructions = role_instructions(Role.FIXER)
    knowledge = knowledge_for(library, task)
    given = _given(job, surfaces)

    def prompt_for(number: int, refused: str, result_path: Path) -> str:
        return compose(
            task=task,
            instructions=instructions,
            knowledge=knowledge,
            notes=notes,
            result_path=result_path,
            tools=tuple((tool.name, tool.description) for tool in toolkit.tools()),
            attempt=number,
            invalid_reason=refused,
            shape=FIX_RESULT_SHAPE,
            given=given,
        )

    attempted = await run_attempts(
        task,
        roster=roster,
        tasks_dir=tasks_dir,
        budget=budget,
        toolkit=toolkit,
        prompt_for=prompt_for,
        parse=read_fix_result,
    )
    for attempt in attempted.attempts:
        await ledger.record(attempt.session.usage)
    verification = check(surfaces, toolkit.calls)
    changed = tree.dirty()
    if verification.failed:
        verification = verification.against_base(
            await asyncio.to_thread(_already_broken, tree, verification.failed, toolkits, task)
        )
    fix = Fix(
        job=job,
        outcome=FixOutcome.UNVERIFIED,
        reason=Reason.UNAVAILABLE,
        attempts=attempted.attempts,
        calls=toolkit.as_json(),
        verification=verification,
        changed=changed,
    )
    result: FixResult | None = attempted.parsed
    if result is None:
        # A result that never arrived and a session that failed are different stories, and the
        # record keeps them apart: one is a model that could not answer, the other an environment.
        ran_out = _ran_out(attempted.failure)
        fix.outcome = FixOutcome.EXHAUSTED if ran_out else FixOutcome.UNVERIFIED
        if ran_out:
            fix.reason = Reason.EXHAUSTED
        else:
            fix.reason = Reason.INVALID_RESULT if attempted.rejected else Reason.UNAVAILABLE
        fix.detail = attempted.rejected or (
            attempted.failure.value if attempted.failure else "no result was written"
        )
        return fix
    fix.notes = result.notes
    fix.reason = result.reason
    fix.outcome = result.outcome
    if result.outcome is FixOutcome.FIXED:
        fix.outcome, fix.detail = _shippable(fix)
    elif result.outcome is FixOutcome.REFUSED:
        fix.detail = result.notes
    return fix


def _already_broken(
    tree: Worktree,
    failed: tuple[tuple[str, ...], ...],
    toolkits: Toolkits,
    task: PlannedTask,
) -> tuple[tuple[str, ...], ...]:
    """Which of these commands fail on the unchanged head, with the change taken away.

    A repository whose lint or test surface is red before anything is touched would otherwise make
    every fix look like the fix's fault, and a team would read a week of refusals as "the agent
    cannot bump our dependencies". The question is cheap to answer honestly: the failing commands
    are re-run in the same checkout after it is restored. Restoring is safe because a failed
    verification has already decided this branch will not ship, and reusing the checkout keeps what
    the first run built, which is most of what a second one would pay for.
    """
    tools = toolkits.session.for_task(f"{task.id}-base", root=tree.path)
    try:
        tree.restore()
    except ConfigError:
        return ()
    inherited: list[tuple[str, ...]] = []
    for command in failed:
        try:
            if not tools.commands.run(command).succeeded:
                inherited.append(command)
        except NotPermitted, FileNotFoundError, ValueError:
            # A command the agent may not run itself says nothing about the base, and it is not the
            # fix's fault either. Leaving it out of `pre_existing` keeps the failure attributed to
            # the change, which is the conservative half of the guess.
            continue
    return tuple(inherited)


def _shippable(fix: Fix) -> tuple[FixOutcome, str]:
    """Whether a task's `fixed` survives contact with the record.

    Both checks catch the same class of mistake from opposite ends: a session that concluded it was
    done without changing anything, and one that changed something without proving it safe. Either
    way the branch would look ready to merge, which is the most expensive kind of wrong here.
    """
    if not fix.changed:
        return FixOutcome.REFUSED, "reported a fix, but the worktree is unchanged"
    if fix.verification is None or not fix.verification.passed:
        detail = fix.verification.detail if fix.verification else "verification was not checked"
        return FixOutcome.REFUSED, f"reported a fix, but {detail}"
    return FixOutcome.FIXED, ""


def _settle(fix: Fix, *, tree: Worktree, run: str) -> None:
    """Commit a fix that survived the checks; throw the rest away, branch included."""
    if fix.outcome is not FixOutcome.FIXED:
        tree.discard(keep_branch=False)
        return
    finding = fix.job.judged.finding
    surfaces = ", ".join(fix.verification.verified) if fix.verification else ""
    message = "\n".join(
        [
            f"{finding.klass.value}: {finding.summary}",
            "",
            fix.notes,
            "",
            f"Finding{'s' if fix.job.also else ''}: {', '.join(fix.job.keys)}",
            f"Verified: {surfaces}" if surfaces else "",
            f"Prepared by ai-devsecops-agent in run {run}.",
        ]
    )
    fix.commit = tree.commit(message.replace("\n\n\n", "\n\n"))
    fix.branch = tree.branch
    tree.discard(keep_branch=True)


def _given(job: FixJob, surfaces: Surfaces) -> tuple[str, ...]:
    """The facts a fix task is handed, instead of leaving it to find them.

    The verification commands are handed over rather than left to be discovered. They live in the
    product's overlay, which the task could read, and a session that reads it wrong runs a check
    nobody asked for or skips one that mattered — for something the agent already knows exactly.
    """
    finding = job.judged.finding
    lines = [
        f"- Finding to fix: `{finding.key}`",
        f"- Class: `{finding.klass.value}`, severity `{finding.severity.value}`",
        f"- Problem: {finding.summary}",
        f"- Why it matters: {finding.rationale}",
        f"- Suggested remediation: {finding.remediation}",
    ]
    if finding.location:
        where = finding.location.path
        if finding.location.line:
            where += f":{finding.location.line}"
        lines.append(f"- Reported at: `{where}`")
    if job.also:
        lines.append(
            f"- Also on this subject, to fix in the same change ({len(job.also)}): "
            + "; ".join(f"`{item.finding.key}` — {item.finding.remediation}" for item in job.also)
        )
    for surface, commands in sorted(surfaces.items()):
        lines.append(
            f"- Verification surface `{surface}`, in order: "
            + "; ".join(f"`{' '.join(command)}`" for command in commands)
        )
    lines.append(
        "- Run every command of at least one surface the change affects, exactly as written. A "
        "surface counts as verified only in full: a partly run surface ships nothing, and so does "
        "a single failing command."
    )
    lines.append(
        "- You are working in an isolated copy of the repository. Nothing you do here touches the "
        "branch under maintenance, and the agent commits what you leave behind."
    )
    return tuple(lines)


def _ran_out(failure: Failure | None) -> bool:
    return failure in {Failure.TIMED_OUT, Failure.EXHAUSTED}
