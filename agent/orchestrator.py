"""One run, end to end.

The shape is deliberate: plan deterministically, let subagents judge, then decide deterministically
again. Everything a model produces passes through validation before it can influence the verdict,
and anything that fails validation is recorded as "did not run" rather than as "found nothing".
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

from agent import __version__
from agent.absence import Absences
from agent.answer import Aftermath, Answered, answer, deliver, status_for
from agent.backends.port import Backend, Budget
from agent.backends.select import Roster
from agent.budget import Ledger, RunBudget
from agent.config import Config, Models
from agent.containment import Checkout
from agent.coverage import Coverage, previous
from agent.domain import AnswerOutcome, FixOutcome, Intent, Plan, Role, Trigger
from agent.errors import ConfigError, ExitCode
from agent.escalate import Escalation, weigh
from agent.executor import Executed, execute
from agent.findings import Finding, merge
from agent.intent import Course, Read, classify, narrow
from agent.issues import LABEL, Tracking, track_findings
from agent.library import Library
from agent.lifecycle import notice_open_prs, reclaim_abandoned
from agent.manifest import Manifest
from agent.overlay import MAINTENANCE, REVIEW, VALUES_FILE, Overlay, digest_on_disk, within
from agent.patch import prepare
from agent.planner import ChangeSet, plan_run
from agent.policy import BlockingRules
from agent.posture import posture_for
from agent.propose import propose_fixes
from agent.publish import publish_review
from agent.reconcile import caution_for, concluded
from agent.remediate import BRANCH_PREFIX, Fix, Queue, apply, plan_fixes
from agent.repo import ChangeView, Repository
from agent.report import render
from agent.scm import GitHub, Identity, Issue, Platform, ScmError
from agent.scm.port import Proposal
from agent.session import Session
from agent.state import Memory
from agent.storage import FactCache
from agent.toolkit import Toolkits
from agent.tools import grant
from agent.unlock import Approval, granted, refuse_unlock
from agent.verdict import TaskOutcome, Verdict, decide, judge
from agent.wake import Wake, Woken, admit

PLANNED = "planned"
DECLINED = "declined"
ANSWERED = "answered"
"""A run woken by a comment that replied to it. Not a verdict: nothing was judged and nothing was
changed, so it is neither a pass nor a refusal, and it exits successfully either way."""
REPORT = "report.md"


@dataclass(frozen=True, slots=True)
class Request:
    trigger: Trigger
    repository: Path
    library_path: Path
    overlay_path: Path
    run_dir: Path
    config_dir: Path | None = None
    base: str | None = None
    change: int | None = None
    wake: Wake | None = None
    """Somebody's comment, when one started this run: whose it was, and which conversation. Checked
    against the platform before anything is spent — an agent that answers its own comment answers it
    forever, and an account with no write access is not who a budget answers to."""
    outside: bool = False
    """Treat the head as code from outside this repository, whatever the platform says. Can only
    restrain the run, which is why it needs no counterpart: there is no flag for asserting that
    somebody else's code is safe to execute."""
    plan_only: bool = False
    dry_run: bool = False
    """Analyse and say what would be fixed, without creating a worktree, a branch or a commit."""
    publish: bool = False
    """Post the decision on the hosting platform. Off by default, so a run on a laptop reports to
    the person who started it, and a run in CI says out loud that it means to comment."""
    use_cache: bool = True
    only: tuple[str, ...] = ()
    """Task identifiers to run, for development. A run so narrowed says so in its manifest and in
    its report: its verdict describes the tasks it ran and cannot stand for the whole playbook."""


@dataclass(frozen=True, slots=True)
class RunRecord:
    manifest: Manifest
    manifest_path: Path
    exit_code: ExitCode
    verdict: Verdict | None = None
    report: str = ""


def _overlay_for(
    request: Request,
    *,
    repository: Repository,
    base: str | None,
    library: Library,
    config: Config,
) -> tuple[Overlay, str]:
    """The overlay this run obeys, and what the report must say when it is not the visible one.

    A review reads the overlay from the merge base rather than from the checkout. The overlay is
    where the meaning of a finding is settled here — the quarantine window, the local exceptions,
    which ecosystems are examined at all — and `NOTES.md` enters every task's prompt. Read from the
    checkout, all of that would be editable by the change being examined: one commit could set the
    quarantine to zero, drop the ecosystem whose dependency it bumps, or add a line to the notes
    telling the model what to conclude, and the run would carry out those instructions while
    reporting a pass. The change is still reviewed in full; it just does not get to write the rules
    it is judged by.

    Three cases legitimately fall back to the checkout, and all three say so. An overlay kept
    outside the repository is not part of any change, so there is nothing to protect it from. A base
    with no overlay is a change introducing one, and there is no earlier version to prefer. And a
    base whose overlay this agent cannot read is a change that migrates it: refusing there would
    mean an overlay's shape could never change again, because every such change would need a run
    that already understood the new shape. Nothing is given away — the base is the default branch,
    which a change under review cannot rewrite.
    """
    load = partial(
        Overlay.load,
        request.overlay_path,
        library=library,
        notes_limit=config.notes_limit,
    )
    if base is None:
        return load(), ""

    unreadable = ""
    try:
        committed = Overlay.at(
            repository,
            repository.merge_base(base),
            path=request.overlay_path,
            library=library,
            notes_limit=config.notes_limit,
        )
    except ConfigError as error:
        committed, unreadable = None, str(error)
    if committed is None and unreadable:
        return load(), (
            "this agent cannot read the overlay on the base of this change, so the run obeyed the "
            f"copy the change brings ({unreadable}). A migration of the overlay takes effect on "
            "merge, and until then the change sets the rules it is judged by"
        )
    if committed is None:
        if not within(repository.path, request.overlay_path):
            return load(), ""
        return load(), (
            "the base of this change has no overlay, so this run read the one the change brings: "
            "the rules it was judged by are the rules it proposes"
        )
    if digest_on_disk(request.overlay_path) != committed.digest:
        return committed, (
            f"this change edits the agent overlay in `{request.overlay_path.name}`; the run obeyed "
            f"the base version ({committed.digest[:19]}), because a change does not set the rules "
            "it is judged by. The edit takes effect once it is merged"
        )
    return committed, ""


