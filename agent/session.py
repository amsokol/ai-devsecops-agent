"""The run's toolbox: one place that owns the tools, the evidence and the cache.

Tasks do not construct tools. They ask the session, so that the ceiling, the allowlists, the
evidence record and the cache cannot be bypassed by a task that would rather not bother.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from agent.evidence import Evidence, EvidenceStore, Subject
from agent.repo import ChangeView
from agent.storage import FactCache
from agent.tools import CommandRunner, FileTools, Grants, HttpClient


@dataclass(frozen=True, slots=True)
class TaskTools:
    """What one task may do. `scratch` dies with the task, so a probe cannot leave residue."""

    files: FileTools
    commands: CommandRunner
    http: HttpClient
    scratch: Path


class Session:
    def __init__(
        self,
        *,
        repository: Path,
        grants: Grants,
        cache: FactCache,
        scratch_root: Path,
        never_send: tuple[str, ...] = (),
        change: ChangeView | None = None,
        reading_token: str = "",
        tool_cache: Path | None = None,
    ) -> None:
        self.repository = repository
        self.grants = grants
        self.cache = cache
        self.change = change
        """The change under review, when there is one. Absent in a repository-wide run."""
        self.evidence = EvidenceStore()
        self._never_send = never_send
        self._scratch_root = scratch_root
        self._tool_cache = tool_cache or scratch_root / "tools"
        """Where a command may download what it needs. Falls back inside the run's own scratch,
        which is correct but slow: nothing is reused, so every verification fetches the world."""
        self._reading_token = reading_token
        """The hosting platform's read credential, for the HTTP tool. Never for a command: the
        environment those get is built without it, because a command may be running code from the
        change under review."""

    def for_task(self, task_id: str, *, root: Path | None = None) -> TaskTools:
        """The tools for one task, reading and writing inside `root`.

        `root` is the repository for analysis and the task's own worktree for a fix. Everything is
        derived from it — file access, the working directory for commands — so a fix task cannot
        reach the tree under review even by accident, and the never-send list applies in both.
        """
        scratch = self._scratch_root / task_id
        scratch.mkdir(parents=True, exist_ok=True)
        tree = root or self.repository
        return TaskTools(
            files=FileTools(root=tree, never_send=self._never_send),
            commands=CommandRunner(
                grants=self.grants, workdir=tree, scratch=scratch, tools=self._tool_cache
            ),
            http=HttpClient(grants=self.grants, token=self._reading_token),
            scratch=scratch,
        )

    def fact(
        self,
        *,
        question: str,
        subject: Subject,
        recipe: str,
        acquire: Callable[[], Evidence],
    ) -> Evidence:
        """Answer a question once per run, reusing the cache when the answer cannot change.

        The order — run store, then cache, then acquisition — is what keeps a run from asking the
        same registry the same thing four times because four tasks happened to need it.
        """
        known = self.evidence.find(question, subject)
        if known is not None and known.is_verified:
            return known
        cached = self.cache.get(question, subject, recipe=recipe)
        if cached is not None:
            return self.evidence.add(cached)
        acquired = self.evidence.add(acquire())
        if acquired.is_verified:
            self.cache.put(acquired)
        return acquired
