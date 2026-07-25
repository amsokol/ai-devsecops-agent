"""The product overlay: values from `agent.yaml`, prose from `NOTES.md`.

Values are validated strictly and every message names the file and the key, because an overlay
mistake that passes silently changes a verdict without anyone noticing.

An overlay can be read from the checkout or from a commit. Which one a run uses is not a detail: the
overlay decides what counts as a problem in this repository, so a review must not read the copy the
change under review brought with it. See `orchestrator._overlay_for`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, Self

from agent.domain import Role, Trigger
from agent.errors import ConfigError
from agent.library import Library, parse_yaml_mapping

if TYPE_CHECKING:
    from agent.repo import Repository

SCHEMA = 1
VALUES_FILE = "agent.yaml"
NOTES_FILE = "NOTES.md"
CHECKOUT = "checkout"
EXCEPTION_SCOPES = frozenset({"quarantine"})
REVIEW = "review"
MAINTENANCE = "maintenance"
_TOP_LEVEL_KEYS = frozenset(
    {
        "schema",
        REVIEW,
        MAINTENANCE,
        "ecosystems",
        "hotspots",
        "quarantine",
        "exceptions",
        "verification",
    }
)
_SETTINGS_KEYS = frozenset({"models", "limits"})
_LIMIT_KEYS = frozenset({"tokens_per_run", "minutes_per_task", "tasks_at_once"})
_QUEUE_KEYS = frozenset({"max_new_issues_per_run", "max_open_fix_requests"})
_UNANSWERED = (
    "is required: what a run may spend and how much it may leave behind are the product's "
    "decisions, and a default in the agent would be the agent making them by omission"
)
_PAIR = "/"


@dataclass(frozen=True, slots=True)
class LocalException:
    """A documented deviation. Without a reason it is indistinguishable from an accident."""

    subject: str
    scope: str
    reason: str


@dataclass(frozen=True, slots=True)
class Choice:
    """A product's provider and model for one role, written as `provider/model`.

    One string rather than two keys: swapping the model is then the edit it looks like, and the
    provider stays visible because the model name means nothing without it.
    """

    backend: str
    model: str


@dataclass(frozen=True, slots=True)
class Limits:
    """What one kind of run may spend. `None` is a ceiling deliberately removed.

    Removed has to be written as `null`, because a missing key is a question nobody answered, and
    reading it as "no limit" would make the most expensive setting in the file the one nobody typed.
    """

    tokens_per_run: int | None
    minutes_per_task: int
    tasks_at_once: int

    @property
    def seconds_per_task(self) -> int:
        """Minutes are what a person states; seconds are what a clock counts."""
        return self.minutes_per_task * 60


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything one kind of run is configured with: who does the work, and what it may spend.

    Grouped by kind of run rather than by kind of setting. The earlier shape had models split by
    role, spending split by whether anybody was waiting, and volume split by neither — three ways of
    slicing one file, two of which read as "maintenance". Whoever edits this has one question in
    mind, "what happens when the agent reviews a change" or "…when it maintains the branch", and now
    each answer is in one place.
    """

    models: Mapping[Role, Choice]
    limits: Limits


@dataclass(frozen=True, slots=True)
class Queue:
    """How much a maintenance run may leave behind: issues opened, fix branches awaiting review.

    Not spending but volume — how loud the agent is allowed to be. Reaching a limit defers work to
    the next run and says so in the record; it never drops a finding.
    """

    max_new_issues_per_run: int
    max_open_fix_requests: int