def _narrow(plan: Plan, only: tuple[str, ...]) -> Plan:
    """Keep the named tasks, and refuse a name that matches nothing.

    A typo that silently ran nothing would produce a run that looks like a pass, which is the one
    outcome this project cannot afford to make easy.
    """
    known = {task.id for task in plan.tasks}
    unknown = [name for name in only if name not in known]
    if unknown:
        available = ", ".join(sorted(known))
        raise ConfigError(f"no planned task named {', '.join(unknown)}. This plan has: {available}")
    kept = tuple(task for task in plan.tasks if task.id in set(only))
    dropped = tuple(
        (task.capability, "narrowed by --only") for task in plan.tasks if task not in kept
    )
    return replace(plan, tasks=kept, skipped=plan.skipped + dropped)


def run(
    request: Request, *, backend: Backend | None = None, platform: Platform | None = None
) -> RunRecord:
    config = Config.load(request.config_dir)
    scenario = config.scenario_for(request.trigger)

    library = Library.load(request.library_path, agent_version=__version__)
    library.check_pinned(version=config.pin.version, digest=config.pin.digest)
    # Read before anything runs: a blocking table the agent cannot parse must stop the run, not be
    # discovered when the first finding needs judging.
    rules = BlockingRules.read(library)

    if request.publish and request.change is None and not request.trigger.is_maintenance:
        raise ConfigError(
            "--publish needs --change: there is no conversation to publish a review to without one"
        )

    repository = Repository.open(request.repository)
    change: ChangeSet | None = None
    if request.trigger.is_maintenance:
        base = None
    else:
        base = request.base or "main"
        change = ChangeSet(paths=repository.changed_paths(base))

    overlay, notice = _overlay_for(
        request, repository=repository, base=base, library=library, config=config
    )
    # What this kind of run is configured with: its models and its ceilings, from the one block that
    # describes it. The agent names no model anywhere, so the overlay is the only place they exist.
    settings = overlay.settings_for(request.trigger)
    kind = MAINTENANCE if request.trigger.is_maintenance else REVIEW
    models = Models.chosen(
        settings.models,
        options=config.backend_options,
        where=f"{overlay.path / VALUES_FILE} ({kind}.models)",
    )

    plan = plan_run(
        scenario=scenario,
        trigger=request.trigger,
        library=library,
        overlay=overlay,
        change=change,
    )

    if request.only:
        plan = _narrow(plan, request.only)

    manifest = Manifest.start(
        agent_version=__version__,
        plan=plan,
        repository=repository.path,
        head=repository.head,
        change=request.change,
        library={
            "path": str(library.root),
            "version": library.identity.version,
            "contract_version": library.identity.contract_version,
            "digest": library.digest,
            "pinned": config.pin.version is not None or config.pin.digest is not None,
        },
        overlay={
            "path": str(overlay.path),
            "origin": overlay.origin,
            "digest": overlay.digest,
            "ecosystems": list(overlay.ecosystems),
            "quarantine_days": overlay.quarantine_days,
            "kind": kind,
            "queue": {
                "max_open_fix_requests": overlay.queue.max_open_fix_requests,
                "max_new_issues_per_run": overlay.queue.max_new_issues_per_run,
            },
        },
    )
    manifest.warnings.extend(overlay.warnings)
    if notice:
        manifest.warnings.append(notice)
    if request.only:
        manifest.partial = list(request.only)
        manifest.warnings.append(
            "this run was narrowed to "
            + ", ".join(request.only)
            + ": its verdict covers those tasks and nothing else"
        )
    if not manifest.library["pinned"]:
        manifest.warnings.append(
            "the library is not pinned: this run cannot prove which knowledge it used"
        )
    if base is not None:
        manifest.library["base"] = base
    manifest.policy = {
        "source": rules.source,
        "blocks": sorted(f"{klass}/{severity}" for klass, severity in rules.blocking),
        "forbidden_state_blocks": rules.forbidden_state_blocks,
    }

    # The ceiling is checked before any task starts: a refused binary discovered mid-review would
    # look like a broken tool, and the run would spend its budget finding that out.
    grants = grant(library=library, ecosystems=overlay.ecosystems, ceiling=config.ceiling)
    manifest.grants = {"binaries": sorted(grants.binaries), "hosts": sorted(grants.hosts)}

    # Every role the plan needs must have a backend that can do the role's work, settled before
    # anything is spent: an unbound `fixer`, or one bound to an adapter that cannot change files, is
    # a mistake in a file. Found mid-run it would mean a maintenance pass that opened issues and
    # then reported it had fixed nothing.
    # A maintenance run that is allowed to fix needs a `fixer` binding before it starts, not after
    # the analysis it just paid for: the alternative is a pass that reports findings and silently
    # never ships anything, which looks exactly like a week with nothing to fix.
    # A wake needs both of its roles bound before it reads anything, even though only one of them
    # will run: which one depends on what the comment turns out to ask for, and finding out that the
    # answering model is unbound *after* classifying would leave a person with silence.
    may_fix = request.trigger.is_maintenance and not request.dry_run and not request.plan_only
    needed = {task.role for task in plan.tasks} | ({Role.FIXER} if may_fix else set())
    # A patch offered in a review thread is a fixing session, so it needs that role bound too. Read
    # rather than required: a product that binds no fixer for its reviews has decided its agent
    # explains instead of proposing, and answering in prose is a course the table already has.
    patching = request.trigger is Trigger.COMMENT_ON_CHANGE and Role.FIXER in models.bindings
    if request.wake is not None:
        needed |= {Role.INTENT, Role.WRITER} | ({Role.FIXER} if patching else set())
    manifest.roles = [models.for_role(role).as_json() for role in sorted(needed)]

    # Recorded before the plan-only exit: seeing what a trigger would be allowed to spend is half
    # the reason to ask for a plan without running one.
    spend = settings.limits
    manifest.budget = {
        "task_seconds": spend.seconds_per_task,
        "task_steps": config.steps_limit,
        "max_parallel": spend.tasks_at_once,
        "run_tokens": spend.tokens_per_run,
        "kind": kind,
    }

    if request.plan_only:
        manifest.finish(PLANNED)
        return RunRecord(manifest, manifest.write(request.run_dir), ExitCode.OK)

    # Resolved before anything is spent, because two questions depend on it: whether this run should
    # exist at all, and which branches are already under review. A run that guesses the second opens
    # a change request somebody is already reviewing; one that skips the first answers itself.
    speaker, speaks = _resolve(request, platform=platform, repository=repository)
    if speaks:
        manifest.warnings.append(
            f"nothing was published: {speaks}"
            if request.publish
            else f"the platform could not be reached: {speaks}"
        )

    # Whose code this is, and therefore whether a single command may be run over it. Settled here
    # because everything downstream depends on it: the tools a session is offered, whether a fix is
    # prepared, and what the report has to admit it did not establish.
    posture, restrained = posture_for(
        change=request.change, platform=speaker, forced=request.outside
    )
    manifest.posture = posture.as_json()
    if restrained:
        manifest.warnings.append(restrained)
    if not posture.executes:
        # `manifest.roles` above still lists a fixer when one is bound: that is what the run was
        # configured with, and the posture record is what says why it never ran.
        may_fix, patching = False, False

    woken: Woken | None = None
    if request.wake is not None:
        manifest.wake = request.wake.as_json()
        admitted = admit(request.wake, platform=speaker, identity=_speaks_as(speaker))
        if isinstance(admitted, str):
            # Recorded as a run, deliberately: "the agent declined to answer its own comment" and
            # "it does not take orders from an account without write access" are the properties
            # these checks exist for, and a run that left no trace could not demonstrate them.
            manifest.wake |= {"course": "none", "detail": admitted}
            manifest.warnings.append(admitted)
            manifest.finish(DECLINED)
            return RunRecord(manifest, manifest.write(request.run_dir), ExitCode.OK)
        woken = admitted
        manifest.wake = woken.as_json()

    # Only a run on the default branch may write facts. A review runs on code a stranger proposed,
    # so it reads the cache and never feeds it.
    cache = FactCache(_cache_root(request, config), writable=request.trigger.is_maintenance)
    run_directory = request.run_dir / manifest.run_id
    # Whether the platform's API was read anonymously belongs in the record: it decides which rate
    # limit applied, and therefore whether a fact this run could not establish was unobtainable or
    # merely the sixty-first request of the hour.
    reading = speaker.reading_token() if speaker is not None else ""
    manifest.grants |= {"platform_api": "authenticated" if reading else "anonymous"}
    session = Session(
        repository=repository.path,
        grants=grants,
        cache=cache,
        change=ChangeView.of(repository, base) if base is not None else None,
        scratch_root=run_directory / "scratch",
        never_send=config.never_send,
        reading_token=reading,
        tool_cache=_tool_cache(request, config),
    )

    owned = backend is None
    roster = Roster.of(backend) if backend is not None else Roster(models)
    if owned:
        # Created up front, so an SDK this machine has not installed is an error before the first
        # task rather than one task's failure among several.
        roster.prepare(needed)
    # One clock for the whole run: quarantine arithmetic that moved between two tasks of the same
    # run would make the verdict depend on how long the earlier tasks took.
    toolkits = Toolkits(
        session=session,
        now=datetime.now(UTC),
        quarantine_days=overlay.quarantine_days,
        executes=posture.executes,
    )
    # One event loop for execution and shutdown alike. A backend that holds a subprocess cannot be
    # closed from a second loop: the process was awaited in the first one, and closing it elsewhere
    # fails with a future attached to another loop.
    ledger = Ledger(RunBudget(max_parallel=spend.tasks_at_once, tokens=spend.tokens_per_run))

    proposed: tuple[str, ...] = ()
    open_proposals: dict[str, Proposal] = {}
    tracked: tuple[Issue, ...] | None = None
    approvals: dict[str, Approval] = {}
    if may_fix and request.publish:
        if speaker is None:
            may_fix = False
        else:
            try:
                listed = speaker.proposals(prefix=BRANCH_PREFIX)
                open_proposals = {item.head: item for item in listed}
                proposed = tuple(open_proposals)
            except ScmError as error:
                may_fix = False
                manifest.warnings.append(
                    f"no fix branches were prepared: the open change requests could not be read "
                    f"({error}), and preparing branches blind would duplicate ones already open"
                )
            # Which holds a person has released, read before anything is planned and reused when
            # the issues are reconciled at the end. A run that cannot read them ships nothing that
            # waits for approval, which is the safe direction: the finding is reported as waiting
            # for one more week rather than changed on nobody's say-so.
            try:
                tracked = speaker.issues(label=LABEL)
                approvals = granted(tracked)
            except ScmError as error:
                manifest.warnings.append(
                    f"which findings a person has approved could not be read ({error}), so "
                    "anything waiting for approval was left waiting"
                )

    # Watched from here, so that anything already uncommitted when the run started is left alone and
    # only what a session does is undone.
    facts = _cache_root(request, config)
    mine = (run_directory, _tool_cache(request, config), *((facts,) if facts else ()))
    checkout = Checkout.of(repository.path, mine=mine)
    performed = asyncio.run(
        _conduct(
            plan,
            woken=woken,
            manifest=manifest,
            roster=roster,
            library=library,
            overlay=overlay,
            session=session,
            rules=rules,
            repository=repository,
            run_directory=run_directory,
            budget=Budget(seconds=spend.seconds_per_task, steps=config.steps_limit),
            toolkits=toolkits,
            ledger=ledger,
            may_fix=may_fix,
            patching=patching,
            restraint=posture.aside,
            proposed=proposed,
            open_proposals=open_proposals,
            approvals=approvals,
            checkout=checkout,
            platform=speaker if may_fix else None,
            close=owned,
        )
    )
    if checkout.strayed:
        manifest.warnings.append(
            "a session wrote into the repository's checkout instead of the worktree it was given: "
            + ", ".join(stray.path for stray in checkout.strayed)
            + ". The checkout was put back and the results of those attempts were refused; the "
            "changes are kept under the run directory. A backend whose own file tools are not "
            "confined does this, so check that the sandbox is on for the backend in use"
        )
    manifest.budget["spend"] = ledger.spend.as_json()
    if performed.read is not None:
        manifest.wake |= performed.read.as_json()
    if performed.approval is not None:
        manifest.wake |= {"unlocked": performed.approval.as_json()}
    _spent(manifest, performed)
    if performed.verdict is None:
        # A wake that answered or found nothing to do. There is no verdict, and inventing one would
        # mean a question about a finding could pass or block a branch nobody proposed.
        return _wrote_back(
            request,
            manifest=manifest,
            performed=performed,
            woken=woken,
            platform=speaker,
            cache=cache,
            session=session,
        )
    verdict = performed.verdict
    executed, fixes, queue = performed.executed, performed.fixes, performed.queue
    if queue is not None:
        manifest.fixes = [fix.as_json() for fix in fixes]
        manifest.remediation = queue.as_json()

    outcomes = tuple(item.outcome for item in executed)
    covered = Coverage.of(session.evidence)
    manifest.coverage = covered.as_json()
    # Before the report rather than with the rest of the publishing, because a run that got through
    # less of the tree than the last one has to say so in the text a person reads, and the report is
    # rendered here. Reading the memory is a git command over a ref; writing it still happens after
    # the issues, where it belongs.
    escalations, remembered, document, absences, shortfall = _recall(
        request,
        config=config,
        repository=repository,
        outcomes=outcomes,
        coverage=covered,
        run=manifest.run_id,
        woken=woken,
    )
    manifest.warnings.extend(shortfall)

    report = render(
        verdict,
        trigger=request.trigger,
        tasks=outcomes,
        library_version=library.identity.version,
        unverified_facts=len(session.evidence.unverified()),
        fixes=tuple(fixes),
        notice=notice,
        restraint=posture.restraint,
        approvals=approvals,
        shortfall=shortfall,
    )

    if request.publish and speaker is not None:
        # Outside the event loop, because talking to the platform is a subprocess away rather than
        # a coroutine.
        _announce(
            request,
            manifest=manifest,
            platform=speaker,
            repository=repository,
            verdict=verdict,
            report=report,
            outcomes=outcomes,
            change=session.change,
            fixes=tuple(fixes),
            queue=performed.queue,
            new_issues=overlay.queue.max_new_issues_per_run,
            tracked=tracked,
            approvals=approvals,
            surfaces=overlay.verification,
            escalations=escalations,
            memory=remembered,
            document=document,
            absences=absences,
            woken=woken,
            asked=performed.read.classification.gist
            if performed.read and performed.read.classification
            else "",
        )
    elif request.publish:
        manifest.actions = {"failure": speaks}

    manifest.cache = cache.stats.as_json() | {"writable": cache.writable}
    manifest.finish(verdict.result.value)
    manifest_path = manifest.write(request.run_dir)
    session.evidence.write(manifest_path.parent / "evidence.jsonl")
    (manifest_path.parent / REPORT).write_text(report, encoding="utf-8")
    return RunRecord(manifest, manifest_path, verdict.result.exit_code, verdict, report)


