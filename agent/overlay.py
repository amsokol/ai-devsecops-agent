"""The product overlay: values from `agent.yaml`, prose from `NOTES.md`.

Values are validated strictly and every message names the file and the key, because an overlay
mistake that passes silently changes a verdict without anyone noticing.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn, Self

from agent.errors import ConfigError
from agent.library import Library, load_yaml_mapping

SCHEMA = 1
VALUES_FILE = "agent.yaml"
NOTES_FILE = "NOTES.md"
EXCEPTION_SCOPES = frozenset({"quarantine"})
_TOP_LEVEL_KEYS = frozenset(
    {"schema", "ecosystems", "hotspots", "quarantine", "maintenance", "exceptions", "verification"}
)


@dataclass(frozen=True, slots=True)
class LocalException:
    """A documented deviation. Without a reason it is indistinguishable from an accident."""

    subject: str
    scope: str
    reason: str


@dataclass(frozen=True, slots=True)
class Limits:
    """Defaults live in the agent's configuration; the overlay may narrow or widen them."""

    open_change_requests: int
    new_issues_per_run: int


@dataclass(frozen=True, slots=True)
class Overlay:
    path: Path
    ecosystems: tuple[str, ...]
    hotspots: tuple[str, ...]
    quarantine_days: int
    limits: Limits
    exceptions: tuple[LocalException, ...]
    verification: dict[str, tuple[tuple[str, ...], ...]]
    notes: str
    digest: str
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        library: Library,
        default_limits: Limits,
        notes_limit: int,
    ) -> Self:
        path = path.resolve()
        if not path.is_dir():
            raise ConfigError(f"overlay path {path} is not a directory")
        values_path = path / VALUES_FILE
        raw = load_yaml_mapping(values_path)
        reader = _Reader(values_path, raw)

        unknown = sorted(raw.keys() - _TOP_LEVEL_KEYS)
        if unknown:
            known = ", ".join(sorted(_TOP_LEVEL_KEYS))
            reader.fail(f"unknown key(s) {', '.join(unknown)}; known keys are {known}")

        schema = reader.integer("schema", minimum=1)
        if schema != SCHEMA:
            reader.fail(f"schema {schema} is not supported; this agent reads schema {SCHEMA}")

        ecosystems = reader.strings("ecosystems")
        _check_ecosystems(reader, ecosystems, library)

        quarantine = reader.mapping("quarantine", required=True)
        quarantine_days = _Reader(values_path, quarantine, prefix="quarantine").integer(
            "days", minimum=0
        )

        maintenance = reader.mapping("maintenance", required=False)
        limits = default_limits
        if maintenance:
            sub = _Reader(values_path, maintenance, prefix="maintenance")
            limits = Limits(
                open_change_requests=sub.integer(
                    "open_change_requests",
                    minimum=1,
                    default=default_limits.open_change_requests,
                ),
                new_issues_per_run=sub.integer(
                    "new_issues_per_run",
                    minimum=1,
                    default=default_limits.new_issues_per_run,
                ),
            )

        notes_path = path / NOTES_FILE
        notes = notes_path.read_text(encoding="utf-8") if notes_path.is_file() else ""
        warnings: list[str] = []
        if len(notes) > notes_limit:
            warnings.append(
                f"{notes_path} is {len(notes)} characters, over the {notes_limit} limit; "
                "notes enter every task's context, so the excess is paid for on every run"
            )

        return cls(
            path=path,
            ecosystems=ecosystems,
            hotspots=reader.strings("hotspots"),
            quarantine_days=quarantine_days,
            limits=limits,
            exceptions=_read_exceptions(reader),
            verification=_read_verification(reader),
            notes=notes,
            digest=_digest(values_path, notes_path),
            warnings=tuple(warnings),
        )

    def exception_for(self, subject: str, scope: str) -> LocalException | None:
        for entry in self.exceptions:
            if entry.subject == subject and entry.scope == scope:
                return entry
        return None