@dataclass(frozen=True, slots=True)
class Overlay:
    path: Path
    origin: str
    """Which copy this is: the checkout, or the commit it was read from. Recorded in the manifest,
    because two runs on one repository can legitimately obey different overlays."""
    ecosystems: tuple[str, ...]
    hotspots: tuple[str, ...]
    quarantine_days: int
    review: Settings
    """Models and spending for a run that judges a change. The agent ships neither: a product
    outlives any provider, and a model named inside the agent would make switching one a fork."""
    maintenance: Settings
    """The same for a run that maintains the default branch, whether it woke on a schedule or
    somebody started it. Both do identical work, so both are held to one set of numbers."""
    queue: Queue
    exceptions: tuple[LocalException, ...]
    verification: dict[str, tuple[tuple[str, ...], ...]]
    notes: str
    digest: str
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def settings_for(self, trigger: Trigger) -> Settings:
        return self.maintenance if trigger.is_maintenance else self.review

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        library: Library,
        notes_limit: int,
    ) -> Self:
        """The overlay as it is on disk."""
        path = path.resolve()
        if not path.is_dir():
            raise ConfigError(f"overlay path {path} is not a directory")
        values_path = path / VALUES_FILE
        if not values_path.is_file():
            raise ConfigError(f"{values_path} is missing")
        notes_path = path / NOTES_FILE
        return cls._parse(
            values_path.read_text(encoding="utf-8"),
            notes_path.read_text(encoding="utf-8") if notes_path.is_file() else "",
            path=path,
            origin=CHECKOUT,
            named=str(values_path),
            library=library,
            notes_limit=notes_limit,
        )

    @classmethod
    def at(
        cls,
        repository: Repository,
        ref: str,
        *,
        path: Path,
        library: Library,
        notes_limit: int,
    ) -> Self | None:
        """The overlay as a commit has it, or `None` when that commit has no overlay there.

        `None` is not a failure: a change that introduces the overlay has nothing to read at the
        base, and so does a run whose overlay is kept outside the repository entirely.
        """
        inside = _inside(repository.path, path)
        if inside is None:
            return None
        values = repository.file_at(ref, _named(inside, VALUES_FILE))
        if values is None:
            return None
        return cls._parse(
            values,
            repository.file_at(ref, _named(inside, NOTES_FILE)) or "",
            path=path,
            origin=ref,
            named=f"{_named(inside, VALUES_FILE)} at {ref}",
            library=library,
            notes_limit=notes_limit,
        )

    @classmethod
    def _parse(
        cls,
        values: str,
        notes: str,
        *,
        path: Path,
        origin: str,
        named: str,
        library: Library,
        notes_limit: int,
    ) -> Self:
        raw = parse_yaml_mapping(values, named=named)
        reader = _Reader(named, raw)

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
        quarantine_days = _Reader(named, quarantine, prefix="quarantine").integer("days", minimum=0)

        review = _read_settings(reader, named, kind=REVIEW)
        maintenance = _read_settings(reader, named, kind=MAINTENANCE)

        warnings: list[str] = []
        if len(notes) > notes_limit:
            warnings.append(
                f"{path / NOTES_FILE} is {len(notes)} characters, over the {notes_limit} limit; "
                "notes enter every task's context, so the excess is paid for on every run"
            )

        return cls(
            path=path,
            origin=origin,
            ecosystems=ecosystems,
            hotspots=reader.strings("hotspots"),
            quarantine_days=quarantine_days,
            review=review,
            maintenance=maintenance,
            queue=_read_queue(reader, named),
            exceptions=_read_exceptions(reader),
            verification=_read_verification(reader),
            notes=notes,
            digest=digest_of(values, notes),
            warnings=tuple(warnings),
        )

    def exception_for(self, subject: str, scope: str) -> LocalException | None:
        for entry in self.exceptions:
            if entry.subject == subject and entry.scope == scope:
                return entry
        return None


