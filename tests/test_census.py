"""Failing a github-actions outdated sweep that skipped census pins."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from agent.census import incomplete_action_sweep
from agent.domain import PlannedTask, Role
from agent.evidence import Evidence, Origin, Subject


def _task() -> PlannedTask:
    return PlannedTask(
        id="deps-outdated@github-actions",
        capability="capabilities/deps-outdated",
        role=Role.ANALYST,
        required=True,
        ecosystem="ecosystems/github-actions",
    )


def _fact(package: str) -> Evidence:
    return Evidence.verified(
        question="declared-pin",
        subject=Subject(ecosystem="ecosystems/github-actions", package=package),
        value="v7",
        origin=Origin.TOOL,
        source="list_action_pins",
        observed_at=datetime(2026, 7, 27, tzinfo=UTC),
        recipe="capabilities/deps-outdated@list_action_pins",
    )


def test_incomplete_when_census_pins_lack_facts(tmp_path: Path) -> None:
    workflow = tmp_path / ".github" / "workflows"
    workflow.mkdir(parents=True)
    (workflow / "ci.yml").write_text(
        "jobs:\n  a:\n    steps:\n      - uses: actions/checkout@v7\n"
        "      - uses: Swatinem/rust-cache@v2\n",
        encoding="utf-8",
    )
    gap = incomplete_action_sweep(tmp_path, _task(), (_fact("actions/checkout"),))
    assert gap is not None
    assert "Swatinem/rust-cache" in gap
    assert "list_action_pins" in gap


def test_complete_when_every_census_pin_has_a_fact(tmp_path: Path) -> None:
    workflow = tmp_path / ".github" / "workflows"
    workflow.mkdir(parents=True)
    (workflow / "ci.yml").write_text(
        "jobs:\n  a:\n    steps:\n      - uses: actions/checkout@v7\n",
        encoding="utf-8",
    )
    assert incomplete_action_sweep(tmp_path, _task(), (_fact("actions/checkout"),)) is None


def test_committer_date_publish_time_fails_the_sweep(tmp_path: Path) -> None:
    workflow = tmp_path / ".github" / "workflows"
    workflow.mkdir(parents=True)
    (workflow / "ci.yml").write_text(
        "jobs:\n  a:\n    steps:\n      - uses: actions/checkout@v7\n",
        encoding="utf-8",
    )
    bad = Evidence.verified(
        question="publish-time",
        subject=Subject(ecosystem="ecosystems/github-actions", package="actions/checkout", version="v7"),
        value="2026-07-17T18:45:11Z",
        origin=Origin.API,
        source=(
            "https://api.github.com/repos/actions/checkout/git/commits/3d3c42e#committer.date; "
            "2026-07-17T18:45:11+00:00+7d"
        ),
        observed_at=datetime(2026, 7, 27, tzinfo=UTC),
        recipe="capabilities/deps-outdated@fetch+check_quarantine",
    )
    gap = incomplete_action_sweep(tmp_path, _task(), (_fact("actions/checkout"), bad))
    assert gap is not None
    assert "committer.date" in gap
    assert "action_publish_time" in gap
