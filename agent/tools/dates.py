"""`date_math`: quarantine arithmetic, answered exactly.

Every function takes `now` explicitly. A hidden clock makes a run unreproducible and makes a test
depend on the day it is executed, which is how "it passed yesterday" becomes unanswerable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass(frozen=True, slots=True)
class Quarantine:
    published_at: datetime
    clears_at: datetime
    age_days: float
    cleared: bool

    def phrase(self) -> str:
        """The wording used in reports, so that every report says it the same way."""
        published = self.published_at.strftime("%Y-%m-%dT%H:%MZ")
        if self.cleared:
            return f"published {published}, cleared"
        return f"published {published}, clears ~{self.clears_at.strftime('%Y-%m-%dT%H:%MZ')}"


def age_days(published_at: datetime, now: datetime) -> float:
    return (_utc(now) - _utc(published_at)).total_seconds() / 86400


def quarantine(
    published_at: datetime,
    *,
    days: int,
    now: datetime,
    margin_days: float = 0.0,
) -> Quarantine:
    """Whether a version has been published long enough to be adopted.

    `margin_days` exists for heuristic timestamps: the library allows such a date to keep a
    candidate waiting freely, but to clear it only when the version is unambiguously older than the
    window. The margin is how "unambiguously" is expressed, and the caller supplies it because the
    asymmetry is policy, not arithmetic.
    """
    published_at, now = _utc(published_at), _utc(now)
    clears_at = published_at + timedelta(days=days)
    age = age_days(published_at, now)
    return Quarantine(
        published_at=published_at,
        clears_at=clears_at,
        age_days=age,
        cleared=age >= days + margin_days,
    )


def _utc(moment: datetime) -> datetime:
    return moment.astimezone(UTC) if moment.tzinfo else moment.replace(tzinfo=UTC)
