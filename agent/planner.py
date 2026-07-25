"""The deterministic planner: trigger plus facts in, task graph out.

No model participates. For "a change was opened" the task set is computable from the diff, and a
model call there would add latency, cost and nondeterminism to a blocking check — a gate that
sometimes passes on a rerun stops being believed.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent.config import Scenario, TaskRule, When
from agent.domain import Plan, PlannedTask, Trigger
from agent.library import Library
from agent.overlay import Overlay

WORKFLOW_MARKERS = (".github/workflows/", ".github/actions/")
NON_SOURCE_SUFFIXES = frozenset({".md", ".txt", ".rst", ".adoc", ".png", ".jpg", ".svg"})
NON_SOURCE_NAMES = frozenset({"LICENSE", "NOTICE", "CODEOWNERS", ".gitignore"})


@dataclass(frozen=True, slots=True)
class ChangeSet:
    """The facts about a change that the planner is allowed to look at."""

    paths: tuple[str, ...]

    def workflows(self) -> tuple[str, ...]:
        return tuple(path for path in self.paths if path.startswith(WORKFLOW_MARKERS))


def plan_run(
    *,
    scenario: Scenario,
    trigger: Trigger,
    library: Library,
    overlay: Overlay,
    change: ChangeSet | None,
) -> Plan:
    paths = change.paths if change else ()
    pin_paths = _pin_paths(library, overlay, paths)
    source_paths = _source_paths(paths, pin_paths)
    touched = library.ecosystems_for_paths(paths, overlay.ecosystems)

    tasks: list[PlannedTask] = []
    skipped: list[tuple[str, str]] = []
    for rule in scenario.tasks:
        ecosystems, scope, reason = _resolve(
            rule,
            overlay=overlay,
            touched=touched,
            source_paths=source_paths,
            workflow_paths=change.workflows() if change else (),
            pin_paths=pin_paths,
        )
        if reason is not None:
            skipped.append((rule.capability, reason))
            continue
        if rule.per_ecosystem:
            tasks.extend(
                _task(rule, scenario, library, ecosystem=ecosystem, scope=scope.get(ecosystem, ()))
                for ecosystem in ecosystems
            )
        else:
            tasks.append(_task(rule, scenario, library, ecosystem=None, scope=scope.get(None, ())))
    return Plan(
        playbook=scenario.playbook, trigger=trigger, tasks=tuple(tasks), skipped=tuple(skipped)
    )


def _task(
    rule: TaskRule,
    scenario: Scenario,
    library: Library,
    *,
    ecosystem: str | None,
    scope: tuple[str, ...],
) -> PlannedTask:
    short = rule.capability.rsplit("/", 1)[-1]
    roots: tuple[str, ...] = (scenario.playbook, rule.capability)
    if ecosystem is not None:
        roots += (ecosystem,)
        short = f"{short}@{ecosystem.rsplit('/', 1)[-1]}"
    return PlannedTask(
        id=short,
        capability=rule.capability,
        role=rule.role,
        required=rule.required,
        ecosystem=ecosystem,
        scope=scope,
        knowledge=library.closure(roots),
    )


def _resolve(
    rule: TaskRule,
    *,
    overlay: Overlay,
    touched: tuple[str, ...],
    source_paths: tuple[str, ...],
    workflow_paths: tuple[str, ...],
    pin_paths: dict[str, tuple[str, ...]],
) -> tuple[tuple[str, ...], dict[str | None, tuple[str, ...]], str | None]:
    """Whether a rule becomes tasks, over which ecosystems, and with which scope."""
    match rule.when:
        case When.SOURCE_CHANGED:
            if not source_paths:
                return (), {}, "no source files changed"
            return (), {None: source_paths}, None
        case When.SOURCE_OR_WORKFLOWS_CHANGED:
            combined = tuple(dict.fromkeys(source_paths + workflow_paths))
            if not combined:
                return (), {}, "no source or workflow files changed"
            return (), {None: combined}, None
        case When.ECOSYSTEM_PINS_CHANGED:
            if not touched:
                return (), {}, "no enabled ecosystem's pins changed"
            scope: dict[str | None, tuple[str, ...]] = {
                ecosystem: pin_paths.get(ecosystem, ()) for ecosystem in touched
            }
            return touched, scope, None
        case When.ECOSYSTEM_ENABLED:
            if not overlay.ecosystems:
                return (), {}, "the overlay enables no ecosystems"
            return overlay.ecosystems, {}, None
        case When.HOTSPOTS_PRESENT:
            if not overlay.hotspots:
                return (), {}, "the overlay names no hotspots"
            return (), {None: overlay.hotspots}, None


def _pin_paths(
    library: Library, overlay: Overlay, paths: tuple[str, ...]
) -> dict[str, tuple[str, ...]]:
    """Changed paths grouped by the enabled ecosystem that claims them."""
    grouped: dict[str, tuple[str, ...]] = {}
    for document in library.by_kind("ecosystem"):
        if document.id not in overlay.ecosystems:
            continue
        matched = tuple(path for path in paths if document.matches_path(path))
        if matched:
            grouped[document.id] = matched
    return grouped


def _source_paths(paths: tuple[str, ...], pin_paths: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    """Changed paths that are the product's own code.

    Workflow files are excluded even when no ecosystem claims them: the library judges them under
    `code-vuln`, and a workflow is not the kind of code `code-quality` reasons about.
    """
    claimed = {path for group in pin_paths.values() for path in group}
    return tuple(
        path
        for path in paths
        if path not in claimed
        and not path.startswith(WORKFLOW_MARKERS)
        and not path.endswith(tuple(NON_SOURCE_SUFFIXES))
        and path.rsplit("/", 1)[-1] not in NON_SOURCE_NAMES
    )
