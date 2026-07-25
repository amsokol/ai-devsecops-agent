"""Fixtures: a miniature knowledge library and overlay, built in a temporary directory.

The tests deliberately do not read the real library: the planner's behaviour must be provable
against a known small input, and the real library changes for reasons unrelated to these tests.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from agent.config import BUILTIN_CONFIG_DIR, Config
from agent.library import Library
from agent.overlay import Overlay

LIBRARY_YAML = """\
schema: 1
version: 0.1.0
contract_version: 1
min_agent_version: 0.1.0
"""

INDEX = """\
# Index

| id | kind | summary | applies_to |
| --- | --- | --- | --- |
| `playbooks/pr-review` | playbook | Review a change. | — |
| `playbooks/maintain` | playbook | Maintain the branch. | — |
| `capabilities/code-quality` | capability | Correctness risks. | — |
| `capabilities/code-vuln` | capability | Security defects. | — |
| `capabilities/deps-outdated` | capability | Version drift. | — |
| `capabilities/deps-vuln` | capability | Known vulnerabilities. | — |
| `policy/verdicts` | policy | Blocking rights. | — |
| `ecosystems/python-uv` | ecosystem | uv-managed Python. | `pyproject.toml`, `uv.lock` |
| `ecosystems/github-actions` | ecosystem | Workflow pins. | `.github/workflows` |
"""

# The blocking table is parsed out of this document, so the fixture has to carry a real one. Its
# values mirror the shipped library on purpose: a test that quietly used a different policy would
# prove nothing about the agent people run.
VERDICTS = """\
Classes, severities and what blocks.

## What blocks

| Class | Severity | Blocks |
| --- | --- | --- |
| `security` | `critical`, `high` | yes |
| `security` | `medium`, `low` | no — comment |
| `routine` | `critical` | yes |
| `routine` | `high`, `medium`, `low` | no — comment |
| forbidden state | any | yes |

## Evidence ceiling

Reproducible evidence may block; heuristic evidence may only comment.
"""

DOCUMENTS = {
    "playbooks/pr-review": "Review the change. See [verdicts](../policy/verdicts.md).\n",
    "playbooks/maintain": "Maintain, never judge changes: see [review](pr-review.md).\n",
    "capabilities/code-quality": "Look for correctness risks.\n",
    "capabilities/code-vuln": "Look for security defects.\n",
    "capabilities/deps-outdated": "Look for drift. See [verdicts](../policy/verdicts.md).\n",
    "capabilities/deps-vuln": "Look for advisories.\n",
    "policy/verdicts": VERDICTS,
    "ecosystems/python-uv": (
        "Use uv.\n\n## Requirements\n\n- Binaries: `uv`.\n- Hosts: `pypi.org`.\n\n## Detect\n\n"
        "A `uv.lock` in the tree.\n"
    ),
    "ecosystems/github-actions": (
        "Pin actions.\n\n## Requirements\n\n- Binaries: `gh`.\n- Hosts: `api.github.com`.\n"
    ),
}

OVERLAY_VALUES = """\
schema: 1
ecosystems:
  - ecosystems/python-uv
hotspots:
  - src
quarantine:
  days: 7
verification:
  python-uv:
    - [uv, sync, --frozen]
"""


@pytest.fixture
def library_root(tmp_path: Path) -> Path:
    root = tmp_path / "library"
    root.mkdir()
    (root / "library.yaml").write_text(LIBRARY_YAML, encoding="utf-8")
    (root / "INDEX.md").write_text(INDEX, encoding="utf-8")
    for doc_id, body in DOCUMENTS.items():
        path = root / f"{doc_id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        kind = doc_id.split("/", 1)[0].rstrip("s")
        header = f"---\nid: {doc_id}\nkind: {kind}\nsummary: Test document.\n---\n\n"
        path.write_text(header + body, encoding="utf-8")
    return root


@pytest.fixture
def library(library_root: Path) -> Library:
    return Library.load(library_root, agent_version="0.1.0")


@pytest.fixture
def config() -> Config:
    return Config.load()


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """The shipped configuration, with the library pin removed and the backend replaced.

    The pin names the real library and these tests run against a fixture one; the backend would call
    a model. Everything else — scenarios, ceiling, limits, budgets — stays exactly as it ships, so a
    test still exercises what a release does.
    """
    directory = tmp_path / "config"
    shutil.copytree(BUILTIN_CONFIG_DIR, directory)
    (directory / "library.yaml").write_text("version:\ndigest:\n", encoding="utf-8")
    execution = (directory / "execution.yaml").read_text(encoding="utf-8")
    (directory / "execution.yaml").write_text(
        execution.replace("backend: cursor", "backend: fake"), encoding="utf-8"
    )
    return directory


@pytest.fixture
def overlay_root(tmp_path: Path) -> Path:
    root = tmp_path / "overlay"
    root.mkdir()
    (root / "agent.yaml").write_text(OVERLAY_VALUES, encoding="utf-8")
    (root / "NOTES.md").write_text("# Notes\n\nNothing yet.\n", encoding="utf-8")
    return root


@pytest.fixture
def overlay(overlay_root: Path, library: Library, config: Config) -> Overlay:
    return Overlay.load(
        overlay_root,
        library=library,
        default_limits=config.maintenance_limits,
        notes_limit=config.notes_limit,
    )


@pytest.fixture
def git_repo(tmp_path: Path) -> Iterator[Path]:
    root = tmp_path / "product"
    root.mkdir()

    def git(*arguments: str) -> None:
        subprocess.run(
            ["git", "-C", str(root), *arguments],
            check=True,
            capture_output=True,
            env={
                "GIT_AUTHOR_NAME": "test",
                "GIT_AUTHOR_EMAIL": "test@example.com",
                "GIT_COMMITTER_NAME": "test",
                "GIT_COMMITTER_EMAIL": "test@example.com",
                "PATH": "/usr/bin:/bin",
                "HOME": str(tmp_path),
            },
        )

    git("init", "--initial-branch", "main", "--quiet")
    (root / "README.md").write_text("product\n", encoding="utf-8")
    git("add", "README.md")
    git("commit", "--quiet", "-m", "initial")
    yield root
