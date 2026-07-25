"""One run, end to end.

At this stage the run plans, records and stops: no subagent exists yet. It therefore never claims a
passing result. Absence of a result is not a result, so a run that executed no task is inconclusive
and refuses the merge — the same behaviour a broken scanner will get later.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent import __version__
from agent.config import Config
from agent.domain import Outcome, Reason, RunResult, Trigger
from agent.errors import ExitCode
from agent.library import Library
from agent.manifest import Manifest
from agent.overlay import Overlay
from agent.planner import ChangeSet, plan_run
from agent.repo import Repository
from agent.session import Session
from agent.storage import FactCache
from agent.tools import grant

PLANNED = "planned"


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


def run(request: Request) -> RunRecord:
    config = Config.load(request.config_dir)
    scenario = config.scenario_for(request.trigger)

    library = Library.load(request.library_path, agent_version=__version__)
    library.check_pinned(version=config.pin.version, digest=config.pin.digest)

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
    session = Session(
        repository=repository.path,
        grants=grants,
        cache=cache,
        scratch_root=request.run_dir / manifest.run_id / "scratch",
    )

    for task in manifest.tasks:
        task.outcome = Outcome.UNVERIFIED
        task.reason = Reason.NOT_IMPLEMENTED

    manifest.evidence = [record.as_json() for record in session.evidence]
    manifest.cache = cache.stats.as_json() | {"writable": cache.writable}
    result = result_from_tasks(manifest)
    manifest.finish(result.value)
    manifest_path = manifest.write(request.run_dir)
    session.evidence.write(manifest_path.parent / "evidence.jsonl")
    return RunRecord(manifest, manifest_path, result.exit_code)


def _cache_root(request: Request, config: Config) -> Path | None:
    """A relative cache path is resolved against the repository, so CI can cache one directory."""
    if config.storage.cache_path is None or not request.use_cache:
        return None
    path = config.storage.cache_path
    return path if path.is_absolute() else request.repository / path


def result_from_tasks(manifest: Manifest) -> RunResult:
    """A required task that ended in a failure kind makes the run inconclusive."""
    for task in manifest.tasks:
        if not task.required:
            continue
        if task.outcome is Outcome.EXHAUSTED:
            return RunResult.INCONCLUSIVE
        if task.outcome is Outcome.UNVERIFIED and (task.reason is None or task.reason.is_failure):
            return RunResult.INCONCLUSIVE
        if task.outcome is None:
            return RunResult.INCONCLUSIVE
    return RunResult.PASS
