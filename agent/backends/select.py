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
            try:
                from agent.backends.cursor import CursorBackend
            except ImportError as error:
                raise ConfigError(
                    f"the cursor backend needs the cursor-sdk package: {error}. Install the agent "
                    "with its 'cursor' extra, or select another backend in execution.yaml"
                ) from None
            return CursorBackend(model=execution.model, sandbox=execution.sandbox)
        case "fake":
            # Shipped, not hidden behind a test-only import: a run that used it says so in the
            # manifest, which is how a report from a fake session is recognisable as one.
            return FakeBackend()
        case other:
            raise ConfigError(f"unknown backend {other!r}; known backends: {', '.join(KNOWN)}")
