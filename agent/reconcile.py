"""What a run may say about a finding it no longer sees, and how it records what it did.

One rule, shared by every place the agent writes: a problem that stopped being reported is only
settled when the check that owns it looked and found nothing. A review thread and a tracked issue
would otherwise answer the same question differently — a thread resolved here, an issue left open
there, from one run's facts — and a reader of both would trust neither.
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


def unproven(key: str, outcomes: tuple[TaskOutcome, ...]) -> str | None:
    """Why the absence of this finding proves nothing, or `None` when it may be acted on.

    The owning capability is read from the head of the key, which is where it is by construction.
    Requiring that capability's own task to have finished `clean` is the whole rule: a task that was
    exhausted, or that never ran because the run was narrowed, says nothing about the finding.

    Without this, the first scanner outage looks like a week of fixes: every thread resolved, every
    issue closed, and nothing actually checked.
    """
    capability = key.split(":", 1)[0]
    owning = [item for item in outcomes if item.capability == capability]
    if not owning:
        return f"{capability} did not run in this run, so its absence proves nothing"
    if any(item.outcome is not Outcome.CLEAN for item in owning):
        states = ", ".join(sorted({item.outcome.value for item in owning}))
        return f"{capability} finished {states} rather than clean"
    return None


def concluded(key: str, outcomes: tuple[TaskOutcome, ...]) -> bool:
    """Whether the check that owns this finding reached a result this run.

    A different question from `unproven`, and confusing the two is how the answer to "what happened
    to the thing I approved?" became "the check did not finish" in every case where it had. Closing
    an issue needs the owning task to have finished *clean*, because absence is the claim. Telling
    somebody what the run did needs only that it finished at all — and a task that reports the
    finding again has finished, informatively.
    """
    capability = key.split(":", 1)[0]
    owning = [item for item in outcomes if item.capability == capability]
    return bool(owning) and all(
        item.outcome in {Outcome.CLEAN, Outcome.FINDINGS} for item in owning
    )


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
