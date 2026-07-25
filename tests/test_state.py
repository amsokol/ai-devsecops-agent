"""The memory between runs: a document in a ref, written by plumbing and read defensively."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import cast

import pytest
from agent.repo import Repository
from agent.scm.port import Platform, ScmError
from agent.state import FILE, Memory

REF = "refs/agent/state"
ENVIRONMENT = {
    "PATH": "/usr/bin:/bin",
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def git(path: Path, *arguments: str, stdin: str | None = None) -> str:
    finished = subprocess.run(
        ["git", "-C", str(path), *arguments],
        input=stdin,
        check=True,
        capture_output=True,
        text=True,
        env=ENVIRONMENT | {"HOME": str(path)},
    )
    return finished.stdout


class Pusher:
    """A platform that pushes for real, which is the only way to test the round trip.

    The credential and the built URL are the GitHub adapter's part. What this file is about is a ref
    that travels from a local commit to the remote and back, and a local remote shows that.
    """

    def __init__(self, *, fail: str = "") -> None:
        self.fail = fail
        self.pushes: list[tuple[str, str]] = []

    def push(self, path: Path, *, source: str, target: str) -> None:
        self.pushes.append((source, target))
        if self.fail:
            raise ScmError(self.fail)
        git(path, "push", "--quiet", "origin", f"{source}:{target}")


def platform_of(pusher: Pusher) -> Platform:
    # Pushing is all of the platform this path uses, so a stand-in for the rest would only be a way
    # for a test to disagree with the port later.
    return cast("Platform", pusher)


@pytest.fixture
def checkout(git_repo: Path, tmp_path: Path) -> Repository:
    """A repository with somewhere to push to, as any real checkout has."""
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "--quiet", str(origin))
    git(git_repo, "remote", "add", "origin", str(origin))
    git(git_repo, "push", "--quiet", "origin", "main")
    return Repository.open(git_repo)


def hand_write(checkout: Repository, content: str) -> None:
    """Put something the agent did not write at the ref, as an older version or a person would."""
    blob = git(checkout.path, "hash-object", "-w", "--stdin", stdin=content).strip()
    tree = git(checkout.path, "mktree", stdin=f"100644 blob {blob}\t{FILE}\n").strip()
    commit = git(checkout.path, "commit-tree", tree, "-m", "hand written").strip()
    git(checkout.path, "push", "--quiet", "origin", f"{commit}:{REF}")


def test_a_repository_that_never_stored_anything_remembers_nothing(checkout: Repository) -> None:
    """The first run is the normal case, not an error to report."""
    assert Memory(repository=checkout, ref=REF).read() == {}


def test_what_one_run_stores_the_next_one_reads(checkout: Repository) -> None:
    memory = Memory(repository=checkout, ref=REF)
    pusher = Pusher()
    document = {"failures": {"capabilities/deps-vuln:failure:unavailable": {"runs": 1}}}

    stored, failure = memory.write(document, platform=platform_of(pusher), run="run-1")

    assert (stored, failure) == (True, "")
    assert pusher.pushes[0][1] == REF
    assert memory.read() == document


def test_the_second_write_moves_the_ref_forward_and_keeps_the_first(checkout: Repository) -> None:
    """Chained rather than replaced, so the ref never needs a force push and what the agent believed
    last week is there to read when a decision it made looks wrong."""
    memory = Memory(repository=checkout, ref=REF)
    pusher = Pusher()
    memory.write({"runs": 1}, platform=platform_of(pusher), run="run-1")

    memory.write({"runs": 2}, platform=platform_of(pusher), run="run-2")

    assert memory.read() == {"runs": 2}
    assert git(checkout.path, "log", "--format=%s", REF).splitlines() == [
        "agent: state after run run-2",
        "agent: state after run run-1",
    ]


def test_a_week_with_nothing_new_writes_nothing(checkout: Repository) -> None:
    """The same restraint the rest of the scheduled run obeys. A commit a week repeating last week's
    document is churn in somebody else's repository, for a run whose whole point is to be silent."""
    memory = Memory(repository=checkout, ref=REF)
    pusher = Pusher()
    memory.write({"failures": {}}, platform=platform_of(pusher), run="run-1")
    memory.read()

    stored, failure = memory.write({"failures": {}}, platform=platform_of(pusher), run="run-2")

    assert (stored, failure) == (False, "")
    assert len(pusher.pushes) == 1
    assert git(checkout.path, "log", "--format=%s", REF).splitlines() == [
        "agent: state after run run-1"
    ]


def test_the_working_tree_is_untouched_by_a_write(checkout: Repository) -> None:
    """A fix task may be looking at that tree. The state lives in a commit no branch points at."""
    memory = Memory(repository=checkout, ref=REF)

    memory.write({"runs": 1}, platform=platform_of(Pusher()), run="run-1")

    assert git(checkout.path, "status", "--porcelain") == ""
    assert not (checkout.path / FILE).exists()
    assert git(checkout.path, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"


def test_the_memory_is_committed_as_the_agent_and_never_as_a_person(checkout: Repository) -> None:
    memory = Memory(repository=checkout, ref=REF)

    memory.write({"runs": 1}, platform=platform_of(Pusher()), run="run-1")

    assert git(checkout.path, "log", "-1", "--format=%an <%ae>", REF).strip() == (
        "ai-devsecops-agent <ai-devsecops-agent@users.noreply.github.com>"
    )


def test_a_push_that_is_refused_is_reported_rather_than_raised(checkout: Repository) -> None:
    """A memory that could not be stored costs one week of escalation. The run it happened in is
    otherwise complete: its issues are written and its verdict stands, so it must not end here."""
    memory = Memory(repository=checkout, ref=REF)

    stored, failure = memory.write(
        {"runs": 1}, platform=platform_of(Pusher(fail="not a fast-forward")), run="run-1"
    )

    assert (stored, failure) == (False, "not a fast-forward")
    assert memory.read() == {}


@pytest.mark.parametrize("content", ["{{ not json", json.dumps([1, 2, 3]), ""])
def test_a_document_that_makes_no_sense_is_read_as_no_memory(
    checkout: Repository, content: str
) -> None:
    """Invalid JSON, valid JSON of the wrong shape, and nothing at all — what a hand edit or an
    older version of this agent leaves behind. A crash here would fail every scheduled run."""
    hand_write(checkout, content)

    assert Memory(repository=checkout, ref=REF).read() == {}
