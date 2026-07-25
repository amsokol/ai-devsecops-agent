"""The change's own diff as a fact, and the limit on what a tool hands to a model.

Both exist because of the same live run. A review reported pins the change never touched and blocked
a merge on one of them, and a task spent 71% of the run's tokens on one registry document that was
too large to parse — so the fact taken from it was heuristic for a reason unrelated to the registry.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from agent.domain import PlannedTask, Role
from agent.repo import ChangeView, Repository
from agent.session import Session
from agent.storage import FactCache
from agent.toolkit import MODEL_PAYLOAD_CHARS, Refused, Toolkit
from agent.tools import Grants
from agent.tools.network import Response

MOMENT = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
TASK = PlannedTask(
    id="deps-outdated@python-uv",
    capability="capabilities/deps-outdated",
    role=Role.ANALYST,
    required=True,
    ecosystem="ecosystems/python-uv",
)
MANIFEST_ON_MAIN = 'dependencies = [\n    "pyyaml==6.0.3",\n    "ruff==0.16.0",\n]\n'
MANIFEST_IN_CHANGE = (
    'dependencies = [\n    "pyyaml==6.0.3",\n    "requests==2.31.0",\n    "ruff==0.16.0",\n]\n'
)


def commit(root: Path, message: str) -> None:
    environment = {
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "PATH": "/usr/bin:/bin",
        "HOME": str(root.parent),
    }
    for arguments in (("add", "-A"), ("commit", "--quiet", "-m", message)):
        subprocess.run(
            ["git", "-C", str(root), *arguments], check=True, capture_output=True, env=environment
        )


def a_change(root: Path) -> ChangeView:
    (root / "pyproject.toml").write_text(MANIFEST_ON_MAIN, encoding="utf-8")
    commit(root, "pin the toolchain")
    (root / "pyproject.toml").write_text(MANIFEST_IN_CHANGE, encoding="utf-8")
    commit(root, "add an upload dependency")
    return ChangeView.of(Repository.open(root), "main~1")


def toolkit_for(root: Path, *, change: ChangeView | None) -> Toolkit:
    session = Session(
        repository=root,
        grants=Grants(binaries=frozenset(), hosts=frozenset({"pypi.org"})),
        cache=FactCache(None, writable=False),
        scratch_root=root.parent / "scratch",
        change=change,
    )
    return Toolkit(session=session, task=TASK, now=MOMENT, quarantine_days=7)


def test_the_change_names_the_lines_it_added(git_repo: Path) -> None:
    kit = toolkit_for(git_repo, change=a_change(git_repo))
    answer = kit.call("read_change", {"path": "pyproject.toml"})

    assert answer["in_change"] is True
    added = [line["text"].strip() for line in answer["added"]]
    assert added == ['"requests==2.31.0",']
    # The line number is the file's own, so a finding can point at something a reader can open.
    assert answer["added"][0]["line"] == 3
    assert answer["removed"] == []


def test_a_file_the_change_left_alone_says_so(git_repo: Path) -> None:
    """The whole point: a pin nobody touched is out of a review's scope, and git says which."""
    kit = toolkit_for(git_repo, change=a_change(git_repo))
    answer = kit.call("read_change", {"path": "README.md"})
    assert answer["in_change"] is False
    assert answer["added"] == []


def test_the_tool_is_absent_when_there_is_no_change(git_repo: Path) -> None:
    kit = toolkit_for(git_repo, change=None)
    assert "read_change" not in {tool.name for tool in kit.tools()}
    # Not offered and not callable: a repository-wide run has no scope to respect.
    with pytest.raises(Refused, match="no tool named"):
        kit.call("read_change", {"path": "pyproject.toml"})


def test_a_path_outside_the_repository_is_refused(git_repo: Path) -> None:
    kit = toolkit_for(git_repo, change=a_change(git_repo))
    with pytest.raises(Refused, match="outside the repository"):
        kit.call("read_change", {"path": "../../etc/hostname"})


