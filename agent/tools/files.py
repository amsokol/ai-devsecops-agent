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


class NotEdited(Exception):
    """The edit was not applied, and the file is unchanged. Never a partial write."""


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

    def edit_file(self, relative: str, *, find: str, replace: str) -> int:
        """Replace an exact fragment, and only when it occurs exactly once.

        Deliberately not a whole-file write. A fix should be the smallest change that removes the
        problem, and a tool that rewrites a file lets a model quietly reformat, drop a comment or
        lose a line it never read. Ambiguity is refused rather than resolved by position: `find`
        matching twice means the caller does not know which one it is changing.

        Returns the line number where the replacement starts, so the caller can say where it edited.
        """
        if self.withheld(relative):
            # The contents were never shown to the model, so an edit here would be a change made
            # blind — and this is exactly the class of file where that is unacceptable.
            raise Withheld(f"{relative!r} is on the never-send list")
        if not find:
            raise NotEdited(
                f"{relative}: an edit needs the exact text to replace. There is no whole-file "
                "write: read the file, then replace the fragment you mean to change"
            )
        path = self.resolve(relative)
        if not path.is_file():
            raise NotEdited(f"{relative} does not exist in this tree")
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise NotEdited(f"{relative}: {error}") from None
        occurrences = text.count(find)
        if occurrences == 0:
            raise NotEdited(
                f"{relative} does not contain that text exactly. Read the file again and copy the "
                "fragment from what you read, including its indentation"
            )
        if occurrences > 1:
            raise NotEdited(
                f"{relative} contains that text {occurrences} times, so it does not say which one "
                "to change. Include enough surrounding lines to make it unique"
            )
        path.write_text(text.replace(find, replace, 1), encoding="utf-8")
        return text[: text.index(find)].count("\n") + 1

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
