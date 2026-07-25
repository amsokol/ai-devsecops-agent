from __future__ import annotations

from pathlib import Path

import pytest
from agent.config import Config
from agent.domain import Role
from agent.errors import ConfigError
from agent.library import Library
from agent.overlay import Choice, Overlay

REQUIRED = """\
schema: 1
review:
  models:
    analyst: fake/none
  limits:
    tokens_per_run: 9
    minutes_per_task: 15
    tasks_at_once: 4
maintenance:
  models:
    analyst: fake/none
    fixer: fake/none
  limits:
    tokens_per_run: 9
    minutes_per_task: 10
    tasks_at_once: 2
  queue:
    max_new_issues_per_run: 5
    max_open_fix_requests: 3
"""
"""What no overlay may omit: the agent ships no model and no ceiling, so a product states both."""


def load(root: Path, library: Library, config: Config, *, notes_limit: int = 8000) -> Overlay:
    return Overlay.load(root, library=library, notes_limit=notes_limit)


def written(root: Path, values: str) -> None:
    (root / "agent.yaml").write_text(REQUIRED + values, encoding="utf-8")


def test_values_are_read(overlay: Overlay) -> None:
    assert overlay.ecosystems == ("ecosystems/python-uv",)
    assert overlay.quarantine_days == 7
    assert overlay.verification["python-uv"] == (("uv", "sync", "--frozen"),)
    assert overlay.queue.max_open_fix_requests == 3
    assert overlay.review.models[Role.ANALYST] == Choice(backend="fake", model="composer-2.5")
    assert overlay.review.limits.tokens_per_run == 24_000_000
    assert overlay.maintenance.models[Role.FIXER] == Choice(backend="fake", model="composer-2.5")
    assert overlay.maintenance.limits.tokens_per_run == 12_000_000


def test_unknown_key_is_refused_with_the_known_keys_listed(
    overlay_root: Path, library: Library, config: Config
) -> None:
    path = overlay_root / "agent.yaml"
    path.write_text(path.read_text(encoding="utf-8") + "ecosystem: oops\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown key\\(s\\) ecosystem"):
        load(overlay_root, library, config)


def test_unknown_ecosystem_lists_what_is_available(
    overlay_root: Path, library: Library, config: Config
) -> None:
    written(overlay_root, "ecosystems: [ecosystems/rust]\nquarantine: {days: 7}\n")
    with pytest.raises(ConfigError, match="available: ecosystems/github-actions"):
        load(overlay_root, library, config)


def test_quarantine_is_required_because_it_must_never_be_invented(
    overlay_root: Path, library: Library, config: Config
) -> None:
    written(overlay_root, "ecosystems: []\n")
    with pytest.raises(ConfigError, match="quarantine: is required"):
        load(overlay_root, library, config)


def test_a_product_that_names_no_model_is_stopped_before_it_spends(
    overlay_root: Path, library: Library, config: Config
) -> None:
    """The agent has no model to fall back on, and inventing one would be spending on a guess."""
    (overlay_root / "agent.yaml").write_text("schema: 1\nquarantine: {days: 7}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="review: is required"):
        load(overlay_root, library, config)


def test_a_role_the_agent_does_not_know_is_refused_rather_than_ignored(
    overlay_root: Path, library: Library, config: Config
) -> None:
    written(overlay_root, "quarantine: {days: 7}\n")
    path = overlay_root / "agent.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "    analyst: fake/none\n  limits:\n    tokens_per_run: 9\n    minutes_per_task: 15",
            "    reviewer: fake/none\n  limits:\n    tokens_per_run: 9\n    minutes_per_task: 15",
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match=r"review\.models\.reviewer: unknown role"):
        load(overlay_root, library, config)


def test_a_setting_in_the_wrong_block_is_refused_rather_than_ignored(
    overlay_root: Path, library: Library, config: Config
) -> None:
    """A queue limit under `review:` would be a number that quietly does nothing."""
    written(overlay_root, "quarantine: {days: 7}\n")
    path = overlay_root / "agent.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "review:\n  models:", "review:\n  queue:\n    max_new_issues_per_run: 1\n  models:"
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match=r"review: unknown key\(s\) queue"):
        load(overlay_root, library, config)


def test_a_model_without_its_provider_is_not_an_address(
    overlay_root: Path, library: Library, config: Config
) -> None:
    """The backend decides which models exist, so a bare name names nothing in particular."""
    written(overlay_root, "quarantine: {days: 7}\n")
    path = overlay_root / "agent.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace("fake/none", "none"),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="is not a pair"):
        load(overlay_root, library, config)


def test_limits_are_required_because_the_agent_will_not_decide_what_a_run_costs(
    overlay_root: Path, library: Library, config: Config
) -> None:
    (overlay_root / "agent.yaml").write_text(
        "schema: 1\nquarantine: {days: 7}\nreview:\n  models:\n    analyst: fake/none\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match=r"review\.limits: is required"):
        load(overlay_root, library, config)


def test_each_kind_of_run_is_described_in_full_and_on_its_own(
    overlay_root: Path, library: Library, config: Config
) -> None:
    """Neither block inherits from the other: what a run does is visible where the run is named."""
    (overlay_root / "agent.yaml").write_text(
        "schema: 1\nquarantine: {days: 7}\nreview:\n  models:\n    analyst: fake/none\n"
        "  limits:\n    tokens_per_run: 9\n    minutes_per_task: 1\n    tasks_at_once: 1\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="maintenance: is required"):
        load(overlay_root, library, config)


def test_command_as_string_is_refused(overlay_root: Path, library: Library, config: Config) -> None:
    written(overlay_root, "quarantine: {days: 7}\nverification:\n  python-uv:\n    - uv sync\n")
    with pytest.raises(ConfigError, match="must be a non-empty list of arguments"):
        load(overlay_root, library, config)


def test_exception_without_a_reason_is_refused(
    overlay_root: Path, library: Library, config: Config
) -> None:
    written(
        overlay_root,
        "quarantine: {days: 7}\n"
        "exceptions:\n  - subject: pkg\n    scope: quarantine\n    reason: ''\n",
    )
    with pytest.raises(ConfigError, match="an exception without a reason is an accident"):
        load(overlay_root, library, config)


def test_oversized_notes_warn_rather_than_fail(
    overlay_root: Path, library: Library, config: Config
) -> None:
    (overlay_root / "NOTES.md").write_text("x" * 100, encoding="utf-8")
    overlay = load(overlay_root, library, config, notes_limit=10)
    assert overlay.warnings and "over the 10 limit" in overlay.warnings[0]


def test_digest_covers_both_files(overlay_root: Path, library: Library, config: Config) -> None:
    before = load(overlay_root, library, config).digest
    (overlay_root / "NOTES.md").write_text("# Notes\n\nOne invariant.\n", encoding="utf-8")
    assert load(overlay_root, library, config).digest != before