@dataclass(slots=True)
class Performed:
    """What the run's one event loop produced. A verdict, or the reason there is none."""

    executed: list[Executed] = field(default_factory=list)
    verdict: Verdict | None = None
    fixes: list[Fix] = field(default_factory=list)
    queue: Queue | None = None
    reclaimed: list[str] = field(default_factory=list)
    """Abandoned fix branches removed before this run prepared replacements."""
    read: Read | None = None
    """How a comment was read, when one woke this run."""
    answered: Answered | None = None
    approval: Approval | None = None
    """The hold a person released in this run, when their comment did that."""
    halted: str = ""
    """Why the run stopped after reading the comment, when it did."""


async def _conduct(
    plan: Plan,
    *,
    woken: Woken | None,
    manifest: Manifest,
    roster: Roster,
    library: Library,
    overlay: Overlay,
    session: Session,
    rules: BlockingRules,
    repository: Repository,
    run_directory: Path,
    budget: Budget,
    toolkits: Toolkits,
    ledger: Ledger,
    may_fix: bool,
    patching: bool,
    restraint: str,
    proposed: tuple[str, ...],
    open_proposals: dict[str, Proposal],
    approvals: dict[str, Approval],
    checkout: Checkout,
    platform: Platform | None,
    close: bool,
) -> Performed:
    """Everything that needs a model, in one event loop, including the shutdown.

    A backend holding a subprocess cannot be closed from a second loop: the process was awaited in
    the first one. That is why reading a comment, answering it, analysing, deciding and fixing all
    happen inside this one call rather than in several `asyncio.run` invocations.
    """
    performed = Performed()
    try:
        if woken is not None:
            plan = await _episode(
                performed,
                plan,
                woken=woken,
                manifest=manifest,
                roster=roster,
                library=library,
                overlay=overlay,
                repository=repository,
                run_directory=run_directory,
                budget=budget,
                toolkits=toolkits,
                ledger=ledger,
                patching=patching,
                restraint=restraint,
                approvals=approvals,
                checkout=checkout,
            )
            if performed.halted or performed.answered is not None:
                return performed
        await _perform(
            performed,
            plan,
            manifest=manifest,
            roster=roster,
            library=library,
            overlay=overlay,
            session=session,
            rules=rules,
            repository=repository,
            run_directory=run_directory,
            budget=budget,
            toolkits=toolkits,
            ledger=ledger,
            may_fix=may_fix,
            proposed=proposed,
            open_proposals=open_proposals,
            approvals=approvals,
            checkout=checkout,
            platform=platform,
        )
        return performed
    finally:
        if close:
            await roster.close()


