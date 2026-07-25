"""Failures the agent distinguishes, and the exit codes they map to."""

from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    """Process exit codes. The surrounding CI acts on these, so they are part of the interface."""

    OK = 0
    INTERNAL = 2
    BLOCKED = 5
    INCONCLUSIVE = 6
    CONFIG = 64


class AgentError(Exception):
    """Base class for failures the agent reports rather than raises as a traceback."""

    exit_code: ExitCode = ExitCode.INTERNAL


class ConfigError(AgentError):
    """The run was asked to do something impossible before any work started.

    A malformed overlay, an unknown ecosystem, an incompatible library. Always raised at startup:
    the point of this class is that such problems are never discovered mid-review.
    """

    exit_code = ExitCode.CONFIG
