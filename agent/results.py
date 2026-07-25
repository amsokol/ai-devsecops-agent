"""Reading what a subagent produced.

The result is a JSON file, not the final assistant message. A message may be summarised, truncated
or chatty, and parsing prose for a decision that refuses merges is a known way to build something
that works until the day it does not.

Validation is strict and every rejection names the field. A malformed result is a failure, never a
clean bill of health: the core retries the task once and then records it as unverified.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent.domain import FixOutcome, Outcome, Reason
from agent.evidence import Subject
from agent.findings import Finding, Klass, Location, Severity

# Reasons a subagent may state. The others describe what the agent itself concluded, and a task
# claiming one of those would be describing a decision it does not make.
STATEABLE_REASONS = frozenset(
    {Reason.NO_TOOLING, Reason.UNAVAILABLE, Reason.UNEXPECTED_SHAPE, Reason.NOT_PERMITTED}
)
_RESULT_KEYS = frozenset({"outcome", "reason", "findings", "notes"})
_FIX_KEYS = frozenset({"outcome", "reason", "notes"})
_FINDING_KEYS = frozenset(
    {
        "capability",
        "class",
        "severity",
        "subject",
        "location",
        "summary",
        "rationale",
        "evidence",
        "remediation",
        "advisory",
        "symbol",
        "forbidden_state",
    }
)
_SUBJECT_KEYS = frozenset({"ecosystem", "package", "version", "path"})


class InvalidResult(Exception):
    """The result file is missing, unreadable, or does not describe a result."""


@dataclass(frozen=True, slots=True)
class TaskResult:
    outcome: Outcome
    findings: tuple[Finding, ...] = field(default_factory=tuple)
    reason: Reason | None = None
    notes: str = ""


def read_result(
    path: Path,
    *,
    capability: str,
    known_evidence: frozenset[str],
    ecosystem: str | None = None,
) -> TaskResult:
    """Parse and validate one task's result file.

    `known_evidence` is the set of records the run actually collected. A finding citing anything
    else invalidates the result rather than being quietly downgraded: a citation that points at
    nothing is either a fabrication or a bug, and both deserve a second attempt and a loud record,
    not a comment posted under a reference no one can follow.

    `ecosystem` settles the subject's ecosystem the same way the tools do. A finding's key has to
    mean the same thing across runs, and it cannot if one run spells the ecosystem `python-uv` and
    the next spells it `ecosystems/python-uv`.
    """
    raw = _document(path, keys=_RESULT_KEYS)
    outcome = _enum(Outcome, raw.get("outcome"), path=path, field_name="outcome")
    reason = (
        _enum(Reason, raw["reason"], path=path, field_name="reason")
        if raw.get("reason") is not None
        else None
    )
    if reason is not None and reason not in STATEABLE_REASONS:
        allowed = ", ".join(sorted(item.value for item in STATEABLE_REASONS))
        raise InvalidResult(
            f"{path}: reason {reason.value!r} is not one a task may state ({allowed})"
        )
    if outcome is Outcome.UNVERIFIED and reason is None:
        raise InvalidResult(f"{path}: outcome 'unverified' requires a reason")
    if outcome is Outcome.EXHAUSTED:
        reason = Reason.EXHAUSTED

    listed = _list(raw.get("findings"), path=path, field_name="findings")
    findings = tuple(
        _finding(
            item,
            path=path,
            position=position,
            capability=capability,
            known=known_evidence,
            ecosystem=ecosystem,
        )
        for position, item in enumerate(listed)
    )
    if outcome is Outcome.CLEAN and findings:
        raise InvalidResult(f"{path}: outcome 'clean' cannot carry findings")
    if outcome is Outcome.FINDINGS and not findings:
        raise InvalidResult(f"{path}: outcome 'findings' requires at least one finding")

    return TaskResult(
        outcome=outcome,
        findings=findings,
        reason=reason,
        notes=str(raw.get("notes") or "").strip(),
    )


@dataclass(frozen=True, slots=True)
class FixResult:
    """What a fix task claims. Everything factual about the change is read from the tree instead."""

    outcome: FixOutcome
    notes: str
    reason: Reason | None = None


def read_fix_result(path: Path) -> FixResult:
    """Parse and validate one fix task's result file.

    Deliberately small. A fix task's claims are prose for a human — what was changed, or why not —
    while the facts come from elsewhere: which files differ is read from the worktree, and whether
    verification ran is matched against the run's own record of executed commands. Asking the model
    to declare those too would only create a second version of them to disagree with the first.
    """
    raw = _document(path, keys=_FIX_KEYS)
    outcome = _enum(FixOutcome, raw.get("outcome"), path=path, field_name="outcome")
    reason = (
        _enum(Reason, raw["reason"], path=path, field_name="reason")
        if raw.get("reason") is not None
        else None
    )
    if reason is not None and reason not in STATEABLE_REASONS:
        allowed = ", ".join(sorted(item.value for item in STATEABLE_REASONS))
        raise InvalidResult(
            f"{path}: reason {reason.value!r} is not one a task may state ({allowed})"
        )
    if outcome is FixOutcome.UNVERIFIED and reason is None:
        raise InvalidResult(f"{path}: outcome 'unverified' requires a reason")
    if outcome is FixOutcome.EXHAUSTED:
        reason = Reason.EXHAUSTED
    notes = str(raw.get("notes") or "").strip()
    if outcome in {FixOutcome.FIXED, FixOutcome.REFUSED} and not notes:
        # A branch with no explanation, or a refusal with no reason, both leave a human to
        # reconstruct the session's reasoning from a diff. That was this task's job.
        raise InvalidResult(
            f"{path}: outcome {outcome.value!r} requires notes — what you changed, or why not"
        )
    return FixResult(outcome=outcome, notes=notes, reason=reason)


def _document(path: Path, *, keys: frozenset[str]) -> dict[str, Any]:
    if not path.is_file():
        raise InvalidResult(f"{path} was not written")
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise InvalidResult(f"{path}: {error}") from None
    if not isinstance(raw, dict):
        raise InvalidResult(f"{path}: expected an object at the top level")
    unknown = sorted(raw.keys() - keys)
    if unknown:
        raise InvalidResult(f"{path}: unknown field(s) {', '.join(unknown)}")
    return raw


def _finding(
    raw: Any,
    *,
    path: Path,
    position: int,
    capability: str,
    known: frozenset[str],
    ecosystem: str | None,
) -> Finding:
    where = f"{path}: findings[{position}]"
    if not isinstance(raw, dict):
        raise InvalidResult(f"{where} must be an object")
    unknown = sorted(raw.keys() - _FINDING_KEYS)
    if unknown:
        raise InvalidResult(f"{where}: unknown field(s) {', '.join(unknown)}")
    summary = _text(raw.get("summary"), where=where, field_name="summary")
    rationale = _text(raw.get("rationale"), where=where, field_name="rationale")
    cited = _list(raw.get("evidence"), path=path, field_name=f"{where} evidence")
    evidence = tuple(dict.fromkeys(str(item) for item in cited))
    unknown_records = [key for key in evidence if key not in known]
    if unknown_records:
        raise InvalidResult(
            f"{where}: evidence {', '.join(unknown_records)} was never recorded by this run"
        )
    return Finding(
        capability=str(raw.get("capability") or capability),
        klass=_enum(Klass, raw.get("class"), path=path, field_name=f"{where} class"),
        severity=_enum(Severity, raw.get("severity"), path=path, field_name=f"{where} severity"),
        subject=_subject(raw.get("subject"), where=where, ecosystem=ecosystem),
        summary=summary,
        rationale=rationale,
        evidence=evidence,
        remediation=str(raw.get("remediation") or "").strip(),
        location=_location(raw.get("location"), where=where),
        advisory=str(raw.get("advisory") or "").strip(),
        symbol=str(raw.get("symbol") or "").strip(),
        forbidden_state=bool(raw.get("forbidden_state", False)),
    )


def _subject(raw: Any, *, where: str, ecosystem: str | None = None) -> Subject:
    if not isinstance(raw, dict):
        raise InvalidResult(f"{where}: subject must be an object")
    unknown = sorted(raw.keys() - _SUBJECT_KEYS)
    if unknown:
        raise InvalidResult(f"{where}: subject has unknown field(s) {', '.join(unknown)}")
    subject = Subject(
        ecosystem=ecosystem or _optional(raw.get("ecosystem")),
        package=_optional(raw.get("package")),
        version=_optional(raw.get("version")),
        path=_optional(raw.get("path")),
    )
    if not (subject.package or subject.path):
        raise InvalidResult(f"{where}: subject must name a package or a path")
    if subject.package and subject.path:
        raise InvalidResult(
            f"{where}: subject names a package or a path, not both. The file belongs in `location`"
        )
    return subject


def _location(raw: Any, *, where: str) -> Location | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or not raw.get("path"):
        raise InvalidResult(f"{where}: location must be an object with a path")
    line = raw.get("line")
    if line is not None and (isinstance(line, bool) or not isinstance(line, int) or line < 1):
        raise InvalidResult(f"{where}: location line must be a positive integer, got {line!r}")
    return Location(path=str(raw["path"]), line=int(line) if line is not None else None)


def _enum[T: (Outcome, FixOutcome, Reason, Klass, Severity)](
    kind: type[T], value: Any, *, path: Path, field_name: str
) -> T:
    if value is None:
        raise InvalidResult(f"{path}: {field_name} is required")
    try:
        return kind(value)
    except ValueError:
        known = ", ".join(str(item.value) for item in kind)
        raise InvalidResult(f"{path}: {field_name} {value!r} is not one of {known}") from None


def _list(value: Any, *, path: Path, field_name: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise InvalidResult(f"{path}: {field_name} must be a list")
    return list(value)


def _text(value: Any, *, where: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidResult(f"{where}: {field_name} must be a non-empty string")
    return value.strip()


def _optional(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