async def _episode(
    performed: Performed,
    plan: Plan,
    *,
    woken: Woken,
    manifest: Manifest,
    roster: Roster,
    library: Library,
    overlay: Overlay,
    repository: Repository,
    run_directory: Path,
    budget: Budget,
    toolkits: Toolkits,
    ledger: Ledger,
    patching: bool,
    restraint: str,
    approvals: dict[str, Approval],
    checkout: Checkout,
) -> Plan:
    """Read the comment, then do the one thing the table says it asks for.

    Four outcomes, and each of them is cheaper than a full run — which is the point. Somebody who
    comments on one issue is asking about one thing, and a weekly sweep in reply would make a
    question the most expensive way to ask one.
    """
    performed.read = await classify(
        woken,
        roster=roster,
        tasks_dir=run_directory / "tasks",
        budget=budget,
        toolkits=toolkits,
        ledger=ledger,
        patching=patching,
    )
    match performed.read.course:
        case Course.IGNORE:
            performed.halted = (
                "nothing was written: this comment asks for nothing the agent does"
                + (
                    f" ({performed.read.classification.gist})"
                    if performed.read.classification
                    else ""
                )
            )
        case Course.ANSWER:
            performed.answered = await answer(
                woken,
                roster=roster,
                library=library,
                playbook=plan.playbook,
                notes=overlay.notes,
                tasks_dir=run_directory / "tasks",
                budget=budget,
                toolkits=toolkits,
                ledger=ledger,
                restraint=restraint,
            )
        case Course.PATCH:
            performed.answered = await prepare(
                woken,
                repository=repository,
                roster=roster,
                library=library,
                playbook=plan.playbook,
                notes=overlay.notes,
                surfaces=overlay.verification,
                trees_dir=run_directory / "patch",
                tasks_dir=run_directory / "tasks",
                budget=budget,
                toolkits=toolkits,
                ledger=ledger,
                run=manifest.run_id,
                checkout=checkout,
            )
        case Course.RECHECK:
            refusal = refuse_unlock(woken.key, body=woken.remark)
            classified = performed.read.classification if performed.read else None
            if (
                refusal is not None
                and classified is not None
                and classified.intent is Intent.UNLOCK
            ):
                # Routine quarantine: do not stamp, do not re-check, answer the person now.
                performed.halted = refusal
                performed.answered = Answered(
                    outcome=AnswerOutcome.ANSWERED,
                    reply=refusal,
                    task="wake-unlock-refused",
                )
                return plan
            _release(performed, woken, approvals=approvals, when=toolkits.now)
            narrowed, why = narrow(plan, woken.key)
            if why:
                performed.halted = f"nothing was re-established: {why}"
            else:
                # The record shows the tasks that ran, with the rest listed as skipped and why. A
                # manifest carrying the full plan would claim a weekly sweep this run never did.
                manifest.replan(narrowed)
                return narrowed
    return plan


