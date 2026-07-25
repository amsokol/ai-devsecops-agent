from __future__ import annotations

from pathlib import Path

import pytest
from agent.config import Config
from agent.errors import ConfigError
from agent.library import Library
from agent.overlay import Overlay


def load(root: Path, library: Library, config: Config, *, notes_limit: int = 8000) -> Overlay:
    return Overlay.load(
        root,
        library=library,
        default_limits=config.maintenance_limits,
        notes_limit=notes_limit,
    )


def test_values_are_read(overlay: Overlay) -> None:
    assert overlay.ecosystems == ("ecosystems/python-uv",)
    assert overlay.quarantine_days == 7
    assert overlay.verification["python-uv"] == (("uv", "sync", "--frozen"),)
    assert overlay.limits.open_change_requests == 3


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
    (overlay_root / "agent.yaml").write_text(
        "schema: 1\necosystems: [ecosystems/rust]\nquarantine: {days: 7}\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="available: ecosystems/github-actions"):
        load(overlay_root, library, config)


def test_quarantine_is_required_because_it_must_never_be_invented(
    overlay_root: Path, library: Library, config: Config
) -> None:
    (overlay_root / "agent.yaml").write_text("schema: 1\necosystems: []\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="quarantine: is required"):
        load(overlay_root, library, config)


def test_command_as_string_is_refused(overlay_root: Path, library: Library, config: Config) -> None:
    (overlay_root / "agent.yaml").write_text(
        "schema: 1\nquarantine: {days: 7}\nverification:\n  python-uv:\n    - uv sync\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="must be a non-empty list of arguments"):
        load(overlay_root, library, config)


def test_exception_without_a_reason_is_refused(
    overlay_root: Path, library: Library, config: Config
) -> None:
    (overlay_root / "agent.yaml").write_text(
        "schema: 1\nquarantine: {days: 7}\n"
        "exceptions:\n  - subject: pkg\n    scope: quarantine\n    reason: ''\n",
        encoding="utf-8",
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
