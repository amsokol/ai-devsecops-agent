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
    grants: dict[str, list[str]] = field(default_factory=dict)
    """Binaries and hosts this run was permitted to use, so a `not-permitted` gap is explainable."""
    cache: dict[str, Any] = field(default_factory=dict)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    findings: list[dict[str, Any]] = field(default_factory=list)
    models: list[dict[str, Any]] = field(default_factory=list)
    tool_versions: dict[str, str] = field(default_factory=dict)
    cost: dict[str, Any] = field(
        default_factory=lambda: {"known": False, "tokens": None, "money": None}
    )
    warnings: list[str] = field(default_factory=list)
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
        manifest.tasks = [
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
        manifest.skipped = [
            {"capability": capability, "reason": reason} for capability, reason in plan.skipped
        ]
        return manifest

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
            "cache": self.cache,
            "evidence": self.evidence,
            "findings": self.findings,
            "models": self.models,
            "tool_versions": self.tool_versions,
            "cost": self.cost,
            "warnings": self.warnings,
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