def _release(
    performed: Performed,
    woken: Woken,
    *,
    approvals: dict[str, Approval],
    when: datetime,
) -> None:
    """Record the permission a comment granted, so the rest of this run can act on it.

    Only an `unlock`, and only on an issue: a hold is a thing an issue tracks, and a comment in a
    review thread has none to release. Everything that made this safe has already happened — the
    platform said this account may write here, the comment was confirmed to be theirs, and a
    classification the model was unsure about took the answering course instead of this one.

    The grant is a fact from here on. It decides what the fix queue may take, and it is written into
    the issue body when the issues are reconciled, which is what stops the next run asking again.
    """
    classified = performed.read.classification if performed.read else None
    if classified is None or classified.intent is not Intent.UNLOCK or woken.issue is None:
        return
    performed.approval = Approval(
        by=woken.said.author, comment=woken.wake.comment, at=when.date().isoformat()
    )
    approvals[woken.key] = performed.approval


async def _perform(
    performed: Performed,
    plan: Plan,
    *,
    manifest: Manifest,
    roster: Roster,
    library: Library,
    overlay: Overlay,
    session: Session,
    rules: BlockingRules,
    repository: Repository,
    run_directory: Path,
    budget: Budget,
    toolkits: Toolkits,
    ledger: Ledger,
    may_fix: bool,
    proposed: tuple[str, ...],
    open_proposals: dict[str, Proposal],
    approvals: dict[str, Approval],
    checkout: Checkout,
    platform: Platform | None,
) -> None:
    """Analyse, decide, and then — on a maintenance run — fix what the decision allows."""
    performed.executed = await execute(
        plan,
        roster=roster,
        library=library,
        notes=overlay.notes,
        evidence=session.evidence,
        tasks_dir=run_directory / "tasks",
        budget=budget,
        toolkits=toolkits,
        ledger=ledger,
        checkout=checkout,
    )
    performed.verdict = _conclude(manifest, performed.executed, rules=rules, session=session)
    if not may_fix:
        return
    if platform is not None:
        reclaimed = reclaim_abandoned(
            platform,
            repository,
            judged=performed.verdict.judged,
            open_heads=set(open_proposals) | set(proposed),
            approvals=approvals,
            overlay=overlay,
            run=manifest.run_id,
        )
        performed.reclaimed = list(reclaimed.branches)
        if reclaimed.branches or reclaimed.noted or reclaimed.failure:
            manifest.actions = {
                **(manifest.actions if isinstance(manifest.actions, dict) else {}),
                "reclaim": reclaimed.as_json(),
            }
        if reclaimed.failure:
            manifest.warnings.append(
                f"an abandoned fix branch could not be fully reclaimed ({reclaimed.failure}): "
                "subjects still blocked by that tip were left for a later run"
            )
    performed.queue = plan_fixes(
        performed.verdict.judged,
        library=library,
        overlay=overlay,
        playbook=plan.playbook,
        repository=repository,
        max_open_fix_requests=overlay.queue.max_open_fix_requests,
        proposed=proposed,
        open_proposals=open_proposals,
        approvals=approvals,
    )
    performed.fixes = await apply(
        performed.queue,
        repository=repository,
        roster=roster,
        library=library,
        notes=overlay.notes,
        surfaces=overlay.verification,
        trees_dir=run_directory / "fixes",
        tasks_dir=run_directory / "tasks",
        budget=budget,
        toolkits=toolkits,
        ledger=ledger,
        run=manifest.run_id,
        checkout=checkout,
    )
    _account(manifest, performed.fixes)


