"""One run, end to end.

The shape is deliberate: plan deterministically, let subagents judge, then decide deterministically
again. Everything a model produces passes through validation before it can influence the verdict,
and anything that fails validation is recorded as "did not run" rather than as "found nothing".
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

from agent import __version__
from agent.backends.port import Backend, Budget
from agent.backends.select import Roster
from agent.budget import Ledger, RunBudget
from agent.config import Config, Models
from agent.domain import FixOutcome, Plan, Role, Trigger
from agent.errors import ConfigError, ExitCode
from agent.escalate import Escalation, weigh
from agent.executor import Executed, execute
from agent.findings import Finding, merge
from agent.issues import track_findings
from agent.library import Library
from agent.manifest import Manifest
from agent.overlay import MAINTENANCE, REVIEW, VALUES_FILE, Overlay, digest_on_disk, within
from agent.planner import ChangeSet, plan_run
from agent.policy import BlockingRules
from agent.propose import propose_fixes
from agent.publish import publish_review
from agent.reconcile import caution_for
from agent.remediate import BRANCH_PREFIX, Fix, Queue, apply, plan_fixes
from agent.repo import ChangeView, Repository
from agent.report import render
from agent.scm import GitHub, Platform, ScmError
from agent.session import Session
from agent.state import Memory
from agent.storage import FactCache
from agent.toolkit import Toolkits
from agent.tools import grant
from agent.verdict import TaskOutcome, Verdict, decide, judge

PLANNED = "planned"
DECLINED = "declined"
REPORT = "report.md"
BOT_SUFFIX = "[bot]"


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
    wake_issue: int | None = None
    actor: str = ""
    """The account whose action started this run. Given by whatever woke the agent, and checked
    before anything is spent: an agent that answers its own comment answers it forever."""
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
    may_fix = request.trigger.is_maintenance and not request.dry_run and not request.plan_only
    needed = {task.role for task in plan.tasks} | ({Role.FIXER} if may_fix else set())
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
        manifest.warnings.append(f"nothing was published: {speaks}")
    refusal = _woke_itself(request, platform=speaker)
    if refusal:
        # Recorded as a run, deliberately: "the agent declined to answer its own comment" is the
        # property this check exists for, and a run that left no trace could not demonstrate it.
        manifest.warnings.append(refusal)
        manifest.finish(DECLINED)
        return RunRecord(manifest, manifest.write(request.run_dir), ExitCode.OK)

    # Only a run on the default branch may write facts. A review runs on code a stranger proposed,
    # so it reads the cache and never feeds it.
    cache = FactCache(_cache_root(request, config), writable=request.trigger.is_maintenance)
    run_directory = request.run_dir / manifest.run_id
    session = Session(
        repository=repository.path,
        grants=grants,
        cache=cache,
        change=ChangeView.of(repository, base) if base is not None else None,
        scratch_root=run_directory / "scratch",
        never_send=config.never_send,
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
    )
    # One event loop for execution and shutdown alike. A backend that holds a subprocess cannot be
    # closed from a second loop: the process was awaited in the first one, and closing it elsewhere
    # fails with a future attached to another loop.
    ledger = Ledger(RunBudget(max_parallel=spend.tasks_at_once, tokens=spend.tokens_per_run))

    proposed: tuple[str, ...] = ()
    if may_fix and request.publish:
        if speaker is None:
            may_fix = False
        else:
            try:
                proposed = tuple(item.head for item in speaker.proposals(prefix=BRANCH_PREFIX))
            except ScmError as error:
                may_fix = False
                manifest.warnings.append(
                    f"no fix branches were prepared: the open change requests could not be read "
                    f"({error}), and preparing branches blind would duplicate ones already open"
                )

    executed, verdict, fixes, queue = asyncio.run(
        _perform(
            plan,
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
            proposed=proposed,
            close=owned,
        )
    )
    manifest.budget["spend"] = ledger.spend.as_json()
    if queue is not None:
        manifest.fixes = [fix.as_json() for fix in fixes]
        manifest.remediation = queue.as_json()

    report = render(
        verdict,
        trigger=request.trigger,
        tasks=tuple(item.outcome for item in executed),
        library_version=library.identity.version,
        unverified_facts=len(session.evidence.unverified()),
        fixes=tuple(fixes),
        notice=notice,
    )

    if request.publish and speaker is not None:
        # After the report, because the report is what gets published, and outside the event loop
        # because talking to the platform is a subprocess away rather than a coroutine.
        outcomes = tuple(item.outcome for item in executed)
        escalations, remembered, document = _recall(
            request,
            config=config,
            repository=repository,
            outcomes=outcomes,
            run=manifest.run_id,
        )
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
            new_issues=overlay.queue.max_new_issues_per_run,
            escalations=escalations,
            memory=remembered,
            document=document,
        )
    elif request.publish:
        manifest.actions = {"failure": speaks}

    manifest.cache = cache.stats.as_json() | {"writable": cache.writable}
    manifest.finish(verdict.result.value)
    manifest_path = manifest.write(request.run_dir)
    session.evidence.write(manifest_path.parent / "evidence.jsonl")
    (manifest_path.parent / REPORT).write_text(report, encoding="utf-8")
    return RunRecord(manifest, manifest_path, verdict.result.exit_code, verdict, report)


async def _perform(
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
    close: bool,
) -> tuple[list[Executed], Verdict, list[Fix], Queue | None]:
    """Analyse, decide, and then — on a maintenance run — fix what the decision allows.

    All of it in one event loop, including the shutdown. A backend holding a subprocess cannot be
    closed from a second loop: the process was awaited in the first one. That constraint is why the
    deterministic decision in the middle happens here rather than between two `asyncio.run` calls.
    """
    try:
        executed = await execute(
            plan,
            roster=roster,
            library=library,
            notes=overlay.notes,
            evidence=session.evidence,
            tasks_dir=run_directory / "tasks",
            budget=budget,
            toolkits=toolkits,
            ledger=ledger,
        )
        verdict = _conclude(manifest, executed, rules=rules, session=session)
        if not may_fix:
            return executed, verdict, [], None
        queue = plan_fixes(
            verdict.judged,
            library=library,
            overlay=overlay,
            playbook=plan.playbook,
            repository=repository,
            max_open_fix_requests=overlay.queue.max_open_fix_requests,
            proposed=proposed,
        )
        fixes = await apply(
            queue,
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
        )
        _account(manifest, fixes)
        return executed, verdict, fixes, queue
    finally:
        if close:
            await roster.close()


def _resolve(
    request: Request, *, platform: Platform | None, repository: Repository
) -> tuple[Platform | None, str]:
    """The platform this run will speak to, or why it cannot speak at all.

    A missing client, an unreadable remote or an absent credential costs a warning rather than the
    run: by the time any of it matters the analysis is the expensive part, and it is already done.
    """
    if not request.publish or platform is not None:
        return platform, ""
    try:
        return GitHub.of(repository), ""
    except ScmError as error:
        return None, str(error)


def _woke_itself(request: Request, *, platform: Platform | None) -> str:
    """Why this wake must be ignored, or an empty string when it is somebody's genuine request.

    Two rules. The first is the library's: a bot's comment does not wake the agent. The second is
    the loop this project has already seen the start of — a run publishing under a human account, in
    a workflow that wakes on human comments, wakes itself. The account is compared rather than the
    wording, because "was this comment mine?" has exactly one honest answer.

    Only a wake is checked. A schedule and a manual run have no author to suspect, and a change
    somebody pushed is a change worth reviewing whoever pushed it.
    """
    if not request.actor or not request.trigger.is_woken:
        return ""
    if request.actor.endswith(BOT_SUFFIX):
        return (
            f"declined: {request.actor} is a bot, and a machine's comment does not wake the agent. "
            "A run per comment between two machines is a bill with no reader"
        )
    if platform is None:
        # Without a credential there is no account to compare against, and the suffix rule above is
        # all that can be checked. Said out loud rather than assumed safe.
        return ""
    try:
        mine = platform.identity()
    except ScmError:
        return ""
    if mine.login and mine.login == request.actor:
        return (
            f"declined: this run was woken by {request.actor}, which is the account the agent "
            "publishes as. Answering its own comment is a loop, and each turn of it costs a model"
        )
    return ""


def _recall(
    request: Request,
    *,
    config: Config,
    repository: Repository,
    outcomes: tuple[TaskOutcome, ...],
    run: str,
) -> tuple[tuple[Escalation, ...], Memory | None, dict[str, Any]]:
    """What earlier runs remember about failing checks, and what this run should remember.

    Only a scheduled run keeps this. A run somebody started has that somebody watching its output,
    so a failure needs no issue to be noticed; the memory exists for the runs nobody reads.
    """
    if not request.trigger.is_scheduled:
        return (), None, {}
    memory = Memory(repository=repository, ref=config.storage.state_ref)
    escalations, document = weigh(outcomes, memory=memory.read(), run=run, when=datetime.now(UTC))
    return escalations, memory, document


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
    new_issues: int,
    escalations: tuple[Escalation, ...] = (),
    memory: Memory | None = None,
    document: dict[str, Any] | None = None,
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
    actions: dict[str, Any] = {"identity": identity.as_json()}
    caution = caution_for(identity)

    if request.trigger.is_maintenance:
        tracked = track_findings(
            platform,
            verdict=verdict,
            outcomes=outcomes,
            head=repository.head,
            limit=new_issues,
            escalations=escalations,
        )
        actions["issues"] = tracked.as_json()
        if escalations:
            actions["escalations"] = [item.as_json() for item in escalations]
        if tracked.failure:
            manifest.warnings.append(f"the tracked issues are incomplete: {tracked.failure}")
        if memory is not None:
            # Written after the issues, so a streak is only recorded as continuing once the run has
            # done what the streak is for. A memory stored before the write, in a run that then
            # fails to reach the tracker, would count a run that told nobody anything.
            stored, failed = memory.write(document or {}, platform=platform, run=manifest.run_id)
            actions["memory"] = {"ref": memory.ref, "stored": stored, "failure": failed}
            if failed:
                manifest.warnings.append(
                    f"which checks keep failing was not remembered ({failed}), so a repeat next "
                    "week reads as a first failure and is reported to nobody"
                )
        if any(fix.outcome is FixOutcome.FIXED for fix in fixes):
            opened = propose_fixes(
                platform,
                fixes=fixes,
                path=repository.path,
                base=repository.branch,
                issues=tracked.numbers,
                run=manifest.run_id,
            )
            actions["changes"] = opened.as_json()
            for item in opened.posted:
                if item.what != "proposed":
                    manifest.warnings.append(f"a verified fix was not proposed: {item.detail}")
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


def _cache_root(request: Request, config: Config) -> Path | None:
    """A relative cache path is resolved against the repository, so CI can cache one directory."""
    if config.storage.cache_path is None or not request.use_cache:
        return None
    path = config.storage.cache_path
    return path if path.is_absolute() else request.repository / path
