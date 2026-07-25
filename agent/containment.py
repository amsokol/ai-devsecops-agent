"""Whether a session wrote into the checkout, and putting it back when it did.

Every write a task is meant to make goes through the agent's own tools, into a worktree the agent
made for it. Nothing about that is enforced by the agent, though: a backend brings file tools of its
own, and what keeps those inside the session's directory is the backend's sandbox. That is somebody
else's promise, it is a setting, and a machine that cannot provide a sandbox turns it off in order
to run at all — at which point the promise is gone and nothing says so.

It went exactly that way on the first live fix. Two sessions edited the repository's own checkout
through the backend's tools, the worktree they were given stayed empty, and the agent concluded they
had reported a fix without making one. Two paid sessions wasted, two misleading refusals, and a
developer left with modifications nobody asked for in files they were not working on.

So the agent watches instead of trusting. Nothing here is deleted: a stray change is copied into the
run record before the checkout is restored, because the one thing worse than a session editing the
wrong tree is the agent throwing away work — including work that was already there.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

STATUS = ("status", "--porcelain=v1", "--untracked-files=all", "-z")
TIMEOUT_SECONDS = 30


def _git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    """Git, with the failure left to the caller: here a non-zero exit is often the answer."""
    located = shutil.which("git")
    if located is None:
        return subprocess.CompletedProcess(args=arguments, returncode=1, stdout=b"", stderr=b"")
    return subprocess.run(  # noqa: S603 - fixed binary, no shell, arguments are ours
        [located, "-C", str(repository), *arguments],
        capture_output=True,
        timeout=TIMEOUT_SECONDS,
        check=False,
    )


@dataclass(frozen=True, slots=True)
class Stray:
    path: str
    kept: str
    """Where the change was copied before the checkout was put back, so nothing is only deleted."""

    def as_json(self) -> dict[str, str]:
        return {"path": self.path, "kept": self.kept}


@dataclass(slots=True)
class Checkout:
    """The repository's working tree as it was before a session ran in it."""

    repository: Path
    mine: tuple[Path, ...] = ()
    """Directories the agent itself writes in: the run record, the caches. A product that does not
    ignore them in git would otherwise see every one of its own runs as a breach."""
    before: frozenset[str] = frozenset()
    strayed: list[Stray] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    """Tasks run at the same time and there is one checkout between them."""

    @classmethod
    def of(cls, repository: Path, *, mine: tuple[Path, ...] = ()) -> Checkout:
        watch = cls(repository=repository, mine=tuple(mine))
        watch.before = watch._dirty()
        return watch

    def restore(self, *, keep: Path) -> tuple[Stray, ...]:
        """Put back anything that changed since, and say what that was.

        Called after a session, so "since" is usually that session — usually, because sessions run
        concurrently and the checkout is one. Attribution can be wrong when two are running; the
        answer to that is not to guess better but to reject both, since a session that believes it
        edited the tree cannot be told apart from one that did.

        A path already modified when the run started is left alone. The agent is not here to tidy a
        developer's work, and a run that reverted uncommitted changes would be unforgivable once.
        """
        with self.lock:
            appeared = sorted(self._dirty() - self.before)
            if not appeared:
                return ()
            keep.mkdir(parents=True, exist_ok=True)
            strays = tuple(self._put_back(path, keep=keep) for path in appeared)
            self.strayed += strays
            self.before = self._dirty()
            return strays

    def _put_back(self, path: str, *, keep: Path) -> Stray:
        source = self.repository / path
        kept = keep / path
        kept.parent.mkdir(parents=True, exist_ok=True)
        if source.is_file():
            shutil.copy2(source, kept)
        _git(self.repository, "checkout", "--", path)
        if source.exists() and self._is_untracked(path):
            # Untracked: `git checkout` has nothing to restore it to, so it is moved out whole. It
            # was not in the repository before this session and it is not the repository's now.
            source.unlink()
        return Stray(path=path, kept=str(kept))

    def _is_untracked(self, path: str) -> bool:
        return _git(self.repository, "ls-files", "--error-unmatch", path).returncode != 0

    def _dirty(self) -> frozenset[str]:
        result = _git(self.repository, *STATUS)
        if result.returncode != 0:
            return frozenset()
        entries = result.stdout.decode("utf-8", errors="replace").split("\0")
        paths = (entry[3:] for entry in entries if len(entry) > 3)
        return frozenset(path for path in paths if not self._is_mine(path))

    def _is_mine(self, path: str) -> bool:
        full = (self.repository / path).resolve()
        return any(full == directory or directory in full.parents for directory in self.mine)


def refusal(strays: tuple[Stray, ...]) -> str:
    """What the next attempt is told, in the words the retry loop passes to a prompt."""
    listed = ", ".join(stray.path for stray in strays)
    return (
        f"the repository's own checkout was written to while the previous attempt ran ({listed}), "
        "instead of the worktree the task was given. Those writes have been undone. Make every "
        "change through the tools this task was handed, which already point at the right tree; a "
        "file edited any other way is not part of the change and does not count as a fix"
    )
