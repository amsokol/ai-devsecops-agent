from __future__ import annotations

from agent.config import Config
from agent.domain import Trigger
from agent.library import Library
from agent.overlay import Overlay
from agent.planner import ChangeSet, plan_run


def review_plan(
    config: Config, library: Library, overlay: Overlay, *paths: str
) -> dict[str, tuple[str, ...]]:
    plan = plan_run(
        scenario=config.scenario_for(Trigger.CHANGE_OPENED),
        trigger=Trigger.CHANGE_OPENED,
        library=library,
        overlay=overlay,
        change=ChangeSet(paths=paths),
    )
    return {task.id: task.scope for task in plan.tasks}


def test_source_change_plans_code_tasks_only(
    config: Config, library: Library, overlay: Overlay
) -> None:
    tasks = review_plan(config, library, overlay, "src/api.py")
    assert set(tasks) == {"code-quality", "code-vuln"}
    assert tasks["code-quality"] == ("src/api.py",)


def test_documentation_only_change_plans_nothing(
    config: Config, library: Library, overlay: Overlay
) -> None:
    assert review_plan(config, library, overlay, "README.md", "docs/guide.md") == {}


def test_pin_change_plans_one_task_per_touched_ecosystem(
    config: Config, library: Library, overlay: Overlay
) -> None:
    tasks = review_plan(config, library, overlay, "uv.lock")
    assert set(tasks) == {"deps-outdated@python-uv", "deps-vuln@python-uv"}
    assert tasks["deps-vuln@python-uv"] == ("uv.lock",)


def test_disabled_ecosystem_is_not_planned(
    config: Config, library: Library, overlay: Overlay
) -> None:
    tasks = review_plan(config, library, overlay, ".github/workflows/ci.yml")
    assert set(tasks) == {"code-vuln"}


def test_skipped_capabilities_carry_a_reason(
    config: Config, library: Library, overlay: Overlay
) -> None:
    plan = plan_run(
        scenario=config.scenario_for(Trigger.CHANGE_OPENED),
        trigger=Trigger.CHANGE_OPENED,
        library=library,
        overlay=overlay,
        change=ChangeSet(paths=("src/api.py",)),
    )
    reasons = dict(plan.skipped)
    assert reasons["capabilities/deps-vuln"] == "no enabled ecosystem's pins changed"


def test_plan_is_identical_for_identical_input(
    config: Config, library: Library, overlay: Overlay
) -> None:
    paths = ("src/api.py", "uv.lock")
    first = review_plan(config, library, overlay, *paths)
    assert first == review_plan(config, library, overlay, *paths)
    assert list(first) == [
        "code-quality",
        "code-vuln",
        "deps-outdated@python-uv",
        "deps-vuln@python-uv",
    ]


def test_knowledge_slice_follows_library_links(
    config: Config, library: Library, overlay: Overlay
) -> None:
    plan = plan_run(
        scenario=config.scenario_for(Trigger.CHANGE_OPENED),
        trigger=Trigger.CHANGE_OPENED,
        library=library,
        overlay=overlay,
        change=ChangeSet(paths=("uv.lock",)),
    )
    task = next(task for task in plan.tasks if task.id == "deps-outdated@python-uv")
    assert task.knowledge == (
        "playbooks/pr-review",
        "capabilities/deps-outdated",
        "ecosystems/python-uv",
        "policy/verdicts",
    )


def test_maintenance_covers_hotspots_and_every_enabled_ecosystem(
    config: Config, library: Library, overlay: Overlay
) -> None:
    plan = plan_run(
        scenario=config.scenario_for(Trigger.MAINTAIN_SCHEDULED),
        trigger=Trigger.MAINTAIN_SCHEDULED,
        library=library,
        overlay=overlay,
        change=None,
    )
    assert [task.id for task in plan.tasks] == [
        "code-quality",
        "code-vuln",
        "deps-outdated@python-uv",
        "deps-vuln@python-uv",
    ]
    assert plan.tasks[0].scope == ("src",)