def _wrote_back(
    request: Request,
    *,
    manifest: Manifest,
    performed: Performed,
    woken: Woken | None,
    platform: Platform | None,
    cache: FactCache,
    session: Session,
) -> RunRecord:
    """Finish a run that answered a person instead of judging anything, and post the answer.

    Kept apart from the verdict path rather than folded into it. This run analysed nothing and
    changed nothing, so it has no stance to publish and no threads to reconcile; the one thing it
    produces is a comment, and treating it as a review would put a pass or a refusal on a change
    request over somebody's question.
    """
    written = performed.answered
    if written is not None and woken is not None:
        if request.publish and platform is not None:
            deliver(platform, woken, written, run=manifest.run_id)
            if written.failure:
                manifest.warnings.append(f"the answer was not posted: {written.failure}")
        else:
            manifest.warnings.append(
                "the answer was written but not posted: this run was not asked to publish. It is "
                "in the run's record and in the report"
            )
        manifest.actions = {"answer": written.as_json()}
    if performed.halted:
        manifest.warnings.append(performed.halted)
    manifest.cache = cache.stats.as_json() | {"writable": cache.writable}
    manifest.finish(ANSWERED if written is not None else DECLINED)
    manifest_path = manifest.write(request.run_dir)
    session.evidence.write(manifest_path.parent / "evidence.jsonl")
    report = _wake_report(manifest, performed=performed, woken=woken)
    (manifest_path.parent / REPORT).write_text(report, encoding="utf-8")
    return RunRecord(manifest, manifest_path, ExitCode.OK, None, report)


def _wake_report(manifest: Manifest, *, performed: Performed, woken: Woken | None) -> str:
    """The report for a wake: what was asked, how it was read, and what was said back."""
    read = performed.read
    lines = [
        f"# {manifest.playbook} — woken by a comment",
        "",
        f"- Run `{manifest.run_id}`, agent {manifest.agent_version}",
    ]
    if woken is not None:
        lines += [
            f"- {woken.said.author} commented on {woken.wake.where}",
            f"- About finding `{woken.key}`",
        ]
    if read is not None:
        detail = f" ({read.detail})" if read.detail else ""
        how = (
            f"{read.classification.intent.value}, "
            + ("confident" if read.classification.confident else "unsure")
            if read.classification
            else "not classified"
        )
        lines.append(f"- Read as: {how} → course `{read.course.value}`{detail}")
        if read.classification:
            lines.append(f"- What was asked: {read.classification.gist}")
    if performed.halted:
        lines += ["", performed.halted]
    if performed.answered is not None:
        prepared = performed.answered.prepared
        if prepared:
            changed = ", ".join(f"`{path}`" for path in prepared.get("changed") or ()) or "nothing"
            verification = prepared.get("verification") or {}
            lines.append(
                f"- Prepared a change ({prepared.get('form')}) in {changed}, "
                + ("verified" if verification.get("passed") else "not verified")
            )
        lines += ["", "## What was said", "", performed.answered.reply or "(nothing)"]
    return "\n".join(lines) + "\n"


def _resolve(
    request: Request, *, platform: Platform | None, repository: Repository
) -> tuple[Platform | None, str]:
    """The platform this run will speak to, or why it cannot speak at all.

    A missing client, an unreadable remote or an absent credential costs a warning rather than the
    run: by the time any of it matters the analysis is the expensive part, and it is already done.

    A wake needs the platform to read rather than to write, so it resolves one even without
    `--publish`: the comment that started the run, and whether its author may write here, are both
    facts only the platform has.

    So does any run that names a change, for one fact: which repository the head lives in. Nothing
    in a checkout says whether it came from a fork — the branch looks the same either way — and that
    answer decides whether this run may execute a single command.

    Every other run resolves one too, for the credential its reads of the platform's API may carry:
    anonymous access is rate-limited far below what a wide repository needs. Failing to resolve is
    silent there — it costs a lower rate limit, not a capability — and becomes a warning only when
    the run actually needed the platform for something.
    """
    if platform is not None:
        return platform, ""
    needed = request.publish or request.trigger.is_woken or request.change is not None
    try:
        return GitHub.of(repository), ""
    except ScmError as error:
        return None, str(error) if needed else ""


def _speaks_as(platform: Platform | None) -> Identity | None:
    """Whose account the credential is, when it can be asked. `None` is not an answer to act on."""
    if platform is None:
        return None
    try:
        return platform.identity()
    except ScmError:
        return None


