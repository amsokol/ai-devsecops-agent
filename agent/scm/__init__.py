"""The hosting platform: the port, its adapters, and the marker that makes publishing idempotent."""

from agent.scm.github import Credential, GitHub, credential
from agent.scm.port import (
    Authority,
    Change,
    Comment,
    Identity,
    Issue,
    NewThread,
    Platform,
    Review,
    ScmError,
    Stance,
    Thread,
)

__all__ = [
    "Authority",
    "Change",
    "Comment",
    "Credential",
    "GitHub",
    "Identity",
    "Issue",
    "NewThread",
    "Platform",
    "Review",
    "ScmError",
    "Stance",
    "Thread",
    "credential",
]
