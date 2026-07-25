"""The agent's memory between runs: a small document in a git ref of its own.

Three kinds of persistence exist in this agent and they are deliberately not the same thing. Run
records are artifacts, kept for reading. The fact cache is a cache: losing it costs time and nothing
else, which is why it holds only answers that cannot change. This is the third kind — the little
the agent must remember to behave differently next week, and the only one allowed to affect what
gets written to a tracker.

That rules a cache out. "Has this been failing for two runs?", answered from a store that evictions
can empty, is a question answered wrongly in the direction of noise or of silence, whichever the
eviction happens to cause. A git ref travels with the repository, survives runners, needs no
service, and is readable by anyone who wants to audit what the agent thought it knew.

`refs/agent/state` is not a branch, so it appears in nobody's history and no change request can be
opened against it. Its contents are one JSON document, replaced whole. Concurrent writers are not
merged: a rejected push is reported and the state stays as it was, which delays a decision by one
run and never corrupts one. The scheduled run holds the platform's own concurrency lock anyway, so
two writers means somebody also started a run by hand in the same minute.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from agent.errors import ConfigError
from agent.repo import Repository
from agent.scm.port import Platform, ScmError

FILE = "state.json"


@dataclass(slots=True)
class Memory:
    """What earlier runs left for this one. An empty memory is the normal first case."""

    repository: Repository
    ref: str
    known: dict[str, Any] | None = None
    """What `read` found, kept so that `write` can tell a change from a week with nothing new."""

    def read(self) -> dict[str, Any]:
        """The stored document, or an empty one when there is none or it cannot be read.

        Never raises. A repository whose ref was never written, a remote that cannot be reached and
        a document somebody edited into invalid JSON all mean the same thing here: this run has
        nothing to go on. Failing the run instead would turn a memory aid into a dependency.
        """
        self.known = self._read()
        return self.known

    def _read(self) -> dict[str, Any]:
        if not self.repository.fetch(self.ref):
            return {}
        raw = self.repository.file_at(self.ref, FILE)
        if raw is None:
            return {}
        try:
            document = json.loads(raw)
        except ValueError:
            return {}
        return document if isinstance(document, dict) else {}

    def write(self, document: dict[str, Any], *, platform: Platform, run: str) -> tuple[bool, str]:
        """Store the document if it says anything new, and answer with what happened.

        A week in which nothing changed writes nothing. The saving is not the point — a commit per
        week that repeats last week's document is churn in somebody else's repository, and the whole
        design of the scheduled run is that a run with nothing to say leaves no trace.

        The commit is built with plumbing and never touches the working tree: a fix task may be
        looking at that tree, and a state write that moved a file under it would be a bug found
        much later. It is chained onto whatever the ref pointed at, so the ref only moves forward
        and the push needs no force.

        What it is chained onto is fetched here rather than assumed from an earlier read. A checkout
        that never fetched the ref would otherwise produce a commit with no parent, and pushing one
        of those is refused as it should be — the write would fail for a reason that has nothing to
        do with what is being written.
        """
        if self.known is not None and document == self.known:
            return False, ""
        body = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        self.repository.fetch(self.ref)
        try:
            commit = self.repository.write_file(
                FILE,
                body,
                message=f"agent: state after run {run}",
                onto=self.repository.resolve(self.ref),
            )
        except ConfigError as error:
            return False, str(error)
        try:
            platform.push(self.repository.path, source=commit, target=self.ref)
        except ScmError as error:
            return False, str(error)
        # Only after the remote agreed. A local ref moved first would make a refused push look
        # stored to everything that reads it later in this run.
        self.repository.point(self.ref, commit)
        self.known = document
        return True, ""
