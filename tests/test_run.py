from __future__ import annotations

import json
import subprocess
from pathlib import Path

from agent.cli import main
from agent.errors import ExitCode


def commit(repo: Path, name: str, content: str) -> None:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    env = {
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "PATH": "/usr/bin:/bin",
        "HOME": str(repo.parent),
    }
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "--quiet", "-B", "change"], check=True, env=env
    )
    subprocess.run(["git", "-C", str(repo), "add", name], check=True, env=env, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "--quiet", "-m", f"add {name}"],
        check=True,
        env=env,
        capture_output=True,
    )


def arguments(
    repo: Path,
    library_root: Path,
    overlay_root: Path,
    run_dir: Path,
    config_dir: Path | None = None,
) -> list[str]:
    options = [
        "--repo",
        str(repo),
        "--library",
        str(library_root),
        "--overlay",
        str(overlay_root),
        "--run-dir",
        str(run_dir),
    ]
    if config_dir is not None:
        options += ["--config-dir", str(config_dir)]
    return options


def test_the_shipped_pin_refuses_a_library_it_does_not_name(
    git_repo: Path, library_root: Path, overlay_root: Path, tmp_path: Path
) -> None:
    """No --config-dir here on purpose: this asserts what a release actually enforces."""
    code = main(["maintain", *arguments(git_repo, library_root, overlay_root, tmp_path / "runs")])
    assert code == int(ExitCode.CONFIG)


def test_plan_only_review_succeeds_and_records_a_manifest(
    git_repo: Path, library_root: Path, overlay_root: Path, config_dir: Path, tmp_path: Path
) -> None:
    commit(git_repo, "src/api.py", "value = 1\n")
    run_dir = tmp_path / "runs"
    code = main(
        [
            "review",
            *arguments(git_repo, library_root, overlay_root, run_dir, config_dir),
            "--plan-only",
        ]
    )
    assert code == int(ExitCode.OK)
    manifest = json.loads(next(run_dir.glob("*/manifest.json")).read_text(encoding="utf-8"))
    assert manifest["result"] == "planned"
    assert [task["id"] for task in manifest["tasks"]] == ["code-quality", "code-vuln"]
    assert manifest["library"]["pinned"] is False
    assert any("not pinned" in warning for warning in manifest["warnings"])


def test_a_run_that_executed_nothing_is_inconclusive(
    git_repo: Path, library_root: Path, overlay_root: Path, config_dir: Path, tmp_path: Path
) -> None:
    commit(git_repo, "src/api.py", "value = 1\n")
    run_dir = tmp_path / "runs"
    code = main(["review", *arguments(git_repo, library_root, overlay_root, run_dir, config_dir)])
    assert code == int(ExitCode.INCONCLUSIVE)
    manifest = json.loads(next(run_dir.glob("*/manifest.json")).read_text(encoding="utf-8"))
    assert manifest["result"] == "inconclusive"
    assert {task["reason"] for task in manifest["tasks"]} == {"not-implemented"}


def test_a_broken_overlay_stops_the_run_before_any_work(
    git_repo: Path, library_root: Path, overlay_root: Path, config_dir: Path, tmp_path: Path
) -> None:
    (overlay_root / "agent.yaml").write_text(
        "schema: 1\nquarantine: {days: -1}\n", encoding="utf-8"
    )
    code = main(
        [
            "maintain",
            *arguments(git_repo, library_root, overlay_root, tmp_path / "runs", config_dir),
        ]
    )
    assert code == int(ExitCode.CONFIG)


def test_explain_prints_a_recorded_run(
    git_repo: Path, library_root: Path, overlay_root: Path, config_dir: Path, tmp_path: Path
) -> None:
    run_dir = tmp_path / "runs"
    main(
        [
            "maintain",
            *arguments(git_repo, library_root, overlay_root, run_dir, config_dir),
            "--plan-only",
        ]
    )
    identifier = next(run_dir.iterdir()).name
    assert main(["explain", "--run", identifier, "--run-dir", str(run_dir)]) == int(ExitCode.OK)