def _recall(
    request: Request,
    *,
    config: Config,
    repository: Repository,
    outcomes: tuple[TaskOutcome, ...],
    coverage: Coverage,
    run: str,
    woken: Woken | None,
) -> tuple[tuple[Escalation, ...], Memory | None, dict[str, Any], Absences, tuple[str, ...]]:
    """What earlier runs left for this one, and what this one will leave behind.

    Two things live in that memory and they are not remembered under the same condition. Which
    checks keep failing is only worth counting for the runs nobody reads: a run somebody started has
    that somebody watching its output, so a repeat needs no issue to be noticed.

    How long a tracked finding has gone unreported is counted by every maintenance run, because it
    decides whether an issue is closed and that has nothing to do with who is watching. A run
    started by hand that could close nothing for want of the count would leave the tracker exactly
    as frozen as the rule this replaced.

    A review writes nothing here, having run on code a stranger proposed.
    """
    when = datetime.now(UTC)
    asked = frozenset({woken.key} if woken is not None and woken.key else ())
    blank = Absences.of({}, outcomes=outcomes, run=run, when=when, coverage=coverage, asked=asked)
    if not request.trigger.is_maintenance:
        return (), None, {}, blank, ()
    memory = Memory(repository=repository, ref=config.storage.state_ref)
    known = memory.read()
    escalations, document = (
        weigh(outcomes, memory=known, run=run, when=when)
        if request.trigger.is_scheduled
        else ((), dict(known))
    )
    # Compared against what was read, and merged after: the other order compares this run's coverage
    # with itself and finds every run thorough.
    shortfall = coverage.shortfall(previous(known))
    return (
        escalations,
        memory,
        coverage.document(document),
        Absences.of(known, outcomes=outcomes, run=run, when=when, coverage=coverage, asked=asked),
        shortfall,
    )


def _announce(
    request: Request,
    *,
    manifest: Manifest,
    platform: Platform,
    repository: Repository,
    verdict: Verdict,
    report: str,
    outcomes: tuple[TaskOutcome, ...],
    change: ChangeView | None,
    fixes: tuple[Fix, ...],
    queue: Queue | None = None,
    new_issues: int,
    tracked: tuple[Issue, ...] | None = None,
    approvals: dict[str, Approval] | None = None,
    surfaces: dict[str, tuple[tuple[str, ...], ...]] | None = None,
    escalations: tuple[Escalation, ...] = (),
    memory: Memory | None = None,
    document: dict[str, Any] | None = None,
    absences: Absences | None = None,
    woken: Woken | None = None,
    asked: str = "",
) -> None:
    """Write the decision where the trigger's audience is, and record who wrote it.

    A review run has a conversation to write in; a maintenance run has none, so its findings are
    tracked as issues and its verified branches are proposed as change requests. Both paths are the
    same reconciliation by finding key, and both ask the platform once who the credential speaks for
    — a decision published under a person's name is a machine's judgement wearing it.

    Issues are written before the change requests that link them, which is the only order in which a
    change request can name the issue it answers.
    """
    try:
        identity = platform.identity()
    except ScmError as error:
        manifest.actions = {"failure": str(error)}
        manifest.warnings.append(f"nothing was published: {error}")
        return
    prior = manifest.actions if isinstance(manifest.actions, dict) else {}
    actions: dict[str, Any] = {"identity": identity.as_json()}
    if "reclaim" in prior:
        actions["reclaim"] = prior["reclaim"]
    caution = caution_for(identity)

    if request.trigger.is_maintenance:
        counted = absences or Absences.of(
            {}, outcomes=outcomes, run=manifest.run_id, when=datetime.now(UTC)
        )
        recorded = track_findings(
            platform,
            verdict=verdict,
            absences=counted,
            head=repository.head,
            limit=new_issues,
            escalations=escalations,
            known=tracked,
            approvals=approvals,
            surfaces=surfaces,
        )
        actions["issues"] = recorded.as_json()
        if escalations:
            actions["escalations"] = [item.as_json() for item in escalations]
        if recorded.failure:
            manifest.warnings.append(f"the tracked issues are incomplete: {recorded.failure}")
        if queue is not None and queue.awaiting_review:
            notices = notice_open_prs(
                platform,
                awaiting=queue.awaiting_review,
                numbers=recorded.numbers,
                judged={item.finding.key: item for item in verdict.judged},
            )
            if notices:
                actions["open_pr_notices"] = [item.as_json() for item in notices]
        if memory is not None:
            # Written after the issues, so a streak is only recorded as continuing once the run has
            # done what the streak is for. A memory stored before the write, in a run that then
            # fails to reach the tracker, would count a run that told nobody anything.
            stored, failed = memory.write(
                counted.document(document or {}), platform=platform, run=manifest.run_id
            )
            actions["memory"] = {"ref": memory.ref, "stored": stored, "failure": failed}
            if failed:
                manifest.warnings.append(
                    f"what this run learnt was not remembered ({failed}): a check that keeps "
                    "failing will read as a first failure next week and be reported to nobody, and "
                    "an issue whose finding has gone will wait another run to be closed"
                )
        proposals: dict[str, tuple[str, str]] = {}
        if any(fix.outcome is FixOutcome.FIXED for fix in fixes):
            opened = propose_fixes(
                platform,
                fixes=fixes,
                path=repository.path,
                base=repository.branch,
                issues=recorded.numbers,
                run=manifest.run_id,
            )
            actions["changes"] = opened.as_json()
            proposals = {item.key: (item.what, item.detail) for item in opened.posted}
            for item in opened.posted:
                if item.what != "proposed":
                    manifest.warnings.append(f"a verified fix was not proposed: {item.detail}")
        if woken is not None and woken.issue is not None:
            failed = _report_back(
                platform,
                woken=woken,
                issue=woken.issue,
                asked=asked,
                verdict=verdict,
                outcomes=outcomes,
                fixes=fixes,
                proposals=proposals,
                tracked=recorded,
                approval=(approvals or {}).get(woken.key),
                run=manifest.run_id,
            )
            actions["status"] = {"posted": not failed, "failure": failed}
            if failed:
                manifest.warnings.append(
                    f"the person who woke this run was not told what happened ({failed}); the "
                    "issue they commented on shows no answer"
                )
    else:
        assert request.change is not None  # noqa: S101 - guaranteed by the check in `run`
        published = publish_review(
            platform,
            number=request.change,
            verdict=verdict,
            report=report,
            head=repository.head,
            outcomes=outcomes,
            change=change,
            identity=identity,
        )
        actions["review"] = published.as_json()
        if published.identity is not None:
            # The review's own author outranks what the credential said about itself, so the caution
            # is recomputed from the name the platform actually recorded.
            actions["identity"] = published.identity.as_json()
            caution = published.caution
        if published.failure:
            manifest.warnings.append(f"nothing was published: {published.failure}")
        elif published.withheld:
            manifest.warnings.append(f"nothing was published: {published.withheld}")

    actions["caution"] = caution
    manifest.actions = actions
    if caution:
        manifest.warnings.append(caution)


