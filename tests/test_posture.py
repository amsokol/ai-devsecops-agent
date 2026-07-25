"""What a run may do with the code it is reading.

The property under test is one sentence: a change from outside this repository is read and never
executed. It is worth this much testing because the failure is silent and total — a review job holds
a platform credential and a model key, and one `postinstall` in a stranger's lockfile is enough to
take both. Nothing in a report would look wrong afterwards.

So both halves are asserted. That the tool is gone, and that a session which asks for it anyway is
refused rather than quietly given a shell; and, on a colleague's branch, that none of this restraint
applies, because a gate that stops running the product's own scanners is a gate nobody keeps.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from agent.backends.fake import FakeBackend
from agent.backends.port import Brief
from agent.domain import PlannedTask, Role, Trigger
from agent.errors import ExitCode
from agent.orchestrator import Request, RunRecord, run
from agent.repo import Repository
from agent.scm.fake import FakePlatform
from agent.session import Session
from agent.storage import FactCache
from agent.toolkit import Refused, Toolkit
from agent.tools import Grants

OUTSIDE = "stranger/product"
TASK = PlannedTask(
    id="code-vuln",
    capability="capabilities/code-vuln",
    role=Role.ANALYST,
    required=True,
)


def commit(repo: Path, name: str, content: str, *, branch: str = "change") -> None:
    """A file on a branch, so the review has a diff to work from."""
    (repo / name).parent.mkdir(parents=True, exist_ok=True)
    (repo / name).write_text(content, encoding="utf-8")
    env = {
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "PATH": "/usr/bin:/bin",
        "HOME": str(repo.parent),
    }
    for arguments in (
        ("checkout", "--quiet", "-B", branch),
        ("add", name),
        ("commit", "--quiet", "-m", f"add {name}"),
    ):
        subprocess.run(
            ["git", "-C", str(repo), *arguments], check=True, env=env, capture_output=True
        )


def prying() -> tuple[FakeBackend, list[str]]:
    """A session that reaches for a command anyway, and the refusals it collected.

    Scripted because that is the case that matters. A model told in its prompt that commands are
    unavailable may still try one — from habit, or because something in the change it is reading
    suggested it — and what happens then is the whole guarantee.
    """
    refused: list[str] = []

    def act(brief: Brief) -> None:
        try:
            brief.toolkit.call("run_command", {"command": ["uv", "sync", "--frozen"]})
        except Refused as error:
            refused.append(str(error))

    return FakeBackend(on_execute=act), refused


def reviewing(
    repo: Path,
    library_root: Path,
    overlay_root: Path,
    config_dir: Path,
    tmp_path: Path,
    *,
    change: int | None = 11,
    outside: bool = False,
) -> Request:
    return Request(
        trigger=Trigger.CHANGE_OPENED,
        repository=repo,
        library_path=library_root,
        overlay_path=overlay_root,
        run_dir=tmp_path / "runs",
        config_dir=config_dir,
        base="main",
        change=change,
        publish=change is not None,
        outside=outside,
    )


def prompt_of(record: RunRecord, task: str = "code-vuln") -> str:
    attempt = Path(record.manifest.tasks[0].attempts[0]["result_path"]).parent
    return (attempt.parent.parent / task / "attempt-1" / "prompt.md").read_text(encoding="utf-8")


def test_a_change_from_a_fork_is_read_and_nothing_in_it_is_run(
    git_repo: Path, library_root: Path, overlay_root: Path, config_dir: Path, tmp_path: Path
) -> None:
    """The whole slice, end to end: the tool is absent, the attempt is refused, the run says so."""
    commit(git_repo, "src/api.py", "value = 1\n")
    repository = Repository.open(git_repo)
    backend, refused = prying()

    record = run(
        reviewing(git_repo, library_root, overlay_root, config_dir, tmp_path),
        platform=FakePlatform(head=repository.head, fork=OUTSIDE),
        backend=backend,
    )

    assert record.manifest.posture["executes"] is False
    assert record.manifest.posture["head"] == "outside"
    assert OUTSIDE in record.manifest.posture["detail"]
    # Every task tried, and every task was told no. Not "the first one was".
    assert len(refused) == len(record.manifest.tasks)
    assert all("comes from outside the repository" in message for message in refused)
    prompt = prompt_of(record)
    assert "`run_command`" not in prompt
    assert "No command may be run in this task" in prompt
    assert "record_gap" in prompt
    # The verdict still stands, and the reader is told what it rests on.
    assert record.exit_code == int(ExitCode.OK)
    assert "Nothing in this change was executed" in record.report
    assert any("nothing was executed" in warning for warning in record.manifest.warnings)


def test_a_branch_in_this_repository_is_reviewed_with_its_commands(
    git_repo: Path, library_root: Path, overlay_root: Path, config_dir: Path, tmp_path: Path
) -> None:
    """The restraint is about where the code came from and nothing else.

    A colleague's branch is the ordinary case, and it keeps the scanners. Asserted so that the fork
    test above cannot be satisfied by an agent that stopped running commands altogether.
    """
    commit(git_repo, "src/api.py", "value = 1\n")
    repository = Repository.open(git_repo)

    record = run(
        reviewing(git_repo, library_root, overlay_root, config_dir, tmp_path),
        platform=FakePlatform(head=repository.head),
    )

    assert record.manifest.posture == {
        "head": "own",
        "executes": True,
        "detail": "the head is a branch in this repository",
    }
    prompt = prompt_of(record)
    assert "`run_command`" in prompt
    assert "No command may be run" not in prompt
    assert "Nothing in this change was executed" not in record.report


def test_a_head_the_platform_will_not_place_is_treated_as_somebody_else_s(
    git_repo: Path, library_root: Path, overlay_root: Path, config_dir: Path, tmp_path: Path
) -> None:
    """Fail-closed, because the alternative is an attacker who only has to break one API call."""
    commit(git_repo, "src/api.py", "value = 1\n")
    backend, refused = prying()

    record = run(
        reviewing(git_repo, library_root, overlay_root, config_dir, tmp_path),
        platform=FakePlatform(fail="the token cannot see this repository"),
        backend=backend,
    )

    assert record.manifest.posture["head"] == "unknown"
    assert record.manifest.posture["executes"] is False
    assert refused
    assert record.exit_code == int(ExitCode.OK)


def test_a_run_that_names_no_change_is_the_repository_s_own_work(
    git_repo: Path, library_root: Path, overlay_root: Path, config_dir: Path, tmp_path: Path
) -> None:
    """A maintenance run builds the project to verify a fix. There is no version of that check which
    executes nothing, so this posture is deliberately unrestrained."""
    record = run(
        Request(
            trigger=Trigger.MAINTAIN_SCHEDULED,
            repository=git_repo,
            library_path=library_root,
            overlay_path=overlay_root,
            run_dir=tmp_path / "runs",
            config_dir=config_dir,
        )
    )

    assert record.manifest.posture["executes"] is True
    assert record.manifest.posture["head"] == "own"


def test_the_flag_can_only_take_permission_away(
    git_repo: Path, library_root: Path, overlay_root: Path, config_dir: Path, tmp_path: Path
) -> None:
    """`--outside` overrules a platform that says the head is ours. There is no flag the other way:
    asserting that a stranger's code is safe to run is not a claim a command line can make."""
    commit(git_repo, "src/api.py", "value = 1\n")
    repository = Repository.open(git_repo)

    record = run(
        reviewing(git_repo, library_root, overlay_root, config_dir, tmp_path, outside=True),
        platform=FakePlatform(head=repository.head),
    )

    assert record.manifest.posture["head"] == "outside"
    assert "told to treat the head as outside code" in record.manifest.posture["detail"]


