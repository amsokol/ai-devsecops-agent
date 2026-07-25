"""The blocking table, read from the library rather than duplicated here.

Which class and severity may refuse a merge is a policy decision, and policy lives in the library.
The agent still has to apply it deterministically, so it parses the table out of `policy/verdicts`
at startup instead of carrying a second copy.

A second copy is the thing being avoided. If the agent held its own table and the library said
something else, the disagreement would ship silently and findings that ought to block would not —
the worst kind of failure, because nothing looks wrong. Parsing has a loud failure mode instead: the
table must be present, every cell must be recognised, and every class and severity must be
covered, or the run refuses to start.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Self

from agent.errors import ConfigError
from agent.findings import Klass, Severity
from agent.library import Library

VERDICTS = "policy/verdicts"
SECTION = "What blocks"
FORBIDDEN_ROW = "forbidden state"

_HEADING = re.compile(r"^##\s+(?P<title>.+?)\s*$")
_ROW = re.compile(r"^\|(?P<cells>.+)\|\s*$")
_SEPARATOR = re.compile(r"^\|[\s:|-]+\|$")


@dataclass(frozen=True, slots=True)
class BlockingRules:
    """Which (class, severity) pairs may refuse a merge, and whether a forbidden state may."""

    blocking: frozenset[tuple[Klass, Severity]]
    forbidden_state_blocks: bool
    source: str

    @classmethod
    def read(cls, library: Library) -> Self:
        document = library.get(VERDICTS)
        rows = _table(document.body())
        if not rows:
            raise ConfigError(
                f"{document.path}: no '{SECTION}' table found; the agent cannot decide what blocks "
                "without it, and will not guess"
            )
        blocking: set[tuple[Klass, Severity]] = set()
        covered: set[tuple[Klass, Severity]] = set()
        forbidden: bool | None = None
        for number, cells in rows:
            where = f"{document.path}: '{SECTION}' row {number}"
            if len(cells) != 3:
                raise ConfigError(f"{where}: expected three columns, got {len(cells)}")
            klass_cell, severity_cell, verdict_cell = cells
            blocks = _blocks(verdict_cell, where)
            if klass_cell.strip().lower() == FORBIDDEN_ROW:
                forbidden = blocks
                continue
            klass = _klass(klass_cell, where)
            for severity in _severities(severity_cell, where):
                pair = (klass, severity)
                if pair in covered:
                    raise ConfigError(f"{where}: {klass}/{severity} is listed twice")
                covered.add(pair)
                if blocks:
                    blocking.add(pair)
        missing = sorted(
            f"{klass}/{severity}"
            for klass in Klass
            for severity in Severity
            if (klass, severity) not in covered
        )
        if missing:
            raise ConfigError(
                f"{document.path}: '{SECTION}' does not cover {', '.join(missing)}. Every "
                "combination must be stated, because an omission would silently mean it does "
                "not block"
            )
        if forbidden is None:
            raise ConfigError(f"{document.path}: '{SECTION}' has no '{FORBIDDEN_ROW}' row")
        return cls(
            blocking=frozenset(blocking),
            forbidden_state_blocks=forbidden,
            source=f"{library.identity.version}:{VERDICTS}",
        )

    def blocks(self, klass: Klass, severity: Severity, *, forbidden_state: bool = False) -> bool:
        if forbidden_state and self.forbidden_state_blocks:
            return True
        return (klass, severity) in self.blocking


def _table(body: str) -> list[tuple[int, list[str]]]:
    rows: list[tuple[int, list[str]]] = []
    inside = False
    header_seen = False
    for number, line in enumerate(body.splitlines(), start=1):
        heading = _HEADING.match(line)
        if heading:
            if inside:
                break
            inside = heading.group("title").strip() == SECTION
            continue
        if not inside:
            continue
        if _SEPARATOR.match(line.strip()):
            header_seen = True
            continue
        match = _ROW.match(line.strip())
        if match is None:
            if rows:
                break
            continue
        cells = [cell.strip() for cell in match.group("cells").split("|")]
        if not header_seen:
            continue
        rows.append((number, cells))
    return rows


def _blocks(cell: str, where: str) -> bool:
    verdict = cell.strip().lower()
    if verdict.startswith("yes"):
        return True
    if verdict.startswith("no"):
        return False
    raise ConfigError(f"{where}: {cell!r} is neither yes nor no")


def _klass(cell: str, where: str) -> Klass:
    token = cell.strip().strip("`")
    try:
        return Klass(token)
    except ValueError:
        known = ", ".join(item.value for item in Klass)
        raise ConfigError(f"{where}: unknown class {token!r}; known: {known}") from None


def _severities(cell: str, where: str) -> tuple[Severity, ...]:
    if cell.strip().lower() == "any":
        return tuple(Severity)
    severities: list[Severity] = []
    for item in cell.split(","):
        token = item.strip().strip("`")
        if not token:
            continue
        try:
            severities.append(Severity(token))
        except ValueError:
            known = ", ".join(entry.value for entry in Severity)
            raise ConfigError(f"{where}: unknown severity {token!r}; known: {known}") from None
    if not severities:
        raise ConfigError(f"{where}: no severity listed")
    return tuple(severities)
