"""The tools a subagent may call, as one registry.

Transport-neutral on purpose. A backend exposes this registry the way its SDK expects — in-process
callables for one, an MCP server for another — but there is one implementation of what the tools do,
so a second adapter inherits the guarantees instead of reimplementing them.

Two properties are enforced here rather than requested in a prompt:

* a fact must come from a call. `record_fact` accepts only call identifiers this task actually
  made, so an evidence key cannot be produced by a model that decided to skip the work;
* reliability is derived from the call, never chosen. A command or a JSON API answer is
  reproducible; a scraped page is heuristic. Because the model cannot pick the origin, it cannot
  promote its own finding into one that blocks.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from agent.domain import PlannedTask, Reason
from agent.evidence import Evidence, Origin, Question, Subject
from agent.session import Session, TaskTools
from agent.tools import (
    HostNotPermitted,
    NotPermitted,
    OutsideRepository,
    Withheld,
    compare_versions,
)
from agent.tools.dates import quarantine

STATEABLE_REASONS = frozenset(
    {Reason.NO_TOOLING, Reason.UNAVAILABLE, Reason.UNEXPECTED_SHAPE, Reason.NOT_PERMITTED}
)


class Refused(Exception):
    """The call is not allowed. Returned to the subagent as an error, and recorded."""


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    schema: dict[str, Any]
    run: Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(slots=True)
class Call:
    id: str
    tool: str
    origin: Origin
    source: str
    at: str
    ok: bool
    detail: str = ""

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tool": self.tool,
            "origin": self.origin.value,
            "source": self.source,
            "at": self.at,
            "ok": self.ok,
            "detail": self.detail,
        }


class Toolkit:
    """One task's tools, its call ledger, and the facts recorded from them."""

    def __init__(
        self,
        *,
        session: Session,
        task: PlannedTask,
        now: datetime,
        quarantine_days: int,
        step_limit: int | None = None,
    ) -> None:
        self.task = task
        self.now = now
        self.quarantine_days = quarantine_days
        self.step_limit = step_limit
        self._session = session
        self._tools: TaskTools = session.for_task(task.id)
        self._calls: list[Call] = []

    @property
    def calls(self) -> tuple[Call, ...]:
        return tuple(self._calls)

    def as_json(self) -> list[dict[str, Any]]:
        return [call.as_json() for call in self._calls]

    def tools(self) -> tuple[Tool, ...]:
        return (
            Tool(
                name="list_files",
                description=(
                    "List files in the repository, optionally filtered by a glob such as "
                    "'**/*.py'. Paths are relative to the repository root."
                ),
                schema=_schema({"glob": _string("glob pattern, defaults to everything")}),
                run=self._list_files,
            ),
            Tool(
                name="read_file",
                description=(
                    "Read a text file from the repository. Large files are truncated rather than "
                    "refused."
                ),
                schema=_schema(
                    {"path": _string("path relative to the repository root")}, required=["path"]
                ),
                run=self._read_file,
            ),
            Tool(
                name="search_text",
                description=(
                    "Search the repository with a regular expression and return matching lines "
                    "with their line numbers."
                ),
                schema=_schema(
                    {
                        "pattern": _string("Python regular expression"),
                        "glob": _string("limit the search to matching files"),
                    },
                    required=["pattern"],
                ),
                run=self._search_text,
            ),
            Tool(
                name="run_command",
                description=(
                    "Run one allowlisted binary with arguments. There is no shell: no pipes, no "
                    "redirection, no chaining. Binaries come from the ecosystem's requirements."
                ),
                schema=_schema(
                    {
                        "command": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "binary first, then its arguments",
                        },
                        "in_scratch": {
                            "type": "boolean",
                            "description": (
                                "run in a scratch directory instead of the repository, for probes "
                                "that write files"
                            ),
                        },
                    },
                    required=["command"],
                ),
                run=self._run_command,
            ),
            Tool(
                name="fetch",
                description=(
                    "GET an allowlisted https URL with no credentials. A response that parses as "
                    "JSON counts as an API answer and is reproducible; anything else is a page and "
                    "is heuristic."
                ),
                schema=_schema({"url": _string("https URL")}, required=["url"]),
                run=self._fetch,
            ),
            Tool(
                name="compare_versions",
                description=(
                    "Order two version strings in one ecosystem's scheme and name the step between "
                    "them. Never reason about version order yourself."
                ),
                schema=_schema(
                    {
                        "ecosystem": _string("ecosystem document id"),
                        "left": _string("first version"),
                        "right": _string("second version"),
                    },
                    required=["ecosystem", "left", "right"],
                ),
                run=self._compare_versions,
            ),
            Tool(
                name="check_quarantine",
                description=(
                    "Given a publication timestamp, say whether the quarantine window has elapsed. "
                    "Never compute elapsed days yourself."
                ),
                schema=_schema(
                    {
                        "published_at": _string("ISO 8601 timestamp"),
                        "heuristic": {
                            "type": "boolean",
                            "description": (
                                "true when the timestamp came from a page rather than an API, "
                                "which makes clearing the window require an unambiguous margin"
                            ),
                        },
                    },
                    required=["published_at"],
                ),
                run=self._check_quarantine,
            ),
            Tool(
                name="known_fact",
                description=(
                    "Ask whether this run already established a fact, including from the cache of "
                    "immutable facts. Call this before acquiring anything expensive."
                ),
                schema=_schema(
                    {"question": _QUESTION, "subject": _SUBJECT},
                    required=["question", "subject"],
                ),
                run=self._known_fact,
            ),
            Tool(
                name="record_fact",
                description=(
                    "Record a fact you established, citing the calls that produced it, and receive "
                    "the evidence key to put in a finding. Only calls made in this task are "
                    "accepted."
                ),
                schema=_schema(
                    {
                        "question": _QUESTION,
                        "subject": _SUBJECT,
                        "value": {"description": "the value obtained"},
                        "calls": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "identifiers returned by the calls this rests on",
                        },
                    },
                    required=["question", "subject", "value", "calls"],
                ),
                run=self._record_fact,
            ),
            Tool(
                name="record_gap",
                description=(
                    "Record that a fact could not be established, with the reason. An honest "
                    "gap is a useful answer; a guess is not."
                ),
                schema=_schema(
                    {
                        "question": _QUESTION,
                        "subject": _SUBJECT,
                        "reason": {
                            "type": "string",
                            "enum": sorted(item.value for item in STATEABLE_REASONS),
                        },
                        "calls": {"type": "array", "items": {"type": "string"}},
                    },
                    required=["question", "subject", "reason"],
                ),
                run=self._record_gap,
            ),
        )

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Dispatch by name. An unknown tool is an error, never a silent no-op."""
        if self.step_limit is not None and len(self._calls) >= self.step_limit:
            # Told to the subagent rather than killed silently: it still has to write a result file,
            # and the honest one here is `exhausted` — the check did not finish.
            raise Refused(
                f"this task's step budget of {self.step_limit} calls is spent. Write the result "
                "file now: `exhausted` if you could not finish the check, or `findings` / `clean` "
                "if what you already established answers it."
            )
        for tool in self.tools():
            if tool.name == name:
                return tool.run(arguments)
        raise Refused(f"there is no tool named {name!r}")

    # Acquisition -----------------------------------------------------------------

    def _list_files(self, arguments: dict[str, Any]) -> dict[str, Any]:
        pattern = str(arguments.get("glob") or "**/*")
        files = self._tools.files.list_files(pattern)
        call = self._record_call("list_files", Origin.TOOL, f"glob:{pattern}", ok=True)
        return {"call": call.id, "files": list(files)}

    def _read_file(self, arguments: dict[str, Any]) -> dict[str, Any]:
        path = _required(arguments, "path")
        try:
            text = self._tools.files.read_file(path)
        except Withheld:
            self._record_call("read_file", Origin.TOOL, path, ok=False, detail="never-send")
            raise Refused(
                f"{path!r} is on the never-send list: its contents are not sent to a model under "
                "any circumstances. Reason about it from its path and its metadata instead."
            ) from None
        except OutsideRepository as error:
            self._record_call("read_file", Origin.TOOL, path, ok=False, detail=str(error))
            raise Refused(str(error)) from None
        except OSError as error:
            self._record_call("read_file", Origin.TOOL, path, ok=False, detail=str(error))
            raise Refused(f"{path}: {error}") from None
        call = self._record_call("read_file", Origin.TOOL, path, ok=True)
        return {"call": call.id, "path": path, "text": text}

    def _search_text(self, arguments: dict[str, Any]) -> dict[str, Any]:
        pattern = _required(arguments, "pattern")
        glob = str(arguments.get("glob") or "**/*")
        try:
            matches = self._tools.files.search_text(pattern, glob=glob)
        except ValueError as error:
            raise Refused(str(error)) from None
        call = self._record_call("search_text", Origin.TOOL, f"/{pattern}/ in {glob}", ok=True)
        return {
            "call": call.id,
            "matches": [
                {"path": match.path, "line": match.line, "text": match.text} for match in matches
            ],
        }

    def _run_command(self, arguments: dict[str, Any]) -> dict[str, Any]:
        raw = arguments.get("command")
        if not isinstance(raw, list) or not raw or not all(isinstance(item, str) for item in raw):
            raise Refused("command must be a non-empty list of strings")
        command = tuple(str(item) for item in raw)
        source = " ".join(command)
        try:
            result = self._tools.commands.run(command, in_scratch=bool(arguments.get("in_scratch")))
        except NotPermitted as error:
            self._record_call("run_command", Origin.TOOL, source, ok=False, detail=str(error))
            raise Refused(str(error)) from None
        except FileNotFoundError as error:
            self._record_call("run_command", Origin.TOOL, source, ok=False, detail=str(error))
            raise Refused(str(error)) from None
        call = self._record_call(
            "run_command",
            Origin.TOOL,
            source,
            ok=result.succeeded,
            detail="" if result.succeeded else f"exit {result.exit_code}",
        )
        return {
            "call": call.id,
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }

    def _fetch(self, arguments: dict[str, Any]) -> dict[str, Any]:
        url = _required(arguments, "url")
        try:
            response = self._tools.http.get(url)
        except HostNotPermitted as error:
            self._record_call("fetch", Origin.WEB, url, ok=False, detail=str(error))
            raise Refused(str(error)) from None
        except OSError as error:
            self._record_call("fetch", Origin.WEB, url, ok=False, detail=str(error))
            raise Refused(f"{url}: {error}") from None
        parsed: Any = None
        try:
            parsed = json.loads(response.body)
        except ValueError:
            parsed = None
        # The distinction is mechanical, and that is the point: a page cannot be presented as an API
        # answer to earn the right to block.
        origin = Origin.API if parsed is not None else Origin.WEB
        call = self._record_call("fetch", origin, response.url, ok=True)
        payload: dict[str, Any] = {
            "call": call.id,
            "url": response.url,
            "status": response.status,
            "kind": "api" if origin is Origin.API else "page",
            "truncated": response.truncated,
        }
        if parsed is not None:
            payload["json"] = parsed
        else:
            payload["text"] = response.body
        return payload

    # Arithmetic ------------------------------------------------------------------

    def _compare_versions(self, arguments: dict[str, Any]) -> dict[str, Any]:
        ecosystem = _required(arguments, "ecosystem")
        left, right = _required(arguments, "left"), _required(arguments, "right")
        comparison = compare_versions(ecosystem, left, right)
        call = self._record_call(
            "compare_versions", Origin.TOOL, f"{left} vs {right} ({ecosystem})", ok=True
        )
        return {
            "call": call.id,
            "ordered": not comparison.unordered,
            "order": comparison.order,
            "step": comparison.step.value if comparison.step else None,
        }

    def _check_quarantine(self, arguments: dict[str, Any]) -> dict[str, Any]:
        published = _timestamp(_required(arguments, "published_at"))
        heuristic = bool(arguments.get("heuristic"))
        answer = quarantine(
            published,
            days=self.quarantine_days,
            now=self.now,
            margin_days=1.0 if heuristic else 0.0,
        )
        source = f"{published.isoformat()}+{self.quarantine_days}d"
        call = self._record_call("check_quarantine", Origin.TOOL, source, ok=True)
        return {
            "call": call.id,
            "cleared": answer.cleared,
            "age_days": round(answer.age_days, 2),
            "window_days": self.quarantine_days,
            "clears_at": answer.clears_at.isoformat(),
            "phrase": answer.phrase(),
        }

    # Evidence --------------------------------------------------------------------

    def _known_fact(self, arguments: dict[str, Any]) -> dict[str, Any]:
        question = _question(arguments)
        subject = self._subject(arguments.get("subject"))
        known = self._session.evidence.find(question, subject)
        if known is None:
            cached = self._session.cache.get(question, subject)
            known = self._session.evidence.add(cached) if cached is not None else None
        if known is None or not known.is_verified:
            return {"found": False}
        return {"found": True, "key": known.key, "value": known.value, "source": known.source}

    def _record_fact(self, arguments: dict[str, Any]) -> dict[str, Any]:
        question = _question(arguments)
        subject = self._subject(arguments.get("subject"))
        cited = self._cited(arguments.get("calls"))
        if "value" not in arguments or arguments["value"] is None:
            raise Refused("a fact needs a value; if there is nothing to record, use record_gap")
        record = Evidence.verified(
            question=question,
            subject=subject,
            value=arguments["value"],
            origin=_weakest(cited),
            source="; ".join(call.source for call in cited),
            observed_at=self.now,
            recipe=f"{self.task.capability}@{'+'.join(call.tool for call in cited)}",
        )
        stored = self._session.evidence.add(record)
        self._session.cache.put(stored)
        return {"key": stored.key, "reliability": stored.reliability.value}

    def _record_gap(self, arguments: dict[str, Any]) -> dict[str, Any]:
        question = _question(arguments)
        subject = self._subject(arguments.get("subject"))
        raw = _required(arguments, "reason")
        try:
            reason = Reason(raw)
        except ValueError:
            raise Refused(f"unknown reason {raw!r}") from None
        if reason not in STATEABLE_REASONS:
            allowed = ", ".join(sorted(item.value for item in STATEABLE_REASONS))
            raise Refused(f"reason {raw!r} is not one a task may state ({allowed})")
        cited = self._cited(arguments.get("calls"), required=False)
        record = self._session.evidence.add(
            Evidence.unverified(
                question=question,
                subject=subject,
                reason=reason,
                origin=_weakest(cited) if cited else Origin.TOOL,
                source="; ".join(call.source for call in cited) or "not attempted",
                observed_at=self.now,
            )
        )
        return {"key": record.key, "recorded": True}

    # Internals -------------------------------------------------------------------

    def _record_call(
        self, tool: str, origin: Origin, source: str, ok: bool, detail: str = ""
    ) -> Call:
        call = Call(
            id=f"c{len(self._calls) + 1}",
            tool=tool,
            origin=origin,
            source=source,
            at=self.now.isoformat(),
            ok=ok,
            detail=detail,
        )
        self._calls.append(call)
        return call

    def _subject(self, raw: Any) -> Subject:
        """Read a subject, and settle its ecosystem from the task rather than from the model.

        A task runs for one ecosystem. Accepting `python-uv` in one record and
        `ecosystems/python-uv` in the next would give one package two identities, and two identities
        means a finding that reappears next week as a new one.
        """
        if not isinstance(raw, dict):
            raise Refused("subject must be an object")
        subject = Subject(
            ecosystem=self.task.ecosystem or _optional(raw.get("ecosystem")),
            package=_optional(raw.get("package")),
            version=_optional(raw.get("version")),
            path=_optional(raw.get("path")),
        )
        if not (subject.package or subject.path):
            raise Refused("subject must name a package or a path")
        if subject.package and subject.path:
            # Otherwise the same fact about the same pin gets two keys depending on whether the
            # manifest was mentioned, and neither the cache nor next week's run recognises either.
            raise Refused(
                "a subject names a package or a path, not both. For a package, the manifest it was "
                "found in belongs in the finding's location, not in the subject"
            )
        return subject

    def _cited(self, raw: Any, *, required: bool = True) -> tuple[Call, ...]:
        if raw is None:
            raw = []
        if not isinstance(raw, list):
            raise Refused("calls must be a list of call identifiers")
        by_id = {call.id: call for call in self._calls}
        unknown = [str(item) for item in raw if str(item) not in by_id]
        if unknown:
            raise Refused(
                f"no call(s) {', '.join(unknown)} were made in this task. A fact must come from a "
                "call you actually made."
            )
        cited = tuple(by_id[str(item)] for item in raw)
        if required and not cited:
            raise Refused(
                "a fact needs at least one call behind it. If nothing could be obtained, use "
                "record_gap instead."
            )
        return cited


_SUBJECT = {
    "type": "object",
    "description": (
        "what the fact is about: a package, or a path in the repository. The ecosystem is taken "
        "from the task, so it does not need to be given"
    ),
    "properties": {
        "package": {"type": "string"},
        "version": {"type": "string"},
        "path": {"type": "string"},
    },
}

_QUESTION = {
    "type": "string",
    "enum": sorted(question.value for question in Question),
    "description": (
        "which question this answers, from the fixed vocabulary. The same question must have the "
        "same name in every run, which is what makes a fact reusable"
    ),
}


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


def _string(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


def _required(arguments: dict[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise Refused(f"{name} is required and must be a non-empty string")
    return value.strip()


def _question(arguments: dict[str, Any]) -> str:
    raw = _required(arguments, "question")
    try:
        return Question(raw).value
    except ValueError:
        known = ", ".join(sorted(question.value for question in Question))
        raise Refused(f"{raw!r} is not a question this run asks. Use one of: {known}") from None


def _optional(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _timestamp(raw: str) -> datetime:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise Refused(f"{raw!r} is not an ISO 8601 timestamp") from None


def _weakest(calls: tuple[Call, ...]) -> Origin:
    """A fact is only as demonstrated as its weakest source.

    `tool` and `api` are both reproducible, so the choice between them only affects what the record
    says it came from; one scraped page, however, makes the whole fact heuristic.
    """
    origins = {call.origin for call in calls}
    if Origin.WEB in origins:
        return Origin.WEB
    return Origin.API if Origin.API in origins else Origin.TOOL


@dataclass(frozen=True, slots=True)
class Toolkits:
    """Builds one toolkit per task, with the run's session and clock."""

    session: Session
    now: datetime
    quarantine_days: int

    def for_task(self, task: PlannedTask, *, step_limit: int | None = None) -> Toolkit:
        return Toolkit(
            session=self.session,
            task=task,
            now=self.now,
            quarantine_days=self.quarantine_days,
            step_limit=step_limit,
        )
