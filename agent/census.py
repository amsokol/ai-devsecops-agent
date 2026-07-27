"""Whether a deps-outdated github-actions sweep examined every pin the census found."""

from __future__ import annotations

from pathlib import Path

from agent.coverage import Coverage
from agent.domain import PlannedTask
from agent.evidence import Evidence
from agent.tools.actions import list_action_pins, packages

ECOSYSTEM = "ecosystems/github-actions"
CAPABILITY = "capabilities/deps-outdated"
NAMED = 8
COMMITTER = "committer.date"


def incomplete_action_sweep(
    root: Path,
    task: PlannedTask,
    records: tuple[Evidence, ...] | list[Evidence],
) -> str | None:
    """Why this task's result is incomplete, or `None` when the census was covered.

    Only github-actions outdated sweeps are checked: that is the census tool we have. A short
    examined set against a non-empty census fails the task rather than publishing a partial list.
    Also refuses publish-time facts that cite a committer date — that clock predates the Release and
    falsely clears quarantine.
    """
    if task.capability != CAPABILITY or task.ecosystem != ECOSYSTEM:
        return None
    if reason := _committer_clock(records):
        return reason
    census = packages(list_action_pins(root))
    if not census:
        return None
    bucket = f"{CAPABILITY}:{ECOSYSTEM}"
    examined = Coverage.of(records).examined.get(bucket, frozenset())
    missing = census - examined
    if not missing:
        return None
    named = ", ".join(f"`{name}`" for name in sorted(missing)[:NAMED])
    more = f" (+{len(missing) - NAMED} more)" if len(missing) > NAMED else ""
    return (
        f"list_action_pins found {len(census)} package(s) under .github/ but this run only "
        f"recorded facts for {len(examined)}; not examined: {named}{more}. Call list_action_pins "
        "and record a fact for every pin — including those that are fine — before finishing"
    )


def _committer_clock(records: tuple[Evidence, ...] | list[Evidence]) -> str | None:
    bad: list[str] = []
    for record in records:
        if not record.is_verified:
            continue
        if record.question != "publish-time":
            continue
        source = record.source or ""
        if COMMITTER not in source:
            continue
        package = record.subject.package or "(unknown)"
        bad.append(package)
    if not bad:
        return None
    named = ", ".join(f"`{name}`" for name in sorted(set(bad))[:NAMED])
    return (
        f"publish-time for {named} cites committer.date. For github-actions call "
        "action_publish_time (GitHub Release published_at) and pass that into check_quarantine. "
        "A commit date is earlier than the release and falsely clears the window"
    )
