"""Run configuration: scenarios, limits, the library pin, the ceiling, storage.

Configuration ships inside the package so that an installed agent behaves exactly like one run from
a source tree. `--config-dir` replaces the directory wholesale.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Self

from agent.backends.abilities import ABILITIES
from agent.domain import Role, Trigger
from agent.errors import ConfigError
from agent.library import load_yaml_mapping
from agent.overlay import Limits
from agent.roles import needs
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
class BudgetConfig:
    """Limits for one kind of run: per task, and for the run as a whole."""

    task_seconds: int
    task_steps: int | None
    max_parallel: int
    run_tokens: int | None


@dataclass(frozen=True, slots=True)
class Execution:
    """What a run and its tasks may spend."""

    budget: BudgetConfig
    scheduled_budget: BudgetConfig
    """Usually tighter: a scheduled run has nobody watching it while it spends."""

    def budget_for(self, trigger: Trigger) -> BudgetConfig:
        return self.scheduled_budget if trigger.is_scheduled else self.budget


@dataclass(frozen=True, slots=True)
class Binding:
    """One role, and the backend and model that answer for it."""

    role: Role
    backend: str
    model: str
    options: Mapping[str, Any] = field(default_factory=dict)
    """Backend-specific settings, such as whether the SDK's own sandbox is used."""

    @property
    def sandbox(self) -> bool:
        value = self.options.get("sandbox", True)
        return value if isinstance(value, bool) else True

    def as_json(self) -> dict[str, str]:
        return {"role": self.role.value, "backend": self.backend, "model": self.model}


@dataclass(frozen=True, slots=True)
class Models:
    """The role bindings declared here, already checked against what the adapters can do."""

    bindings: Mapping[Role, Binding]

    def for_role(self, role: Role) -> Binding:
        binding = self.bindings.get(role)
        if binding is None:
            named = ", ".join(sorted(item.value for item in self.bindings)) or "none"
            raise ConfigError(
                f"no backend is bound to the {role.value!r} role in models.yaml (bound roles: "
                f"{named}). A run that needs this role cannot proceed without one."
            )
        return binding

    def as_json(self) -> list[dict[str, str]]:
        return [self.bindings[role].as_json() for role in sorted(self.bindings)]


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
    execution: Execution
    models: Models
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
            execution=_read_execution(directory),
            models=_read_models(directory),
            never_send=tuple(str(item) for item in (egress.get("never_send") or ())),
        )

    def scenario_for(self, trigger: Trigger) -> Scenario:
        for scenario in self.scenarios:
            if trigger in scenario.triggers:
                return scenario
        raise ConfigError(f"no scenario in {self.directory} handles trigger {trigger}")


def _read_execution(directory: Path) -> Execution:
    path = directory / "execution.yaml"
    raw = load_yaml_mapping(path)
    budget = _read_budget(raw.get("budget"), path=path, where="budget", fallback=None)
    scheduled = _read_budget(
        raw.get("scheduled_budget"), path=path, where="scheduled_budget", fallback=budget
    )
    return Execution(budget=budget, scheduled_budget=scheduled)


def _read_models(directory: Path) -> Models:
    """Read the role bindings, and refuse one an adapter cannot honour.

    Checked here, while reading configuration, rather than when a task starts: nothing has been
    spent yet, and an incompatible binding is a mistake in a file rather than an event in a review.
    """
    path = directory / "models.yaml"
    raw = load_yaml_mapping(path)
    roles = raw.get("roles") or {}
    options = raw.get("backends") or {}
    if not isinstance(roles, dict) or not isinstance(options, dict):
        raise ConfigError(f"{path}: roles and backends must be mappings")
    bindings: dict[Role, Binding] = {}
    for name, entry in roles.items():
        role = _role(str(name), path=path)
        if not isinstance(entry, dict):
            raise ConfigError(f"{path}: the {role.value!r} binding must be a mapping")
        backend = str(entry.get("backend") or "").strip()
        model = str(entry.get("model") or "").strip()
        if not backend or not model:
            raise ConfigError(
                f"{path}: the {role.value!r} binding needs both a backend and a model; a model "
                "without a backend is not an address, because the backend decides which exist"
            )
        abilities = ABILITIES.get(backend)
        if abilities is None:
            known = ", ".join(sorted(ABILITIES))
            raise ConfigError(f"{path}: unknown backend {backend!r} (known: {known})")
        missing = abilities.missing(needs(role))
        if missing:
            lacking = ", ".join(ability.value for ability in missing)
            raise ConfigError(
                f"{path}: the {backend!r} backend cannot run the {role.value!r} role; it does not "
                f"support {lacking}. Bind the role to a backend that does, or leave it unbound "
                "until the adapter grows the ability."
            )
        backend_options = options.get(backend) or {}
        if not isinstance(backend_options, dict):
            raise ConfigError(f"{path}: options for backend {backend!r} must be a mapping")
        bindings[role] = Binding(
            role=role, backend=backend, model=model, options=dict(backend_options)
        )
    if not bindings:
        raise ConfigError(f"{path}: no role is bound to a backend, so no task could run")
    return Models(bindings=bindings)


def _role(name: str, *, path: Path) -> Role:
    try:
        return Role(name)
    except ValueError:
        known = ", ".join(role.value for role in Role)
        raise ConfigError(f"{path}: unknown role {name!r} (known roles: {known})") from None


def _read_budget(
    raw: Any, *, path: Path, where: str, fallback: BudgetConfig | None
) -> BudgetConfig:
    """One budget section. Omitted keys fall back, so tightening one knob restates nothing else."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: {where} must be a mapping")
    base = fallback or BudgetConfig(
        task_seconds=900, task_steps=None, max_parallel=4, run_tokens=None
    )
    steps = raw.get("task_steps", base.task_steps)
    tokens = raw.get("run_tokens", base.run_tokens)
    return BudgetConfig(
        task_seconds=_positive(
            raw.get("task_seconds", base.task_seconds), path=path, name=f"{where}.task_seconds"
        ),
        task_steps=(
            _positive(steps, path=path, name=f"{where}.task_steps") if steps is not None else None
        ),
        max_parallel=_positive(
            raw.get("max_parallel", base.max_parallel), path=path, name=f"{where}.max_parallel"
        ),
        run_tokens=(
            _positive(tokens, path=path, name=f"{where}.run_tokens") if tokens is not None else None
        ),
    )


def _positive(value: Any, *, path: Path, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{path}: {name} must be a positive integer, got {value!r}")
    return value


def _missing(path: Path, name: str) -> str:
    raise ConfigError(f"{path}: {name} is required")


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
