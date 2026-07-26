"""The run manifest: what the run used, what it did, and what it could not do.

The manifest answers "why did the agent decide that" without a second run, and it is what makes
model evaluation possible: without recorded cost and latency, "this model is better here" is not a
checkable claim.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent.domain import Outcome, Plan, Reason, Trigger
from agent.errors import ConfigError

SCHEMA = 1


def run_id(*, trigger: Trigger, repository: Path, head: str, at: datetime) -> str:
    """Sortable and unique per run, with a suffix derived from what the run is about."""
    seed = f"{trigger}\0{repository}\0{head}".encode()
    return f"{at.strftime('%Y%m%dT%H%M%SZ')}-{hashlib.sha256(seed).hexdigest()[:8]}"


@dataclass(slots=True)
class TaskRecord:
    id: str
    capability: str
    role: str
    required: bool
    ecosystem: str | None
    scope: tuple[str, ...]
    knowledge: tuple[str, ...]
    outcome: Outcome | None = None
    reason: Reason | None = None
    attempts: list[dict[str, Any]] = field(default_factory=list)
    """Every session run for this task, including rejected results — otherwise a retried task looks
    identical to one that worked first time, and the prompt that failed is unrecoverable."""
    calls: list[dict[str, Any]] = field(default_factory=list)
    """The task's tool calls, in order, so a fact can be traced to what produced it."""
    notes: str = ""

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "capability": self.capability,
            "role": self.role,
            "required": self.required,
            "ecosystem": self.ecosystem,
            "scope": list(self.scope),
            "knowledge": list(self.knowledge),
            "outcome": self.outcome.value if self.outcome else None,
            "reason": self.reason.value if self.reason else None,
            "attempts": self.attempts,
            "calls": self.calls,
            "notes": self.notes,
        }


