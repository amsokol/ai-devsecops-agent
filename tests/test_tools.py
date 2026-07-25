"""Deterministic tools: versions, dates, files, commands, hosts."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from agent.errors import ConfigError
from agent.library import Library
from agent.tools import (
    Ceiling,
    CommandRunner,
    Comparison,
    FileTools,
    Grants,
    HostNotPermitted,
    HttpClient,
    NotPermitted,
    OutsideRepository,
    Requirements,
    Step,
    compare_versions,
    grant,
    quarantine,
)
from agent.tools.versions import UNORDERED

PYTHON = "ecosystems/python-uv"
NPM = "ecosystems/npm"


@pytest.mark.parametrize(
    ("ecosystem", "left", "right", "order", "step"),
    [
        (NPM, "1.2.3", "1.2.3", 0, Step.SAME),
        (NPM, "2.0.0", "1.9.9", 1, Step.MAJOR),
        (NPM, "1.3.0", "1.2.9", 1, Step.MINOR),
        (NPM, "1.2.3", "1.2.4", -1, Step.PATCH),
        (NPM, "1.0.0-rc.1", "1.0.0", -1, Step.PRERELEASE),
        (NPM, "v1.2.3", "1.2.3", 0, Step.SAME),
        (NPM, "1.2", "1.2.0", 0, Step.SAME),
        (PYTHON, "1.2.3", "1.2.3", 0, Step.SAME),
        (PYTHON, "2026.1", "2025.12", 1, Step.MAJOR),
        (PYTHON, "1.0.0rc1", "1.0.0", -1, Step.PRERELEASE),
        (PYTHON, "1.0.0.post1", "1.0.0", 1, Step.PRERELEASE),
        (PYTHON, "1.0.0.dev3", "1.0.0", -1, Step.PRERELEASE),
        (PYTHON, "0.28.1", "0.28.0", 1, Step.PATCH),
    ],
)
def test_version_ordering(ecosystem: str, left: str, right: str, order: int, step: Step) -> None:
    assert compare_versions(ecosystem, left, right) == Comparison(order=order, step=step)


def test_an_uncomparable_version_is_not_guessed() -> None:
    assert compare_versions(NPM, "main", "1.0.0") is UNORDERED
    assert compare_versions(NPM, "1.0.0", "latest").unordered


def test_quarantine_is_answered_from_an_explicit_clock() -> None:
    published = datetime(2026, 7, 1, tzinfo=UTC)

    waiting = quarantine(published, days=7, now=datetime(2026, 7, 5, tzinfo=UTC))
    cleared = quarantine(published, days=7, now=datetime(2026, 7, 9, tzinfo=UTC))

    assert not waiting.cleared
    assert waiting.clears_at == datetime(2026, 7, 8, tzinfo=UTC)
    assert "clears ~2026-07-08" in waiting.phrase()
    assert cleared.cleared
    assert cleared.phrase().endswith("cleared")


def test_a_heuristic_date_clears_only_when_unambiguous() -> None:
    published = datetime(2026, 7, 1, tzinfo=UTC)
    just_past = datetime(2026, 7, 8, 1, tzinfo=UTC)

    assert quarantine(published, days=7, now=just_past).cleared
    assert not quarantine(published, days=7, now=just_past, margin_days=1).cleared


def test_a_naive_timestamp_is_read_as_utc() -> None:
    assert quarantine(datetime(2026, 7, 1), days=7, now=datetime(2026, 7, 9, tzinfo=UTC)).cleared


def test_files_stay_inside_the_repository(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("import os\nprint(os.name)\n", encoding="utf-8")
    (tmp_path.parent / "secret.txt").write_text("token\n", encoding="utf-8")
    files = FileTools(root=tmp_path)

    assert "import os" in files.read_file("src/app.py")
    assert files.list_files("**/*.py") == ("src/app.py",)
    assert [match.line for match in files.search_text(r"^import")] == [1]
    with pytest.raises(OutsideRepository):
        files.read_file("../secret.txt")


def test_reading_a_large_file_is_truncated_rather_than_refused(tmp_path: Path) -> None:
    (tmp_path / "big.txt").write_text("x" * 100, encoding="utf-8")

    text = FileTools(root=tmp_path).read_file("big.txt", limit=10)

    assert text.startswith("x" * 10)
    assert "truncated" in text


def runner(tmp_path: Path, *binaries: str) -> CommandRunner:
    return CommandRunner(
        grants=Grants(binaries=frozenset(binaries), hosts=frozenset()),
        workdir=tmp_path,
        scratch=tmp_path / "scratch",
    )


def test_only_granted_binaries_run(tmp_path: Path) -> None:
    with pytest.raises(NotPermitted, match="not permitted"):
        runner(tmp_path, "git").run(("curl", "https://example.com"))


def test_a_granted_binary_runs_without_a_shell(tmp_path: Path) -> None:
    result = runner(tmp_path, "git").run(("git", "--version"))

    assert result.succeeded
    assert result.stdout.startswith("git version")


def test_a_shell_expression_is_not_interpreted(tmp_path: Path) -> None:
    result = runner(tmp_path, "git").run(("git", "--version; echo pwned"))

    assert not result.succeeded
    assert "pwned" not in result.stdout


def test_a_hanging_command_is_stopped(tmp_path: Path) -> None:
    result = runner(tmp_path, "sleep").run(("sleep", "30"), timeout=1)

    assert result.timed_out
    assert result.exit_code is None


def test_hosts_are_checked_before_any_request_leaves() -> None:
    client = HttpClient(grants=Grants(binaries=frozenset(), hosts=frozenset({"pypi.org"})))

    with pytest.raises(HostNotPermitted, match="not permitted"):
        client.get("https://evil.example/pypi/httpx/json")
    with pytest.raises(HostNotPermitted, match="not https"):
        client.get("http://pypi.org/pypi/httpx/json")


def test_requirements_are_read_from_the_ecosystem_document(library: Library) -> None:
    document = library.get(PYTHON)

    assert Requirements.of(document) == Requirements(
        ecosystem=PYTHON, binaries=frozenset({"uv"}), hosts=frozenset({"pypi.org"})
    )


def test_a_wrapped_bullet_is_still_one_requirement(library: Library) -> None:
    path = library.root / "ecosystems/python-uv.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "- Binaries: `uv`.",
            "- Binaries: `uv`, and `pip-audit` for advisories; `python` when\n  a probe needs it.",
        ),
        encoding="utf-8",
    )
    reloaded = Library.load(library.root, agent_version="0.1.0")

    assert Requirements.of(reloaded.get(PYTHON)).binaries == frozenset(
        {"uv", "pip-audit", "python"}
    )


def test_a_document_cannot_grant_itself_more_than_the_ceiling(library: Library) -> None:
    ceiling = Ceiling(binaries=frozenset({"uv"}), hosts=frozenset({"pypi.org"}))
    path = library.root / "ecosystems/python-uv.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace("- Binaries: `uv`.", "- Binaries: `uv`, `curl`."),
        encoding="utf-8",
    )
    reloaded = Library.load(library.root, agent_version="0.1.0")

    with pytest.raises(ConfigError, match="outside the agent's ceiling"):
        grant(library=reloaded, ecosystems=(PYTHON,), ceiling=ceiling)


def test_granting_covers_only_the_enabled_ecosystems(library: Library) -> None:
    ceiling = Ceiling(
        binaries=frozenset({"uv", "gh"}), hosts=frozenset({"pypi.org", "api.github.com"})
    )

    grants = grant(library=library, ecosystems=(PYTHON,), ceiling=ceiling)

    assert grants.allows_binary("uv")
    assert not grants.allows_binary("gh")
    assert grants.allows_host("PyPI.org".lower())
    assert not grants.allows_host("api.github.com")


def test_the_ceiling_may_use_a_wildcard_for_a_registry_family() -> None:
    ceiling = Ceiling(binaries=frozenset(), hosts=frozenset({"*.crates.io"}))

    assert ceiling.allows_host("index.crates.io")
    assert ceiling.allows_host("crates.io")
    assert not ceiling.allows_host("crates.io.evil.example")
