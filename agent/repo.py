"""Facts about the target repository, obtained from git rather than guessed."""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

from agent.errors import ConfigError

_TIMEOUT_SECONDS = 30


def _git(repo: Path, *arguments: str) -> str:
    located = shutil.which("git")
    if located is None:
        raise ConfigError("git is not available on PATH")
    try:
        completed = subprocess.run(  # noqa: S603 - fixed binary, no shell, arguments are ours
            [located, "-C", str(repo), *arguments],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise ConfigError(
            f"git {' '.join(arguments)} timed out after {_TIMEOUT_SECONDS}s"
        ) from None
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ConfigError(f"git {' '.join(arguments)} failed: {detail}")
    return completed.stdout


@dataclass(frozen=True, slots=True)
class Repository:
    path: Path
    head: str

    @classmethod
    def open(cls, path: Path) -> Self:
        path = path.resolve()
        if not path.is_dir():
            raise ConfigError(f"repository path {path} is not a directory")
        inside = _git(path, "rev-parse", "--is-inside-work-tree").strip()
        if inside != "true":
            raise ConfigError(f"{path} is not a git work tree")
        return cls(path=path, head=_git(path, "rev-parse", "HEAD").strip())

    def changed_paths(self, base: str) -> tuple[str, ...]:
        """Paths the change touches, relative to the repository root.

        Deleted files are excluded: there is nothing left to analyse in them, and the diff of the
        remaining tree is what every capability reasons about.
        """
        return _changed_paths(self.path, self.merge_base(base))

    def merge_base(self, base: str) -> str:
        return _git(self.path, "merge-base", base, "HEAD").strip()


MAX_CHANGED_LINES = 300
MAX_LINE_CHARS = 400
_HUNK = re.compile(r"^@@ -(?P<old>\d+)(?:,\d+)? \+(?P<new>\d+)(?:,\d+)? @@")


@dataclass(frozen=True, slots=True)
class Line:
    line: int
    text: str

    def as_json(self) -> dict[str, Any]:
        return {"line": self.line, "text": self.text}


@dataclass(frozen=True, slots=True)
class ChangedLines:
    """What the change did to one file: the lines it added, and the lines it replaced."""

    path: str
    in_change: bool
    added: tuple[Line, ...]
    removed: tuple[Line, ...]
    truncated: bool

    def as_json(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "in_change": self.in_change,
            "added": [line.as_json() for line in self.added],
            "removed": [line.as_json() for line in self.removed],
            "truncated": self.truncated,
        }


@dataclass(frozen=True, slots=True)
class ChangeView:
    """The change itself, as git describes it.

    Exists because scope is not a matter of opinion. A review that reads a whole manifest sees every
    pin in it and reports the ones nobody touched — which blocks an author for a line they did not
    write, and that is how a gate gets switched off. Asking git which lines the change added makes
    the boundary a fact, not something a model has to remember to respect.
    """

    repository: Path
    base: str
    merge_base: str

    @classmethod
    def of(cls, repository: Repository, base: str) -> ChangeView:
        return cls(repository=repository.path, base=base, merge_base=repository.merge_base(base))

    def source(self, path: str) -> str:
        """How the answer could be reproduced by hand, recorded with every call."""
        return f"git diff {self.merge_base[:12]}..HEAD -- {path}"

    def lines(self, path: str, *, limit: int = MAX_CHANGED_LINES) -> ChangedLines:
        relative = self._inside(path)
        output = _git(
            self.repository, "diff", "--unified=0", self.merge_base, "HEAD", "--", relative
        )
        added, removed, truncated = _parse(output, limit=limit)
        return ChangedLines(
            path=relative,
            in_change=bool(output.strip()),
            added=added,
            removed=removed,
            truncated=truncated,
        )

    def _inside(self, path: str) -> str:
        """Refuse a path that leaves the repository, or that git would read as an option."""
        if path.startswith("-"):
            raise ConfigError(f"{path!r} is not a path")
        candidate = (self.repository / path).resolve()
        if candidate != self.repository and self.repository not in candidate.parents:
            raise ConfigError(f"{path!r} is outside the repository")
        return candidate.relative_to(self.repository).as_posix()


def _changed_paths(repository: Path, merge_base: str) -> tuple[str, ...]:
    output = _git(repository, "diff", "--name-only", "--diff-filter=d", merge_base, "HEAD", "--")
    return tuple(line.strip() for line in output.splitlines() if line.strip())


def _parse(diff: str, *, limit: int) -> tuple[tuple[Line, ...], tuple[Line, ...], bool]:
    """Read a zero-context diff into numbered added and removed lines.

    Line numbers matter more than the text: a finding has to point somewhere a reader can open, and
    a number taken from the diff is the file's own number rather than one counted by a model.
    """
    added: list[Line] = []
    removed: list[Line] = []
    old_number = new_number = 0
    truncated = False
    for raw in diff.splitlines():
        hunk = _HUNK.match(raw)
        if hunk is not None:
            old_number = int(hunk.group("old"))
            new_number = int(hunk.group("new"))
            continue
        if raw.startswith("+++") or raw.startswith("---"):
            continue
        if raw.startswith("+"):
            if len(added) < limit:
                added.append(Line(new_number, raw[1:][:MAX_LINE_CHARS]))
            else:
                truncated = True
            new_number += 1
        elif raw.startswith("-"):
            if len(removed) < limit:
                removed.append(Line(old_number, raw[1:][:MAX_LINE_CHARS]))
            else:
                truncated = True
            old_number += 1
    return tuple(added), tuple(removed), truncated
