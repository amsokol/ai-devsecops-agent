"""The checkout is watched rather than trusted to the backend's sandbox."""

from __future__ import annotations

from pathlib import Path

from agent.containment import Checkout, refusal


def test_a_session_that_edits_the_checkout_is_undone(git_repo: Path, tmp_path: Path) -> None:
    watch = Checkout.of(git_repo)

    (git_repo / "README.md").write_text("edited by a session\n", encoding="utf-8")
    strays = watch.restore(keep=tmp_path / "kept")

    assert [stray.path for stray in strays] == ["README.md"]
    assert (git_repo / "README.md").read_text(encoding="utf-8") == "product\n"


def test_what_was_undone_is_kept_and_not_lost(git_repo: Path, tmp_path: Path) -> None:
    watch = Checkout.of(git_repo)

    (git_repo / "README.md").write_text("edited by a session\n", encoding="utf-8")
    strays = watch.restore(keep=tmp_path / "kept")

    assert Path(strays[0].kept).read_text(encoding="utf-8") == "edited by a session\n"


def test_a_file_a_session_created_is_moved_out_of_the_checkout(
    git_repo: Path, tmp_path: Path
) -> None:
    watch = Checkout.of(git_repo)

    (git_repo / "scratch.txt").write_text("left behind\n", encoding="utf-8")
    strays = watch.restore(keep=tmp_path / "kept")

    assert not (git_repo / "scratch.txt").exists()
    assert Path(strays[0].kept).read_text(encoding="utf-8") == "left behind\n"


def test_work_that_was_already_there_is_left_alone(git_repo: Path, tmp_path: Path) -> None:
    """A run that reverted somebody's uncommitted work would be unforgivable once."""
    (git_repo / "README.md").write_text("a developer was editing this\n", encoding="utf-8")

    watch = Checkout.of(git_repo)
    strays = watch.restore(keep=tmp_path / "kept")

    assert strays == ()
    assert (git_repo / "README.md").read_text(encoding="utf-8") == "a developer was editing this\n"


def test_the_agents_own_directories_are_not_a_breach(git_repo: Path, tmp_path: Path) -> None:
    """A product that does not ignore the run directory would otherwise fail every run."""
    runs = git_repo / ".agent" / "runs"
    watch = Checkout.of(git_repo, mine=(runs,))

    runs.mkdir(parents=True)
    (runs / "manifest.json").write_text("{}\n", encoding="utf-8")
    strays = watch.restore(keep=tmp_path / "kept")

    assert strays == ()
    assert (runs / "manifest.json").exists()


def test_the_refusal_names_the_files_and_what_to_do(git_repo: Path, tmp_path: Path) -> None:
    watch = Checkout.of(git_repo)
    (git_repo / "README.md").write_text("edited by a session\n", encoding="utf-8")

    said = refusal(watch.restore(keep=tmp_path / "kept"))

    assert "README.md" in said
    assert "worktree" in said