def _report_back(
    platform: Platform,
    *,
    woken: Woken,
    issue: Issue,
    asked: str,
    verdict: Verdict,
    outcomes: tuple[TaskOutcome, ...],
    fixes: tuple[Fix, ...],
    proposals: dict[str, tuple[str, str]],
    tracked: Tracking,
    approval: Approval | None,
    run: str,
) -> str:
    """Tell the person on the issue they commented on what came of it, or say why that failed.

    Only on an issue. A comment on a change request is answered by the review this run publishes
    there — a second comment saying the same thing would be the agent talking twice.

    Every sentence comes from a recorded fact: whether the owning check finished, whether it still
    reports the finding, what the fix session did, and what the platform said about the branch.
    Nothing here is generated.
    """
    key = woken.key
    fix = next((item for item in fixes if key in item.job.keys), None)
    what, detail = proposals.get(key, ("", ""))
    aftermath = Aftermath(
        still_found=any(item.finding.key == key for item in verdict.judged),
        proven=concluded(key, outcomes),
        fixed=fix is not None and fix.outcome is FixOutcome.FIXED,
        fix_detail=fix.detail if fix is not None else "",
        proposal=detail if what == "proposed" else "",
        problem=detail if what and what != "proposed" else "",
        approved=approval.sentence if approval is not None else "",
    )
    if fix is None and not aftermath.problem:
        aftermath = replace(
            aftermath,
            problem=next(
                (
                    item.detail
                    for item in tracked.posted
                    if item.key == key and item.what == "deferred"
                ),
                "",
            ),
        )
    try:
        platform.note(issue, status_for(woken, aftermath, asked=asked, run=run))
    except ScmError as error:
        return str(error)
    return ""


def _conclude(
    manifest: Manifest, executed: list[Executed], *, rules: BlockingRules, session: Session
) -> Verdict:
    """Fold what the tasks produced into the manifest and one verdict."""
    by_id = {record.id: record for record in manifest.tasks}
    findings: list[Finding] = []
    for item in executed:
        record = by_id[item.task.id]
        record.outcome = item.outcome.outcome
        record.reason = item.outcome.reason
        record.attempts = [attempt.as_json() for attempt in item.attempts]
        record.calls = item.calls
        record.notes = item.result.notes if item.result else ""
        findings.extend(item.findings)
        for attempt in item.attempts:
            manifest.models.append(
                {"task": item.task.id, "attempt": attempt.number} | attempt.session.as_json()
            )

    judged = judge(
        merge(tuple(findings)),
        rules=rules,
        reliabilities=session.evidence.reliabilities(),
    )
    verdict = decide(tuple(item.outcome for item in executed), judged)
    manifest.findings = [item.as_json() for item in judged]
    manifest.verdict = verdict.as_json()
    manifest.evidence = [record.as_json() for record in session.evidence]
    manifest.cost = _cost(manifest.models)
    return verdict


def _account(manifest: Manifest, fixes: list[Fix]) -> None:
    """Put the fix sessions on the same books as the analysis ones.

    The first live run of this phase reported one accounted session and 2.5M tokens in `cost` while
    the ledger, which counts everything it is asked to admit, had four and 7.4M. A cost figure that
    leaves out the most expensive half of the run is worse than none: it is a number a team would
    plan a budget with.
    """
    for fix in fixes:
        for attempt in fix.attempts:
            manifest.models.append(
                {"task": fix.job.task.id, "attempt": attempt.number} | attempt.session.as_json()
            )
    manifest.cost = _cost(manifest.models)


def _spent(manifest: Manifest, performed: Performed) -> None:
    """Put a wake's own sessions on the same books as everything else the run paid for.

    The classifier runs before the plan is even narrowed, so a run that went on to re-establish a
    fact would otherwise report the analysis it did and not the reading that chose it — and the
    ledger, which admitted both, would disagree with the manifest about what the run cost.
    """
    written = performed.answered
    for task, attempts in (
        ("wake-intent", performed.read.attempts if performed.read else []),
        (written.task if written else "wake-answer", written.attempts if written else []),
    ):
        manifest.models += [
            {"task": task, "attempt": attempt.number} | attempt.session.as_json()
            for attempt in attempts
        ]
    if performed.read is not None or performed.answered is not None:
        manifest.cost = _cost(manifest.models)


def _cost(models: list[dict[str, object]]) -> dict[str, object]:
    """Tokens summed from the sessions that reported them, and honest about the ones that did not.

    An unaccounted session makes the total partial, and the total says so. Treating silence as zero
    would let a budget be exceeded by a backend that simply does not report.
    """
    total = 0
    accounted = 0
    for entry in models:
        usage = entry.get("usage")
        if not isinstance(usage, dict) or not usage.get("known"):
            continue
        tokens = usage.get("total_tokens")
        if isinstance(tokens, int):
            total += tokens
            accounted += 1
    return {
        "known": accounted > 0,
        "tokens": total if accounted else None,
        "money": None,
        "sessions": len(models),
        "accounted_sessions": accounted,
    }


def _tool_cache(request: Request, config: Config) -> Path:
    """Resolved like the fact cache, and kept even when facts are not: `--no-cache` is about not
    trusting a remembered answer, not about downloading the same crates a second time."""
    path = config.storage.tool_path
    return path if path.is_absolute() else request.repository / path


def _cache_root(request: Request, config: Config) -> Path | None:
    """A relative cache path is resolved against the repository, so CI can cache one directory."""
    if config.storage.cache_path is None or not request.use_cache:
        return None
    path = config.storage.cache_path
    return path if path.is_absolute() else request.repository / path
