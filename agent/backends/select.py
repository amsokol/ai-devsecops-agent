"""Choosing a backend from configuration.

Deliberately a lookup and nothing more: which SDK runs a task is a configuration decision, and an
unknown name is a startup error rather than a silent fallback to something that happens to be
installed.
"""

from __future__ import annotations

from agent.backends.fake import FakeBackend
from agent.backends.port import Backend
from agent.config import Execution
from agent.errors import ConfigError

KNOWN = ("cursor", "fake")


def make_backend(execution: Execution) -> Backend:
    match execution.backend:
        case "cursor":
            # The adapter arrives with the tool server: a subagent that could not reach the agent's
            # tools would have no way to produce evidence, and every finding it made would be
            # unsupported. Refusing is better than shipping a backend that can only guess.
            raise ConfigError(
                "the cursor backend is not implemented yet. Until it is, select 'fake' in "
                "execution.yaml to exercise the pipeline, or use --plan-only"
            )
        case "fake":
            # Shipped, not hidden behind a test-only import: a run that used it says so in the
            # manifest, which is how a report from a fake session is recognisable as one.
            return FakeBackend()
        case other:
            raise ConfigError(f"unknown backend {other!r}; known backends: {', '.join(KNOWN)}")
