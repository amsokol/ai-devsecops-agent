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
from agent.overlay import Choice
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
class Binding:
    """One role, and the backend and model that answer for it."""

    role: Role
    backend: str
    model: str
    options: Mapping[str, Any] = field(default_factory=dict)
    """Backend-specific settings, from the agent's configuration rather than the product's overlay.
    Confinement is not a product's choice to loosen; which model to pay for is."""

    @property
    def sandbox(self) -> bool:
        value = self.options.get("sandbox", True)
        return value if isinstance(value, bool) else True

    def as_json(self) -> dict[str, str]:
        return {"role": self.role.value, "backend": self.backend, "model": self.model}


@dataclass(frozen=True, slots=True)
class Models:
    """The role bindings in force, already checked against what the adapters can do."""

    bindings: Mapping[Role, Binding]

    @classmethod
    def chosen(
        cls,
        choices: Mapping[Role, Choice],
        *,
        options: Mapping[str, Any],
        where: str,
    ) -> Self:
        """The product's pairs, checked as configuration rather than discovered as a failure.

        Every pair comes from the overlay: the agent names no model anywhere, in code or in its own
        configuration. A product outlives any one provider — a subscription ends, an adapter is
        added, a project decides its reviews are worth a more expensive model — and a default in the
        agent would make each of those a fork of the agent.
        """
        return cls(
            bindings={
                role: bind(
                    role,
                    backend=choice.backend,
                    model=choice.model,
                    options=options,
                    where=where,
                )
                for role, choice in choices.items()
            }
        )

    def for_role(self, role: Role) -> Binding:
        binding = self.bindings.get(role)
        if binding is None:
            named = ", ".join(sorted(item.value for item in self.bindings)) or "none"
            raise ConfigError(
                f"no model is bound to the {role.value!r} role in the overlay's `models` (bound "
                f"roles: {named}). A run that needs this role cannot proceed without one."
            )
        return binding

    def as_json(self) -> list[dict[str, str]]:
        return [self.bindings[role].as_json() for role in sorted(self.bindings)]


def bind(
    role: Role,
    *,
    backend: str,
    model: str,
    options: Mapping[str, Any],
    where: str,
) -> Binding:
    """One checked binding.

    `where` is what the errors name, because "unknown backend" is only actionable when the reader is
    told which file to open. Checked while reading configuration rather than when a task starts:
    nothing has been spent yet, and a pair no adapter can honour is a mistake in a file rather than
    an event in a review.
    """
    backend = backend.strip()
    model = model.strip()
    if not backend or not model:
        raise ConfigError(
            f"{where}: the {role.value!r} binding needs both a backend and a model; a model "
            "without a backend is not an address, because the backend decides which exist"
        )
    abilities = ABILITIES.get(backend)
    if abilities is None:
        known = ", ".join(sorted(ABILITIES))
        raise ConfigError(f"{where}: unknown backend {backend!r} (known: {known})")
    missing = abilities.missing(needs(role))
    if missing:
        lacking = ", ".join(ability.value for ability in missing)
        raise ConfigError(
            f"{where}: the {backend!r} backend cannot run the {role.value!r} role; it does not "
            f"support {lacking}. Bind the role to a backend that does, or leave it unbound "
            "until the adapter grows the ability."
        )
    settings = options.get(backend) or {}
    if not isinstance(settings, dict):
        raise ConfigError(f"{where}: options for backend {backend!r} must be a mapping")
    return Binding(role=role, backend=backend, model=model, options=dict(settings))


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
    steps_limit: int
    """Tool calls one task may make. The agent's own runaway guard rather than a budget: it counts
    a step nobody outside sees, and its cost is already capped in tokens by the overlay."""
    notes_limit: int
    ceiling: Ceiling
    storage: Storage
    backend_options: Mapping[str, Any]
    """Settings for each adapter, keyed by backend name. Which backend and model a role uses is not
    here and has no default here: that is the product's decision, made in its overlay."""
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
        ceiling_raw = load_yaml_mapping(directory / "ceiling.yaml")
        egress = ceiling_raw.get("egress") or {}
        return cls(
            directory=directory,
            scenarios=tuple(Scenario.read(path) for path in paths),
            pin=LibraryPin(
                version=_optional_str(library.get("version")),
                digest=_optional_str(library.get("digest")),
            ),
            steps_limit=int(limits.get("tool_calls_per_task", 120)),
            notes_limit=int(limits.get("overlay_notes_characters", 8000)),
            ceiling=Ceiling.read(directory),
            storage=_read_storage(directory),
            backend_options=_read_backends(directory),
            never_send=tuple(str(item) for item in (egress.get("never_send") or ())),
        )

    def scenario_for(self, trigger: Trigger) -> Scenario:
        for scenario in self.scenarios:
            if trigger in scenario.triggers:
                return scenario
        raise ConfigError(f"no scenario in {self.directory} handles trigger {trigger}")


def _read_backends(directory: Path) -> Mapping[str, Any]:
    """Settings per adapter. No role bindings here, and no model names either.

    The file exists for things the agent is responsible for — whether the SDK's own sandbox is used,
    for one — which is why a product may not set them from its overlay.
    """
    path = directory / "backends.yaml"
    raw = load_yaml_mapping(path)
    options = raw.get("backends") or {}
    if not isinstance(options, dict):
        raise ConfigError(f"{path}: backends must be a mapping")
    unknown = sorted(name for name in options if name not in ABILITIES)
    if unknown:
        known = ", ".join(sorted(ABILITIES))
        raise ConfigError(f"{path}: settings for unknown backend(s) {', '.join(unknown)}; {known}")
    return options


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
