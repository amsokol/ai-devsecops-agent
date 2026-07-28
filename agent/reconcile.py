"""What a run may say about a finding it no longer sees, and how it records what it did.

One rule for whether a run *knows* anything about the absence: the check that owns the finding has
to have reached a complete answer. `clean` and `findings` are both complete — a check that looked
and listed four problems has said what it found, and a fifth that is not on the list is not on it.
What is not complete is a check that never ran, ran out of budget, or could not run its tools, and
there absence is evidence of nothing.

Requiring `clean` instead was the same rule read too strictly, and it froze the tracker: while any
one pin in an ecosystem was outdated, no issue of that ecosystem could ever close, including ones
somebody had already fixed. A live maintenance run kept four issues open for that reason, and a
weekly run of a repository that always has something outdated would keep them open forever.

Acting on the absence is a second question, and the two places answer it differently on purpose.
A review thread is resolved straight away: it lives on one change, the next push reopens it if the
problem is still there, and the person reading the conversation sees that happen. A tracked issue
waits for a second consecutive complete run — see `agent/absence.py` — because nobody revisits a
closed issue, and on the default branch there is a memory to be careful with.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent.domain import Outcome
from agent.scm.port import Identity
from agent.verdict import TaskOutcome


@dataclass(frozen=True, slots=True)
class Posted:
    """One thing that happened on the platform, or was deliberately not attempted."""

    what: str
    key: str = ""
    detail: str = ""

    def as_json(self) -> dict[str, Any]:
        return {"what": self.what, "key": self.key, "detail": self.detail}


COMPLETE = frozenset({Outcome.CLEAN, Outcome.FINDINGS})
"""Answers that say what the check found. The rest say that it did not get to look."""


def _ecosystem_in_key(key: str) -> str | None:
    """The ecosystem segment of a finding key, when the key is scoped that way.

    Keys are `capability:ecosystem:package:…` for dependency findings. Without an ecosystem segment
    the owning check is the whole capability (code surfaces are not split per ecosystem task).
    """
    parts = key.split(":")
    if len(parts) >= 2 and parts[1].startswith("ecosystems/"):
        return parts[1]
    return None


def _owns_finding(outcome: TaskOutcome, *, capability: str, ecosystem: str | None) -> bool:
    if outcome.capability != capability:
        return False
    if ecosystem is None:
        return True
    # Per-ecosystem tasks are planned as `deps-outdated@cargo` for `ecosystems/cargo`.
    short = ecosystem.rsplit("/", 1)[-1]
    return outcome.id.endswith(f"@{short}")


def unproven(key: str, outcomes: tuple[TaskOutcome, ...]) -> str | None:
    """Why the absence of this finding proves nothing, or `None` when it may be acted on.

    The owning capability is read from the head of the key, which is where it is by construction.
    A task that was exhausted, that could not run its tools, or that never ran because the run was
    narrowed says nothing about this finding; without that guard the first scanner outage looks like
    a week of fixes — every thread resolved, every issue closed, and nothing actually checked.

    For dependency findings the capability is split into one task per ecosystem. A run narrowed with
    `--only deps-outdated@cargo` must not close a python-uv issue: cargo finishing is not evidence
    that the python pin was examined.
    """
    capability = key.split(":", 1)[0]
    ecosystem = _ecosystem_in_key(key)
    owning = [
        item for item in outcomes if _owns_finding(item, capability=capability, ecosystem=ecosystem)
    ]
    if not owning:
        if ecosystem is not None and any(item.capability == capability for item in outcomes):
            return (
                f"{capability} for {ecosystem} did not run in this run, "
                "so its absence proves nothing"
            )
        return f"{capability} did not run in this run, so its absence proves nothing"
    if any(item.outcome not in COMPLETE for item in owning):
        states = ", ".join(sorted({item.outcome.value for item in owning}))
        where = f"{capability} for {ecosystem}" if ecosystem else capability
        return f"{where} finished {states}, so it never got to the end of what it covers"
    return None


def concluded(key: str, outcomes: tuple[TaskOutcome, ...]) -> bool:
    """Whether the check that owns this finding reached a complete answer this run.

    The same question as `unproven`, asked by the code that tells somebody what the run did rather
    than by the code that acts on it. One implementation, because two of them disagreed once and the
    answer to "what happened to the thing I approved?" became "the check did not finish" in cases
    where it plainly had.
    """
    return unproven(key, outcomes) is None


def caution_for(identity: Identity) -> str:
    """Whether what this run writes will read — and behave — as a person's.

    Not a refusal, because a dedicated machine account is a legitimate way to run this and the
    platform shows it as an ordinary user. It is said out loud, once, because the consequence is not
    cosmetic: a workflow that starts a run on human comments and filters bots cannot filter an
    ordinary user, so the agent's own comment wakes the agent, which comments again.
    """
    if identity.trustworthy:
        return ""
    if not identity.known:
        return (
            "the credential's account could not be read, so it is not known whether what this run "
            "writes will be shown as a machine's. If the workflow wakes on comments, filter by the "
            "login that posts them"
        )
    return (
        f"published as {identity.login}, which the platform shows as a person rather than a bot. A "
        "machine's judgement under a human name is hard to tell from a colleague's opinion, and a "
        f"workflow that filters bot comments will not filter {identity.login}: filter that login "
        "explicitly, or publish with a bot credential"
    )
