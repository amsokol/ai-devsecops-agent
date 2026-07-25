"""Run configuration: scenarios, limits, the library pin, the ceiling, storage.

Configuration ships inside the package so that an installed agent behaves exactly like one run from
a source tree. `--config-dir` replaces the directory wholesale.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Self

from agent.domain import Role, Trigger
from agent.errors import ConfigError
from agent.library import load_yaml_mapping
from agent.overlay import Limits
from agent.tools.ceiling import Ceiling

BUILTIN_CONFIG_DIR = Path(__file__).resolve().parent / "config"


class When(StrEnum):
    """The conditions a task can be gated on. All of them are computed from facts, never judged."""

    SOURCE_CHANGED = "source-changed"
    SOURCE_OR_WORKFLOWS_CHANGED = "source-or-workflows-changed"
    ECOSYSTEM_PINS_CHANGED = "ecosystem-pins-changed"
    ECOSYSTEM_ENABLED = "ecosystem-enabled"
    HOTSPOTS_PRESENT = "hotspots-present"


@dataclass(frozen=True, slots=True)
class TaskRule:
    capability: str
    role: Role
    when: When
    per_ecosystem: bool
    required: bool


@dataclass(frozen=True, slots=True)
class Scenario:
    playbook: str
    triggers: tuple[Trigger, ...]
    tasks: tuple[TaskRule, ...]
    split_threshold_bytes: int

    @classmethod
    def read(cls, path: Path) -> Self:
        raw = load_yaml_mapping(path)
        try:
            triggers = tuple(Trigger(value) for value in raw["triggers"])
            tasks = tuple(_read_rule(path, item) for item in raw["tasks"])
            return cls(
                playbook=str(raw["playbook"]),
                triggers=triggers,
                tasks=tasks,
                split_threshold_bytes=int(raw.get("split_threshold_bytes", 0)),
            )
        except KeyError as error:
            raise ConfigError(f"{path}: missing {error.args[0]}") from None
        except ValueError as error:
            raise ConfigError(f"{path}: {error}") from None


def _read_rule(path: Path, raw: Any) -> TaskRule:
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: each task must be a mapping")
    try:
        return TaskRule(
            capability=str(raw["capability"]),
            role=Role(raw.get("role", Role.ANALYST)),
            when=When(raw["when"]),
            per_ecosystem=bool(raw.get("per_ecosystem", False)),
            required=bool(raw.get("required", True)),
        )
    except KeyError as error:
        raise ConfigError(f"{path}: task is missing {error.args[0]}") from None
    except ValueError as error:
        raise ConfigError(f"{path}: {error}") from None


@dataclass(frozen=True, slots=True)
class LibraryPin:
    """What the agent expects the library to be.

    `None` means "do not check", which is for local work only: the manifest then records that the
    run could not prove which knowledge it used.
    """

    version: str | None
    digest: str | None


@dataclass(frozen=True, slots=True)
class Storage:
    """Where persistence lives. `cache_path` is relative to the run directory's parent in CI."""

    cache_path: Path | None
    state_ref: str


@dataclass(frozen=True, slots=True)
class Config:
    directory: Path
    scenarios: tuple[Scenario, ...]
    pin: LibraryPin
    maintenance_limits: Limits
    notes_limit: int
    ceiling: Ceiling
    storage: Storage
    never_send: tuple[str, ...]

    @classmethod
    def load(cls, directory: Path | None = None) -> Self:
        directory = (directory or BUILTIN_CONFIG_DIR).resolve()
        if not directory.is_dir():
            raise ConfigError(f"configuration directory {directory} does not exist")
        scenario_dir = directory / "scenarios"
        paths = sorted(scenario_dir.glob("*.yaml"))
        if not paths:
            raise ConfigError(f"no scenarios found in {scenario_dir}")
        limits = load_yaml_mapping(directory / "limits.yaml")
        library = load_yaml_mapping(directory / "library.yaml")
        maintenance = limits.get("maintenance", {})
        if not isinstance(maintenance, dict):
            raise ConfigError(f"{directory / 'limits.yaml'}: maintenance must be a mapping")
        ceiling_raw = load_yaml_mapping(directory / "ceiling.yaml")
        egress = ceiling_raw.get("egress") or {}
        return cls(
            directory=directory,
            scenarios=tuple(Scenario.read(path) for path in paths),
            pin=LibraryPin(
                version=_optional_str(library.get("version")),
                digest=_optional_str(library.get("digest")),
            ),
            maintenance_limits=Limits(
                open_change_requests=int(maintenance.get("open_change_requests", 3)),
                new_issues_per_run=int(maintenance.get("new_issues_per_run", 5)),
            ),
            notes_limit=int(limits.get("overlay_notes_characters", 8000)),
            ceiling=Ceiling.read(directory),
            storage=_read_storage(directory),
            never_send=tuple(str(item) for item in (egress.get("never_send") or ())),
        )

    def scenario_for(self, trigger: Trigger) -> Scenario:
        for scenario in self.scenarios:
            if trigger in scenario.triggers:
                return scenario
        raise ConfigError(f"no scenario in {self.directory} handles trigger {trigger}")


def _read_storage(directory: Path) -> Storage:
    raw = load_yaml_mapping(directory / "storage.yaml")
    cache = raw.get("fact_cache") or {}
    state = raw.get("state") or {}
    if not isinstance(cache, dict) or not isinstance(state, dict):
        raise ConfigError(f"{directory / 'storage.yaml'}: fact_cache and state must be mappings")
    enabled = bool(cache.get("enabled", True))
    path = _optional_str(cache.get("path"))
    return Storage(
        cache_path=Path(path) if enabled and path else None,
        state_ref=str(state.get("ref") or "refs/agent/state"),
    )


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