def test_an_option_shaped_path_is_not_a_path(git_repo: Path) -> None:
    """Otherwise a model could hand git a flag instead of a file."""
    kit = toolkit_for(git_repo, change=a_change(git_repo))
    with pytest.raises(Refused, match="not a path"):
        kit.call("read_change", {"path": "--output=/tmp/escape"})


@dataclass(frozen=True, slots=True)
class OneResponse:
    """An HTTP client that answers every request with the same body."""

    body: str

    def get(self, url: str) -> Response:
        return Response(url=url, status=200, headers={}, body=self.body, truncated=False)


def with_response(kit: Toolkit, body: str) -> Toolkit:
    kit._tools = replace(kit._tools, http=OneResponse(body))  # type: ignore[arg-type]
    return kit


def test_a_document_too_large_to_read_is_not_handed_over(git_repo: Path) -> None:
    kit = with_response(
        toolkit_for(git_repo, change=None),
        '{"releases": {' + ",".join(f'"1.{n}.0": []' for n in range(20_000)) + "}}",
    )
    answer: dict[str, Any] = kit.call("fetch", {"url": "https://pypi.org/pypi/ruff/json"})

    assert "json" not in answer
    assert str(MODEL_PAYLOAD_CHARS) in answer["not_delivered"]
    assert answer["keys"] == ["releases"]


def test_a_fact_cannot_rest_on_a_document_nobody_read(git_repo: Path) -> None:
    """The cheapest wrong answer: cite a call that returned nothing and call it reproducible."""
    kit = with_response(toolkit_for(git_repo, change=None), '{"info": "' + "x" * 70_000 + '"}')
    answer = kit.call("fetch", {"url": "https://pypi.org/pypi/ruff/json"})

    with pytest.raises(Refused, match="did not succeed"):
        kit.call(
            "record_fact",
            {
                "question": "publish-time",
                "subject": {"package": "ruff", "version": "0.16.0"},
                "value": "2026-07-23T19:10:46Z",
                "calls": [answer["call"]],
            },
        )


def test_a_small_answer_is_still_delivered(git_repo: Path) -> None:
    kit = with_response(toolkit_for(git_repo, change=None), '{"urls": [{"upload_time": "2026"}]}')
    answer = kit.call("fetch", {"url": "https://pypi.org/pypi/ruff/0.16.0/json"})
    assert answer["kind"] == "api"
    assert answer["json"]["urls"][0]["upload_time"] == "2026"


AN_INDEX = (
    '{"info": {"name": "ruff"}, "releases": {'
    + ",".join(f'"1.{n}.0": [{{"filename": "wheel-{"x" * 200}"}}]' for n in range(1_000))
    + "}}"
)


def test_a_version_list_is_the_names_not_the_files(git_repo: Path) -> None:
    """The fix for a task that spent 71% of a run's tokens on file metadata it never needed."""
    kit = with_response(toolkit_for(git_repo, change=None), AN_INDEX)
    answer = kit.call(
        "fetch",
        {"url": "https://pypi.org/pypi/ruff/json", "select": "releases", "keys_only": True},
    )
    assert len(answer["json"]) == 1_000
    assert "1.999.0" in answer["json"]
    assert "filename" not in json.dumps(answer)


def test_a_selection_is_recorded_as_what_was_read(git_repo: Path) -> None:
    kit = with_response(toolkit_for(git_repo, change=None), AN_INDEX)
    kit.call("fetch", {"url": "https://pypi.org/pypi/ruff/json", "select": "info.name"})
    assert kit.calls[-1].source.endswith("/pypi/ruff/json#info.name")


def test_a_path_that_is_not_there_says_what_is(git_repo: Path) -> None:
    kit = with_response(toolkit_for(git_repo, change=None), AN_INDEX)
    with pytest.raises(Refused, match="releases"):
        kit.call("fetch", {"url": "https://pypi.org/pypi/ruff/json", "select": "versions"})


def test_keys_only_needs_something_with_keys(git_repo: Path) -> None:
    kit = with_response(toolkit_for(git_repo, change=None), AN_INDEX)
    with pytest.raises(Refused, match="only an object has keys"):
        kit.call(
            "fetch",
            {"url": "https://pypi.org/pypi/ruff/json", "select": "info.name", "keys_only": True},
        )