@dataclass(slots=True)
class Manifest:
    run_id: str
    agent_version: str
    trigger: Trigger
    playbook: str
    repository: str
    head: str
    change: int | None
    library: dict[str, Any]
    overlay: dict[str, Any]
    started_at: str
    tasks: list[TaskRecord] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    grants: dict[str, Any] = field(default_factory=dict)
    """Binaries and hosts this run was permitted to use, so a `not-permitted` gap is explainable,
    and whether it read the hosting platform's API as somebody or anonymously, which decides the
    rate limit a missing fact may have run into."""
    posture: dict[str, Any] = field(default_factory=dict)
    """Whose code this run read, and therefore whether it was allowed to execute any of it. Recorded
    for every run: "the agent ran nothing over that fork" is a property somebody will want to check
    afterwards, and it cannot be seen anywhere else in the record."""
    cache: dict[str, Any] = field(default_factory=dict)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    coverage: dict[str, Any] = field(default_factory=dict)
    """Which packages each check examined, as opposed to which ones it had something to say about.

    The difference is invisible in a findings list and decides whether an issue may be closed, so it
    is recorded where somebody comparing two runs can see it."""
    findings: list[dict[str, Any]] = field(default_factory=list)
    verdict: dict[str, Any] = field(default_factory=dict)
    policy: dict[str, Any] = field(default_factory=dict)
    """Which blocking table this run applied, so a verdict can be checked against the knowledge."""
    budget: dict[str, Any] = field(default_factory=dict)
    """The limits this run was given and what it spent, so an `exhausted` task is explainable."""
    wake: dict[str, Any] = field(default_factory=dict)
    """What woke the run when somebody's comment did: whose it was, which conversation, how it was
    read and which course that led to. The classification is recorded even when the run then
    refused to act on it: "the agent read this as a question" is the part somebody will argue
    with."""
    roles: list[dict[str, str]] = field(default_factory=list)
    """Which backend and model each role the plan needed, checked before anything was spent."""
    fixes: list[dict[str, Any]] = field(default_factory=list)
    """What each fix task did, including the ones that refused: a branch that was not created is as
    much a part of the record as one that was."""
    actions: dict[str, Any] = field(default_factory=dict)
    """What was published on the hosting platform, thread by thread — including what was left alone,
    because "the agent did not comment again" is the property idempotency claims."""
    remediation: dict[str, Any] = field(default_factory=dict)
    """The fix queue: what this run attempted, and what it left for the next one and why."""
    models: list[dict[str, Any]] = field(default_factory=list)
    tool_versions: dict[str, str] = field(default_factory=dict)
    cost: dict[str, Any] = field(
        default_factory=lambda: {"known": False, "tokens": None, "money": None}
    )
    warnings: list[str] = field(default_factory=list)
    partial: list[str] = field(default_factory=list)
    """Set when a run was narrowed to named tasks, so nobody reads its verdict as the playbook's."""
    result: str = "unknown"
    finished_at: str | None = None

    @classmethod
    def start(
        cls,
        *,
        agent_version: str,
        plan: Plan,
        repository: Path,
        head: str,
        change: int | None,
        library: dict[str, Any],
        overlay: dict[str, Any],
        at: datetime | None = None,
    ) -> Manifest:
        at = at or datetime.now(UTC)
        manifest = cls(
            run_id=run_id(trigger=plan.trigger, repository=repository, head=head, at=at),
            agent_version=agent_version,
            trigger=plan.trigger,
            playbook=plan.playbook,
            repository=str(repository),
            head=head,
            change=change,
            library=library,
            overlay=overlay,
            started_at=at.isoformat(),
        )
        manifest.replan(plan)
        return manifest

    def replan(self, plan: Plan) -> None:
        """Record this plan as the one the run works from.

        Called a second time when a wake narrows the plan to the finding somebody asked about. The
        record then shows the tasks that actually ran, with the rest listed as skipped and why —
        rather than a plan the run silently did not carry out.
        """
        self.tasks = [
            TaskRecord(
                id=task.id,
                capability=task.capability,
                role=task.role.value,
                required=task.required,
                ecosystem=task.ecosystem,
                scope=task.scope,
                knowledge=task.knowledge,
            )
            for task in plan.tasks
        ]
        self.skipped = [
            {"capability": capability, "reason": reason} for capability, reason in plan.skipped
        ]

    def finish(self, result: str, at: datetime | None = None) -> None:
        self.result = result
        self.finished_at = (at or datetime.now(UTC)).isoformat()

    def as_json(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "run_id": self.run_id,
            "agent_version": self.agent_version,
            "trigger": self.trigger.value,
            "playbook": self.playbook,
            "repository": self.repository,
            "head": self.head,
            "change": self.change,
            "library": self.library,
            "overlay": self.overlay,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "result": self.result,
            "tasks": [task.as_json() for task in self.tasks],
            "skipped": self.skipped,
            "grants": self.grants,
            "posture": self.posture,
            "cache": self.cache,
            "evidence": self.evidence,
            "coverage": self.coverage,
            "findings": self.findings,
            "verdict": self.verdict,
            "policy": self.policy,
            "budget": self.budget,
            "wake": self.wake,
            "roles": self.roles,
            "fixes": self.fixes,
            "remediation": self.remediation,
            "actions": self.actions,
            "models": self.models,
            "tool_versions": self.tool_versions,
            "cost": self.cost,
            "warnings": self.warnings,
            "partial": self.partial,
        }

    def write(self, run_dir: Path) -> Path:
        directory = run_dir / self.run_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "manifest.json"
        path.write_text(json.dumps(self.as_json(), indent=2, ensure_ascii=False) + "\n", "utf-8")
        return path


def read_manifest(run_dir: Path, identifier: str) -> dict[str, Any]:
    path = run_dir / identifier / "manifest.json"
    if not path.is_file():
        raise ConfigError(f"no manifest for run {identifier!r} under {run_dir}")
    loaded: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ConfigError(f"{path}: manifest is not an object")
    return loaded
