"""The run's toolbox: one place that owns the tools, the evidence and the cache.

Tasks do not construct tools. They ask the session, so that the ceiling, the allowlists, the
evidence record and the cache cannot be bypassed by a task that would rather not bother.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from agent.evidence import Evidence, EvidenceStore, Subject
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
    ) -> None:
        self.repository = repository
        self.grants = grants
        self.cache = cache
        self.evidence = EvidenceStore()
        self._never_send = never_send
        self._scratch_root = scratch_root

    def for_task(self, task_id: str) -> TaskTools:
        scratch = self._scratch_root / task_id
        scratch.mkdir(parents=True, exist_ok=True)
        return TaskTools(
            files=FileTools(root=self.repository, never_send=self._never_send),
            commands=CommandRunner(grants=self.grants, workdir=self.repository, scratch=scratch),
            http=HttpClient(grants=self.grants),
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