class _Reader:
    """Typed access to a YAML mapping, with errors that name the source and the key."""

    def __init__(self, named: str, raw: dict[str, Any], *, prefix: str = "") -> None:
        self.named = named
        self.raw = raw
        self.prefix = prefix

    def where(self, key: str = "") -> str:
        parts = [part for part in (self.prefix, key) if part]
        return ".".join(parts)

    def fail(self, message: str, key: str = "") -> NoReturn:
        location = self.where(key)
        raise ConfigError(f"{self.named}: {location + ': ' if location else ''}{message}")

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

    def text(self, key: str) -> str:
        value = self.raw.get(key)
        if not isinstance(value, str) or not value.strip():
            self.fail(f"must be a non-empty string, got {value!r}", key)
        return value.strip()

    def count(self, key: str) -> int:
        """A number that must be there and must be a number. No ceiling is not an option here."""
        if key not in self.raw:
            self.fail(_UNANSWERED, key)
        value = self.raw[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            self.fail(f"must be a positive integer, got {value!r}", key)
        return int(value)

    def ceiling(self, key: str) -> int | None:
        """A limit that may be absent, but only in writing.

        `null` is a decision on the record — somebody chose to run without this ceiling. A missing
        key is not the same thing, and treating it as one would let the most expensive setting in
        the file be the one nobody ever typed.
        """
        if key not in self.raw:
            self.fail(_UNANSWERED, key)
        value = self.raw[key]
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            self.fail(f"must be a positive integer, or null for no ceiling, got {value!r}", key)
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


def _read_settings(reader: _Reader, named: str, *, kind: str) -> Settings:
    """One kind of run, described in one place: its models and what it may spend.

    Both kinds are stated in full and neither inherits from the other. Six extra lines buy the
    property that matters when somebody edits this file: everything a run does is visible where that
    run is named, rather than assembled in the reader's head from sections elsewhere.
    """
    raw = reader.mapping(kind, required=True)
    inner = _Reader(named, raw, prefix=kind)
    # `queue` lives under `maintenance` in the file but is read separately, so it is allowed here
    # without being a setting of the same nature as models or spending.
    allowed = _SETTINGS_KEYS | ({"queue"} if kind == MAINTENANCE else frozenset())
    unknown = sorted(raw.keys() - allowed)
    if unknown:
        known = ", ".join(sorted(allowed))
        inner.fail(f"unknown key(s) {', '.join(unknown)}; known keys are {known}")
    return Settings(
        models=_read_models(inner, named),
        limits=_read_limits(inner, named),
    )


def _read_models(reader: _Reader, named: str) -> Mapping[Role, Choice]:
    """Which pair answers for each role of this kind of run. Required: a run cannot invent one.

    Named per kind of run rather than once for the file, because "cheap model for the weekly sweep,
    the careful one for a change somebody is waiting on" is a decision products actually make. The
    cost is that a role used by both kinds is written twice.

    Whether a named backend exists and can do the role's work is checked where the adapters are
    known — see `config.bind` — but the shape is settled here, next to every other value the product
    supplies.
    """
    raw = reader.mapping("models", required=True)
    if not raw:
        reader.fail(
            "must name at least one role: a run with no model bound cannot analyse anything",
            "models",
        )
    inner = _Reader(named, raw, prefix=reader.where("models"))
    chosen: dict[Role, Choice] = {}
    for name in raw:
        try:
            role = Role(str(name))
        except ValueError:
            known = ", ".join(item.value for item in Role)
            inner.fail(f"unknown role (known roles: {known})", str(name))
        line = inner.text(str(name))
        provider, _, model = line.partition(_PAIR)
        if not provider.strip() or not model.strip():
            inner.fail(
                f"{line!r} is not a pair; write it as provider{_PAIR}model, because the provider "
                "decides which models exist and a model name alone is not an address",
                str(name),
            )
        chosen[role] = Choice(backend=provider.strip(), model=model.strip())
    return chosen


def _read_limits(reader: _Reader, named: str) -> Limits:
    raw = reader.mapping("limits", required=True)
    inner = _Reader(named, raw, prefix=reader.where("limits"))
    unknown = sorted(raw.keys() - _LIMIT_KEYS)
    if unknown:
        known = ", ".join(sorted(_LIMIT_KEYS))
        inner.fail(f"unknown key(s) {', '.join(unknown)}; known keys are {known}")
    return Limits(
        tokens_per_run=inner.ceiling("tokens_per_run"),
        minutes_per_task=inner.count("minutes_per_task"),
        tasks_at_once=inner.count("tasks_at_once"),
    )


def _read_queue(reader: _Reader, named: str) -> Queue:
    """How much a maintenance run may leave behind. Required of every product the agent maintains.

    Which is every product that schedules a run at all, and a number invented here would decide for
    somebody else how loud their tracker gets.
    """
    where = f"{MAINTENANCE}.queue"
    raw = reader.mapping(MAINTENANCE, required=True).get("queue")
    if not isinstance(raw, dict):
        reader.fail(f"{where} is required and must be a mapping")
    inner = _Reader(named, raw, prefix=where)
    unknown = sorted(raw.keys() - _QUEUE_KEYS)
    if unknown:
        known = ", ".join(sorted(_QUEUE_KEYS))
        inner.fail(f"unknown key(s) {', '.join(unknown)}; known keys are {known}")
    return Queue(
        max_new_issues_per_run=inner.count("max_new_issues_per_run"),
        max_open_fix_requests=inner.count("max_open_fix_requests"),
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


def digest_of(values: str, notes: str) -> str:
    """The identity of an overlay's contents, wherever they were read from.

    Comparable across sources on purpose: it is how a run tells "the change edits the overlay" from
    "the change leaves it alone" without parsing the copy it is not going to obey.
    """
    hasher = hashlib.sha256()
    for name, text in ((VALUES_FILE, values), (NOTES_FILE, notes)):
        hasher.update(name.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(text.encode("utf-8"))
        hasher.update(b"\n")
    return "sha256:" + hasher.hexdigest()


def digest_on_disk(path: Path) -> str | None:
    """The digest of the overlay in the checkout, without parsing it.

    `None` when there is no `agent.yaml` there. Deliberately tolerant of an overlay that would not
    load: a change that breaks its own overlay is something to report, not something that should
    stop a run reading a perfectly good overlay from the base.
    """
    values = path / VALUES_FILE
    if not values.is_file():
        return None
    notes = path / NOTES_FILE
    return digest_of(
        values.read_text(encoding="utf-8"),
        notes.read_text(encoding="utf-8") if notes.is_file() else "",
    )


def within(repository: Path, path: Path) -> bool:
    """Whether a change request to this repository could edit this overlay."""
    return _inside(repository, path) is not None


def _inside(repository: Path, path: Path) -> str | None:
    """The overlay's directory as git names it, or `None` when it sits outside the repository.

    Outside is a real deployment, not a mistake: an overlay kept next to the workflow that runs the
    agent is one nobody can edit by opening a change request here. An empty string is the repository
    root, which is why the caller joins with `_named` rather than an f-string.
    """
    try:
        relative = path.resolve().relative_to(repository.resolve())
    except ValueError:
        return None
    return relative.as_posix() if relative.parts else ""


def _named(directory: str, file: str) -> str:
    return f"{directory}/{file}" if directory else file
