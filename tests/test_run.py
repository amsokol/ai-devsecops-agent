from __future__ import annotations

import json
import subprocess
from pathlib import Path

from agent.cli import main
from agent.domain import Trigger
from agent.errors import ExitCode
from agent.orchestrator import Request, run
from agent.repo import Repository
from agent.scm.fake import FakePlatform


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
    assert manifest["roles"] == [{"role": "analyst", "backend": "fake", "model": "composer-2.5"}]
    assert manifest["library"]["pinned"] is False
    assert any("not pinned" in warning for warning in manifest["warnings"])


def test_a_review_whose_tasks_all_report_clean_passes(
    git_repo: Path, library_root: Path, overlay_root: Path, config_dir: Path, tmp_path: Path
) -> None:
    """The default scripted answer is `clean`, so this is the whole pipeline minus the model."""
    commit(git_repo, "src/api.py", "value = 1\n")
    run_dir = tmp_path / "runs"
    code = main(["review", *arguments(git_repo, library_root, overlay_root, run_dir, config_dir)])
    assert code == int(ExitCode.OK)
    manifest_path = next(run_dir.glob("*/manifest.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["result"] == "pass"
    assert {task["outcome"] for task in manifest["tasks"]} == {"clean"}
    assert manifest["policy"]["blocks"] == [
        "routine/critical",
        "security/critical",
        "security/high",
    ]
    assert manifest["cost"]["accounted_sessions"] == 2
    report = (manifest_path.parent / "report.md").read_text(encoding="utf-8")
    assert "No blocking findings" in report
    assert "code-quality" in report

    prompt = next(run_dir.glob("*/tasks/code-vuln/attempt-1/prompt.md")).read_text(encoding="utf-8")
    assert "## Knowledge" in prompt
    assert "capabilities/code-vuln" in prompt


def test_a_maintenance_run_carries_a_fix_phase_even_when_there_is_nothing_to_fix(
    git_repo: Path, library_root: Path, overlay_root: Path, config_dir: Path, tmp_path: Path
) -> None:
    """A clean branch still records the queue: an empty one is a fact, not an absence of the phase.

    It also asserts the binding check reaches `fixer` before any task starts. Discovering an unbound
    fixer after the analysis would mean a run that reports findings and ships nothing.
    """
    run_dir = tmp_path / "runs"
    code = main(["maintain", *arguments(git_repo, library_root, overlay_root, run_dir, config_dir)])
    assert code == int(ExitCode.OK)
    manifest = json.loads(next(run_dir.glob("*/manifest.json")).read_text(encoding="utf-8"))
    assert [role["role"] for role in manifest["roles"]] == ["analyst", "fixer"]
    assert manifest["remediation"] == {"jobs": [], "deferred": []}
    assert manifest["fixes"] == []


def test_a_review_asked_to_publish_without_a_change_number_stops_before_it_starts(
    git_repo: Path, library_root: Path, overlay_root: Path, config_dir: Path, tmp_path: Path
) -> None:
    """There is no conversation to publish to. Found at startup, before the run is paid for."""
    code = main(
        [
            "review",
            *arguments(git_repo, library_root, overlay_root, tmp_path / "runs", config_dir),
            "--publish",
        ]
    )
    assert code == int(ExitCode.CONFIG)


def test_a_published_review_records_every_thread_it_touched(
    git_repo: Path, library_root: Path, overlay_root: Path, config_dir: Path, tmp_path: Path
) -> None:
    """The run's own account of what it did on the platform, down to the stance of the review."""
    commit(git_repo, "src/api.py", "value = 1\n")
    repository = Repository.open(git_repo)
    platform = FakePlatform(head=repository.head)
    request = Request(
        trigger=Trigger.CHANGE_OPENED,
        repository=git_repo,
        library_path=library_root,
        overlay_path=overlay_root,
        run_dir=tmp_path / "runs",
        config_dir=config_dir,
        base="main",
        change=11,
        publish=True,
    )

    record = run(request, platform=platform)

    assert record.manifest.actions["published"] is True
    assert record.manifest.actions["stance"] == "approve"
    assert platform.reviews[0][1] == record.report
    assert not any("published" in warning for warning in record.manifest.warnings)


def test_a_checkout_with_nowhere_to_publish_still_keeps_its_verdict(
    git_repo: Path, library_root: Path, overlay_root: Path, config_dir: Path, tmp_path: Path
) -> None:
    """No remote, and on this machine probably no credential either. By the time publishing is tried
    the analysis is paid for, so this costs the comments and not the run."""
    commit(git_repo, "src/api.py", "value = 1\n")
    request = Request(
        trigger=Trigger.CHANGE_OPENED,
        repository=git_repo,
        library_path=library_root,
        overlay_path=overlay_root,
        run_dir=tmp_path / "runs",
        config_dir=config_dir,
        base="main",
        change=11,
        publish=True,
    )

    record = run(request)

    assert record.exit_code == int(ExitCode.OK)
    assert record.manifest.result == "pass"
    assert any("nothing was published" in warning for warning in record.manifest.warnings)


def test_a_platform_the_run_cannot_reach_is_a_warning_and_not_a_lost_verdict(
    git_repo: Path, library_root: Path, overlay_root: Path, config_dir: Path, tmp_path: Path
) -> None:
    commit(git_repo, "src/api.py", "value = 1\n")
    platform = FakePlatform(fail="the token cannot see this repository")
    request = Request(
        trigger=Trigger.CHANGE_OPENED,
        repository=git_repo,
        library_path=library_root,
        overlay_path=overlay_root,
        run_dir=tmp_path / "runs",
        config_dir=config_dir,
        base="main",
        change=11,
        publish=True,
    )

    record = run(request, platform=platform)

    assert record.exit_code == int(ExitCode.OK)
    assert any("nothing was published" in warning for warning in record.manifest.warnings)


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
