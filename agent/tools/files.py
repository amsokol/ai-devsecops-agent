"""Repository access, bounded to the repository.

Every path is resolved and checked to be inside the target tree. This is not defensive politeness:
the content under review is untrusted, and a task that can be talked into reading `~/.ssh` is a
credential leak rather than a bug.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

MAX_READ_BYTES = 400_000
MAX_MATCHES = 200


class OutsideRepository(Exception):
    """A path escaped the target repository. Never retried with a different spelling."""


@dataclass(frozen=True, slots=True)
class Match:
    path: str
    line: int
    text: str


@dataclass(frozen=True, slots=True)
class FileTools:
    root: Path

    def resolve(self, relative: str) -> Path:
        candidate = (self.root / relative).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise OutsideRepository(f"{relative!r} is outside the repository")
        return candidate

    def read_file(self, relative: str, *, limit: int = MAX_READ_BYTES) -> str:
        path = self.resolve(relative)
        data = path.read_bytes()[: limit + 1]
        text = data.decode("utf-8", errors="replace")
        if len(data) > limit:
            return text[:limit] + "\n… truncated, file is larger than the read limit\n"
        return text

    def list_files(self, pattern: str = "**/*") -> tuple[str, ...]:
        return tuple(
            sorted(
                path.relative_to(self.root).as_posix()
                for path in self.root.glob(pattern)
                if path.is_file() and ".git" not in path.relative_to(self.root).parts
            )
        )

    def search_text(
        self, pattern: str, *, glob: str = "**/*", limit: int = MAX_MATCHES
    ) -> tuple[Match, ...]:
        try:
            expression = re.compile(pattern)
        except re.error as error:
            raise ValueError(f"invalid regular expression: {error}") from None
        matches: list[Match] = []
        for relative in self.list_files(glob):
            path = self.root / relative
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError, OSError:
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                if expression.search(line):
                    matches.append(Match(path=relative, line=number, text=line.strip()[:400]))
                    if len(matches) >= limit:
                        return tuple(matches)
        return tuple(matches)
