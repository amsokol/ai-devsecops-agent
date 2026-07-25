"""One run, end to end.

The shape is deliberate: plan deterministically, let subagents judge, then decide deterministically
again. Everything a model produces passes through validation before it can influence the verdict,
and anything that fails validation is recorded as "did not run" rather than as "found nothing".
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from agent import __version__
from agent.backends.port import Backend, Budget, ToolEndpoint
from agent.backends.select import make_backend
from agent.config import Config
from agent.domain import Trigger
from agent.errors import ExitCode
from agent.executor import Executed, execute
from agent.findings import Finding, merge
from agent.library import Library
from agent.manifest import Manifest
from agent.overlay import Overlay
from agent.planner import ChangeSet, plan_run
from agent.policy import BlockingRules
from agent.repo import Repository
from agent.report import render
from agent.session import Session
from agent.storage import FactCache
from agent.tools import grant
from agent.verdict import Verdict, decide, judge

PLANNED = "planned"
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
    wake_issue: int | None = None
    plan_only: bool = False
    use_cache: bool = True


@dataclass(frozen=True, slots=True)
class RunRecord:
    manifest: Manifest
    manifest_path: Path
    exit_code: ExitCode
    verdict: Verdict | None = None
    report: str = ""


def run(request: Request, *, backend: Backend | None = None) -> RunRecord:
    config = Config.load(request.config_dir)
    scenario = config.scenario_for(request.trigger)

    library = Library.load(request.library_path, agent_version=__version__)
    library.check_pinned(version=config.pin.version, digest=config.pin.digest)
    # Read before anything runs: a blocking table the agent cannot parse must stop the run, not be
    # discovered when the first finding needs judging.
    rules = BlockingRules.read(library)

    overlay = Overlay.load(
        request.overlay_path,
        library=library,
        default_limits=config.maintenance_limits,
        notes_limit=config.notes_limit,
    )

    repository = Repository.open(request.repository)
    change: ChangeSet | None = None
    if request.trigger.is_maintenance:
        base = None
    else:
        base = request.base or "main"
        change = ChangeSet(paths=repository.changed_paths(base))

    plan = plan_run(
        scenario=scenario,
        trigger=request.trigger,
        library=library,
        overlay=overlay,
        change=change,
    )

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
            "digest": overlay.digest,
            "ecosystems": list(overlay.ecosystems),
            "quarantine_days": overlay.quarantine_days,
            "maintenance": {
                "open_change_requests": overlay.limits.open_change_requests,
                "new_issues_per_run": overlay.limits.new_issues_per_run,
            },
        },
    )
    manifest.warnings.extend(overlay.warnings)
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

    if request.plan_only:
        manifest.finish(PLANNED)
        return RunRecord(manifest, manifest.write(request.run_dir), ExitCode.OK)

    # Only a run on the default branch may write facts. A review runs on code a stranger proposed,
    # so it reads the cache and never feeds it.
    cache = FactCache(_cache_root(request, config), writable=request.trigger.is_maintenance)
    run_directory = request.run_dir / manifest.run_id
    session = Session(
        repository=repository.path,
        grants=grants,
        cache=cache,
        scratch_root=run_directory / "scratch",
    )

    owned = backend is None
    backend = backend or make_backend(config.execution)
    endpoint: ToolEndpoint | None = None
    try:
        executed = asyncio.run(
            execute(
                plan,
                backend=backend,
                library=library,
                notes=overlay.notes,
                evidence=session.evidence,
                tasks_dir=run_directory / "tasks",
                workdir=repository.path,
                budget=Budget(
                    seconds=config.execution.task_seconds, steps=config.execution.task_steps
                ),
                endpoint=endpoint,
            )
        )
    finally:
        if owned:
            asyncio.run(backend.close())

    verdict = _conclude(manifest, executed, rules=rules, session=session)
    report = render(
        verdict,
        trigger=request.trigger,
        tasks=tuple(item.outcome for item in executed),
        library_version=library.identity.version,
        unverified_facts=len(session.evidence.unverified()),
    )

    manifest.cache = cache.stats.as_json() | {"writable": cache.writable}
    manifest.finish(verdict.result.value)
    manifest_path = manifest.write(request.run_dir)
    session.evidence.write(manifest_path.parent / "evidence.jsonl")
    (manifest_path.parent / REPORT).write_text(report, encoding="utf-8")
    return RunRecord(manifest, manifest_path, verdict.result.exit_code, verdict, report)


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
