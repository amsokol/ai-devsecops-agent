"""The hosting platform: the port, its adapters, and the marker that makes publishing idempotent."""

from agent.scm.github import Credential, GitHub, credential
from agent.scm.port import Change, Identity, NewThread, Platform, Review, ScmError, Stance, Thread

__all__ = [
    "Change",
    "Credential",
    "GitHub",
    "Identity",
    "NewThread",
    "Platform",
    "Review",
    "ScmError",
    "Stance",
    "Thread",
    "credential",
]
