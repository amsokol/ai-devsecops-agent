"""The cross-run cache of facts that cannot change.

Backed by a directory, which is also how a CI platform cache works: the platform restores a path
before the run and saves it after, so one implementation serves both a laptop and a pipeline.

Three rules are enforced here rather than left to callers, because each of them has been a real
source of wrong verdicts elsewhere:

* only questions whose answers are immutable are cached. Advisory data and version lists are not:
  caching them would make a weekly run stop noticing new advisories, which is the entire reason the
  weekly run exists;
* failures are never cached. An unreachable host is not a fact about a package;
* only runs on the default branch write. Otherwise anyone able to open a change request could seed a
  publication date and walk a fresh package past quarantine.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.evidence.questions import CACHEABLE
from agent.evidence.record import Evidence, Subject


@dataclass(slots=True)
class CacheStats:
    hits: int = 0
    misses: int = 0
    stored: int = 0
    refused: int = 0

    def as_json(self) -> dict[str, int]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "stored": self.stored,
            "refused": self.refused,
        }


class FactCache:
    """A key-value store of immutable facts. A miss is normal and never an error."""

    def __init__(self, root: Path | None, *, writable: bool) -> None:
        self.root = root
        self.writable = writable
        self.stats = CacheStats()

    @property
    def enabled(self) -> bool:
        return self.root is not None

    def get(self, question: str, subject: Subject, *, recipe: str = "") -> Evidence | None:
        if self.root is None or question not in CACHEABLE:
            return None
        path = _path(self.root, question, subject)
        if not path.is_file():
            self.stats.misses += 1
            return None
        try:
            raw: Any = json.loads(path.read_text(encoding="utf-8"))
            record = Evidence.from_json(raw["evidence"])
        except OSError, ValueError, KeyError:
            # A corrupt entry is a miss, not a failure: the fact can always be acquired again.
            self.stats.misses += 1
            return None
        if recipe and record.recipe and record.recipe != recipe:
            # The recipe that produced this changed, so what it produced is no longer trusted.
            self.stats.misses += 1
            return None
        self.stats.hits += 1
        return record

    def put(self, record: Evidence) -> bool:
        if self.root is None or not self.writable:
            return False
        if record.question not in CACHEABLE or not record.is_verified:
            self.stats.refused += 1
            return False
        if not _identifies_an_immutable_thing(record.subject):
            self.stats.refused += 1
            return False
        path = _path(self.root, record.question, record.subject)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"key": _key(record.question, record.subject), "evidence": record.as_json()}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.stats.stored += 1
        return True


def _path(root: Path, question: str, subject: Subject) -> Path:
    """Entry location, keyed by a digest so that a package name cannot shape a path."""
    digest = hashlib.sha256(_key(question, subject).encode("utf-8")).hexdigest()
    return root / question / digest[:2] / f"{digest}.json"


def _key(question: str, subject: Subject) -> str:
    return f"{question}\0{subject.key()}"


def _identifies_an_immutable_thing(subject: Subject) -> bool:
    """A cached fact must be about one exact version, or it is not immutable after all."""
    return bool(subject.ecosystem and subject.package and subject.version)
