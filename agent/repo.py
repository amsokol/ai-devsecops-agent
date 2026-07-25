"""Facts about the target repository, obtained from git rather than guessed."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Self

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
        merge_base = _git(self.path, "merge-base", base, "HEAD").strip()
        output = _git(self.path, "diff", "--name-only", "--diff-filter=d", merge_base, "HEAD", "--")
        return tuple(line.strip() for line in output.splitlines() if line.strip())
