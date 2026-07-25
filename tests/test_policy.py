"""The blocking table is read from the library, and an unreadable one stops the run."""

from __future__ import annotations

from pathlib import Path

import pytest
from agent.errors import ConfigError
from agent.findings import Klass, Severity
from agent.library import Library
from agent.policy import BlockingRules

TABLE = """\
## What blocks

| Class | Severity | Blocks |
| --- | --- | --- |
{rows}
"""


def with_table(library: Library, rows: str) -> Library:
    path = library.root / "policy/verdicts.md"
    header = path.read_text(encoding="utf-8").split("---\n\n", 1)[0] + "---\n\n"
    path.write_text(header + TABLE.format(rows=rows), encoding="utf-8")
    return Library.load(library.root, agent_version="0.1.0")


def test_the_table_decides_what_blocks(library: Library) -> None:
    rules = BlockingRules.read(library)

    assert rules.blocks(Klass.SECURITY, Severity.HIGH)
    assert not rules.blocks(Klass.SECURITY, Severity.MEDIUM)
    assert rules.blocks(Klass.ROUTINE, Severity.CRITICAL)
    assert not rules.blocks(Klass.ROUTINE, Severity.HIGH)
    assert rules.blocks(Klass.ROUTINE, Severity.LOW, forbidden_state=True)


def test_an_incomplete_table_is_refused(library: Library) -> None:
    reloaded = with_table(
        library,
        "| `security` | `critical` | yes |\n"
        "| `routine` | `critical` | yes |\n"
        "| forbidden state | any | yes |",
    )

    with pytest.raises(ConfigError, match="does not cover"):
        BlockingRules.read(reloaded)


def test_an_unknown_severity_is_refused_rather_than_ignored(library: Library) -> None:
    reloaded = with_table(
        library,
        "| `security` | `critical`, `high`, `apocalyptic` | yes |\n"
        "| `security` | `medium`, `low` | no |\n"
        "| `routine` | any | no |\n"
        "| forbidden state | any | yes |",
    )

    with pytest.raises(ConfigError, match="unknown severity"):
        BlockingRules.read(reloaded)


def test_a_missing_forbidden_state_row_is_refused(library: Library) -> None:
    reloaded = with_table(library, "| `security` | any | yes |\n| `routine` | any | no |")

    with pytest.raises(ConfigError, match="forbidden state"):
        BlockingRules.read(reloaded)


def test_no_table_at_all_stops_the_run(library: Library) -> None:
    path: Path = library.root / "policy/verdicts.md"
    header = path.read_text(encoding="utf-8").split("---\n\n", 1)[0] + "---\n\n"
    path.write_text(header + "Blocking is decided sensibly.\n", encoding="utf-8")
    reloaded = Library.load(library.root, agent_version="0.1.0")

    with pytest.raises(ConfigError, match="will not guess"):
        BlockingRules.read(reloaded)


def test_the_shipped_library_parses() -> None:
    """The real thing, because a table only the fixture satisfies would prove nothing."""
    root = Path(__file__).resolve().parents[2] / "ai-devsecops-skills-knowledge"
    if not (root / "library.yaml").is_file():
        pytest.skip("the knowledge library is not checked out next to the agent")
    rules = BlockingRules.read(Library.load(root, agent_version="0.1.0"))

    assert rules.blocks(Klass.SECURITY, Severity.CRITICAL)
    assert not rules.blocks(Klass.ROUTINE, Severity.MEDIUM)