class _Reader:
    """Typed access to a YAML mapping, with errors that name the file and the key."""

    def __init__(self, path: Path, raw: dict[str, Any], *, prefix: str = "") -> None:
        self.path = path
        self.raw = raw
        self.prefix = prefix

    def where(self, key: str = "") -> str:
        parts = [part for part in (self.prefix, key) if part]
        return ".".join(parts)

    def fail(self, message: str, key: str = "") -> NoReturn:
        location = self.where(key)
        raise ConfigError(f"{self.path}: {location + ': ' if location else ''}{message}")

    def integer(self, key: str, *, minimum: int, default: int | None = None) -> int:
        if key not in self.raw:
            if default is None:
                self.fail("is required", key)
            return int(default)
        value = self.raw[key]
        if isinstance(value, bool) or not isinstance(value, int):
            self.fail(f"must be an integer, got {value!r}", key)
        if int(value) < minimum:
            self.fail(f"must be at least {minimum}, got {value}", key)
        return int(value)

    def strings(self, key: str) -> tuple[str, ...]:
        value = self.raw.get(key)
        if value is None:
            return ()
        if not isinstance(value, list):
            self.fail(f"must be a list, got {type(value).__name__}", key)
        items: list[str] = []
        for position, item in enumerate(value):
            if not isinstance(item, str) or not item.strip():
                self.fail(f"[{position}] must be a non-empty string, got {item!r}", key)
            items.append(str(item).strip())
        duplicates = sorted({item for item in items if items.count(item) > 1})
        if duplicates:
            self.fail(f"has duplicate entries: {', '.join(duplicates)}", key)
        return tuple(items)

    def mapping(self, key: str, *, required: bool) -> dict[str, Any]:
        value = self.raw.get(key)
        if value is None:
            if required:
                self.fail("is required", key)
            return {}
        if not isinstance(value, dict):
            self.fail(f"must be a mapping, got {type(value).__name__}", key)
        return dict(value)

    def sequence(self, key: str) -> list[Any]:
        value = self.raw.get(key)
        if value is None:
            return []
        if not isinstance(value, list):
            self.fail(f"must be a list, got {type(value).__name__}", key)
        return list(value)


def _check_ecosystems(reader: _Reader, ecosystems: tuple[str, ...], library: Library) -> None:
    available = sorted(doc.id for doc in library.by_kind("ecosystem"))
    for position, ecosystem in enumerate(ecosystems):
        if ecosystem not in library:
            reader.fail(
                f"[{position}] {ecosystem!r} is not in the library index; "
                f"available: {', '.join(available)}",
                "ecosystems",
            )
        if library.get(ecosystem).kind != "ecosystem":
            reader.fail(
                f"[{position}] {ecosystem!r} is a {library.get(ecosystem).kind} document, "
                "not an ecosystem",
                "ecosystems",
            )


def _read_exceptions(reader: _Reader) -> tuple[LocalException, ...]:
    entries: list[LocalException] = []
    for position, item in enumerate(reader.sequence("exceptions")):
        key = f"exceptions[{position}]"
        if not isinstance(item, dict):
            reader.fail(f"{key} must be a mapping with subject, scope and reason")
        missing = sorted({"subject", "scope", "reason"} - item.keys())
        if missing:
            reader.fail(f"{key} is missing {', '.join(missing)}")
        scope = str(item["scope"])
        if scope not in EXCEPTION_SCOPES:
            recognised = ", ".join(sorted(EXCEPTION_SCOPES))
            reader.fail(f"{key}.scope {scope!r} is unknown; recognised: {recognised}")
        reason = str(item["reason"]).strip()
        if not reason:
            reader.fail(f"{key}.reason is empty; an exception without a reason is an accident")
        entries.append(
            LocalException(subject=str(item["subject"]).strip(), scope=scope, reason=reason)
        )
    return tuple(entries)


def _read_verification(reader: _Reader) -> dict[str, tuple[tuple[str, ...], ...]]:
    surfaces: dict[str, tuple[tuple[str, ...], ...]] = {}
    for surface, commands in reader.mapping("verification", required=False).items():
        key = f"verification.{surface}"
        if not isinstance(commands, list) or not commands:
            reader.fail(f"{key} must be a non-empty list of commands")
        parsed: list[tuple[str, ...]] = []
        for position, command in enumerate(commands):
            if not isinstance(command, list) or not command:
                reader.fail(
                    f"{key}[{position}] must be a non-empty list of arguments, "
                    "for example [uv, run, pytest]; there is no shell, so a command string "
                    "cannot be used"
                )
            arguments = [str(argument) for argument in command]
            if any(not argument.strip() for argument in arguments):
                reader.fail(f"{key}[{position}] has an empty argument")
            parsed.append(tuple(arguments))
        surfaces[str(surface)] = tuple(parsed)
    return surfaces


def _digest(values: Path, notes: Path) -> str:
    hasher = hashlib.sha256()
    for path in (values, notes):
        hasher.update(path.name.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(path.read_bytes() if path.is_file() else b"")
        hasher.update(b"\n")
    return "sha256:" + hasher.hexdigest()
