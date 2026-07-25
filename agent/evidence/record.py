"""Evidence records: every fact a decision rests on, with where it came from.

`reliability` is derived from `origin` in code and is never chosen by a model, because it decides
what the resulting finding is allowed to do — comment or block.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Self

from agent.domain import Reason


class Origin(StrEnum):
    TOOL = "tool"
    API = "api"
    WEB = "web"
    MODEL = "model"


class Reliability(StrEnum):
    REPRODUCIBLE = "reproducible"
    HEURISTIC = "heuristic"

    @classmethod
    def of(cls, origin: Origin) -> Reliability:
        return cls.REPRODUCIBLE if origin in {Origin.TOOL, Origin.API} else cls.HEURISTIC


class Status(StrEnum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"


@dataclass(frozen=True, slots=True)
class Subject:
    """What a fact is about: a package in an ecosystem, or a path in the repository."""

    ecosystem: str | None = None
    package: str | None = None
    version: str | None = None
    path: str | None = None

    def key(self) -> str:
        parts = [self.ecosystem, self.package, self.version, self.path]
        return "|".join(part or "" for part in parts)

    def as_json(self) -> dict[str, str | None]:
        return {
            "ecosystem": self.ecosystem,
            "package": self.package,
            "version": self.version,
            "path": self.path,
        }


@dataclass(frozen=True, slots=True)
class Evidence:
    question: str
    subject: Subject
    origin: Origin
    source: str
    observed_at: datetime
    status: Status
    value: Any = None
    reason: Reason | None = None
    recipe: str = ""
    """Which recipe produced it, so a corrected recipe can invalidate what the old one cached."""

    def __post_init__(self) -> None:
        if self.status is Status.VERIFIED and self.reason is not None:
            raise ValueError("a verified fact cannot carry a reason")
        if self.status is Status.UNVERIFIED and self.reason is None:
            raise ValueError("an unverified fact must carry a reason")

    @property
    def reliability(self) -> Reliability:
        return Reliability.of(self.origin)

    @property
    def is_verified(self) -> bool:
        return self.status is Status.VERIFIED

    @classmethod
    def verified(
        cls,
        *,
        question: str,
        subject: Subject,
        value: Any,
        origin: Origin,
        source: str,
        observed_at: datetime | None = None,
        recipe: str = "",
    ) -> Self:
        return cls(
            question=question,
            subject=subject,
            origin=origin,
            source=source,
            observed_at=observed_at or datetime.now(UTC),
            status=Status.VERIFIED,
            value=value,
            recipe=recipe,
        )

    @classmethod
    def unverified(
        cls,
        *,
        question: str,
        subject: Subject,
        reason: Reason,
        origin: Origin,
        source: str,
        observed_at: datetime | None = None,
        recipe: str = "",
    ) -> Self:
        return cls(
            question=question,
            subject=subject,
            origin=origin,
            source=source,
            observed_at=observed_at or datetime.now(UTC),
            status=Status.UNVERIFIED,
            reason=reason,
            recipe=recipe,
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "subject": self.subject.as_json(),
            "value": self.value,
            "origin": self.origin.value,
            "source": self.source,
            "observed_at": self.observed_at.isoformat(),
            "reliability": self.reliability.value,
            "status": self.status.value,
            "reason": self.reason.value if self.reason else None,
            "recipe": self.recipe,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> Self:
        subject = raw.get("subject") or {}
        status = Status(raw["status"])
        return cls(
            question=str(raw["question"]),
            subject=Subject(
                ecosystem=subject.get("ecosystem"),
                package=subject.get("package"),
                version=subject.get("version"),
                path=subject.get("path"),
            ),
            origin=Origin(raw["origin"]),
            source=str(raw["source"]),
            observed_at=datetime.fromisoformat(str(raw["observed_at"])),
            status=status,
            value=raw.get("value"),
            reason=Reason(raw["reason"]) if raw.get("reason") else None,
            recipe=str(raw.get("recipe", "")),
        )
