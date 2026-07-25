"""Repository access, bounded to the repository.

Every path is resolved and checked to be inside the target tree. This is not defensive politeness:
the content under review is untrusted, and a task that can be talked into reading `~/.ssh` is a
credential leak rather than a bug.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

MAX_READ_BYTES = 400_000
MAX_MATCHES = 200


class OutsideRepository(Exception):
    """A path escaped the target repository. Never retried with a different spelling."""


class Withheld(Exception):
    """The path is on the never-send list. Its contents do not leave the run."""


@dataclass(frozen=True, slots=True)
class Match:
    path: str
    line: int
    text: str


@dataclass(frozen=True, slots=True)
class FileTools:
    root: Path
    # Enforced here rather than at the tool that reads, because there is more than one way to see
    # a file's contents: a search returning matching lines from a private key leaks it just as
    # thoroughly as reading the key.
    never_send: tuple[str, ...] = field(default_factory=tuple)

    def withheld(self, relative: str) -> bool:
        # `full_match` rather than `fnmatch`: a pattern like `**/*.pem` has to cover `server.pem` at
        # the root as well as one in a subdirectory, and fnmatch quietly does not.
        candidate = PurePosixPath(relative)
        return any(candidate.full_match(pattern) for pattern in self.never_send)

    def resolve(self, relative: str) -> Path:
        candidate = (self.root / relative).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise OutsideRepository(f"{relative!r} is outside the repository")
        return candidate

    def read_file(self, relative: str, *, limit: int = MAX_READ_BYTES) -> str:
        if self.withheld(relative):
            raise Withheld(f"{relative!r} is on the never-send list")
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
            if self.withheld(relative):
                continue
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