def test_the_posture_is_in_the_record_of_every_run(
    git_repo: Path, library_root: Path, overlay_root: Path, config_dir: Path, tmp_path: Path
) -> None:
    """Written to the manifest on disk, not just held in memory: "it ran nothing over that fork" is
    a claim somebody will want to check next month, and this is the only place it is kept."""
    commit(git_repo, "src/api.py", "value = 1\n")
    run_dir = tmp_path / "runs"
    repository = Repository.open(git_repo)

    run(
        reviewing(git_repo, library_root, overlay_root, config_dir, tmp_path),
        platform=FakePlatform(head=repository.head, fork=OUTSIDE),
    )

    written = json.loads(next(run_dir.glob("*/manifest.json")).read_text(encoding="utf-8"))
    assert written["posture"] == {
        "head": "outside",
        "executes": False,
        "detail": f"the head lives in {OUTSIDE}",
    }


def restrained(tmp_path: Path, *, executes: bool) -> Toolkit:
    return Toolkit(
        session=Session(
            repository=tmp_path,
            grants=Grants(binaries=frozenset({"uv"}), hosts=frozenset({"pypi.org"})),
            cache=FactCache(None, writable=False),
            scratch_root=tmp_path / "scratch",
            never_send=(),
        ),
        task=TASK,
        now=datetime(2026, 7, 25, 12, 0, tzinfo=UTC),
        quarantine_days=7,
        executes=executes,
    )


def test_a_restrained_toolkit_has_no_command_and_says_what_to_do_instead(tmp_path: Path) -> None:
    kit = restrained(tmp_path, executes=False)

    assert "run_command" not in {tool.name for tool in kit.tools()}
    assert "read_file" in {tool.name for tool in kit.tools()}
    with pytest.raises(Refused, match="outside the repository"):
        kit.call("run_command", {"command": ["uv", "sync"]})
    assert any("record_gap" in line for line in kit.caveats)


def test_an_unrestrained_toolkit_carries_the_command_and_no_caveat(tmp_path: Path) -> None:
    kit = restrained(tmp_path, executes=True)

    assert "run_command" in {tool.name for tool in kit.tools()}
    assert kit.caveats == ()
