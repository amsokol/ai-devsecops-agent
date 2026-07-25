"""Findings: the vocabulary, the stable key, and what a finding is allowed to do.

The criteria for calling something critical are knowledge and live in the library. The vocabulary
and the arithmetic are here, because two runs on one input must produce the same key, the same
deduplication and the same ceiling.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from agent.evidence import Reliability, Subject


class Klass(StrEnum):
    SECURITY = "security"
    ROUTINE = "routine"

    @property
    def rank(self) -> int:
        return 1 if self is Klass.SECURITY else 0


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    @property
    def rank(self) -> int:
        return {
            Severity.LOW: 0,
            Severity.MEDIUM: 1,
            Severity.HIGH: 2,
            Severity.CRITICAL: 3,
        }[self]


class Action(StrEnum):
    """What the run does about a finding. Never stronger than the evidence permits."""

    BLOCK = "block"
    COMMENT = "comment"


_SLUG = re.compile(r"[^a-z0-9]+")


def slug(text: str) -> str:
    return _SLUG.sub("-", text.strip().lower()).strip("-")


@dataclass(frozen=True, slots=True)
class Location:
    """Where to attach a comment today. Volatile, and deliberately absent from the key."""

    path: str
    line: int | None = None

    def as_json(self) -> dict[str, object]:
        return {"path": self.path, "line": self.line}


@dataclass(frozen=True, slots=True)
class Finding:
    capability: str
    klass: Klass
    severity: Severity
    subject: Subject
    summary: str
    rationale: str
    evidence: tuple[str, ...] = field(default_factory=tuple)
    """Keys of the evidence records behind the claim, in the run's evidence store."""
    remediation: str = ""
    location: Location | None = None
    advisory: str = ""
    symbol: str = ""
    forbidden_state: bool = False

    @property
    def key(self) -> str:
        """Stable across runs: what identifies the problem, never what drifts.

        A version, a line number or a scanner's wording change between runs while the problem stays
        the same, and a key that moves turns one problem into a stream of duplicate comments.
        """
        parts = [self.capability]
        if self.subject.ecosystem:
            parts += [self.subject.ecosystem, self.subject.package or ""]
            parts.append(self.advisory or slug(self.summary))
        else:
            parts += [self.subject.path or "", slug(self.summary)]
            if self.symbol:
                parts.append(self.symbol)
        return ":".join(part for part in parts if part)

    def reliability(self, records: dict[str, Reliability]) -> Reliability:
        """The weakest reliability among the records behind it.

        A claim is only as demonstrated as its shakiest input, so one heuristic fact makes the whole
        finding heuristic. With no evidence at all it is heuristic too — an unsupported claim cannot
        earn the right to block.
        """
        if not self.evidence:
            return Reliability.HEURISTIC
        found = [records.get(key, Reliability.HEURISTIC) for key in self.evidence]
        return (
            Reliability.REPRODUCIBLE
            if all(item is Reliability.REPRODUCIBLE for item in found)
            else Reliability.HEURISTIC
        )

    def as_json(self) -> dict[str, object]:
        return {
            "key": self.key,
            "capability": self.capability,
            "class": self.klass.value,
            "severity": self.severity.value,
            "subject": self.subject.as_json(),
            "location": self.location.as_json() if self.location else None,
            "summary": self.summary,
            "rationale": self.rationale,
            "evidence": list(self.evidence),
            "remediation": self.remediation,
            "advisory": self.advisory,
            "symbol": self.symbol,
            "forbidden_state": self.forbidden_state,
        }


def merge(findings: tuple[Finding, ...]) -> tuple[Finding, ...]:
    """One problem found by two tasks is one finding, judged by the stricter of the two.

    Resolved by a rule rather than by asking a stronger model: escalation would put latency, cost
    and nondeterminism into a blocking check, and a gate that answers differently on a rerun stops
    being believed. Both original judgements stay in the manifest.
    """
    by_key: dict[str, Finding] = {}
    for finding in findings:
        existing = by_key.get(finding.key)
        if existing is None:
            by_key[finding.key] = finding
            continue
        by_key[finding.key] = _stricter(existing, finding)
    return tuple(
        sorted(
            by_key.values(),
            key=lambda item: (-item.klass.rank, -item.severity.rank, item.key),
        )
    )


def _stricter(first: Finding, second: Finding) -> Finding:
    klass = first.klass if first.klass.rank >= second.klass.rank else second.klass
    severity = first.severity if first.severity.rank >= second.severity.rank else second.severity
    winner = first if (first.klass, first.severity) == (klass, severity) else second
    evidence = tuple(dict.fromkeys(first.evidence + second.evidence))
    return Finding(
        capability=winner.capability,
        klass=klass,
        severity=severity,
        subject=winner.subject,
        summary=winner.summary,
        rationale=winner.rationale,
        evidence=evidence,
        remediation=winner.remediation or first.remediation or second.remediation,
        location=winner.location or first.location or second.location,
        advisory=winner.advisory or first.advisory or second.advisory,
        symbol=winner.symbol or first.symbol or second.symbol,
        forbidden_state=first.forbidden_state or second.forbidden_state,
    )
