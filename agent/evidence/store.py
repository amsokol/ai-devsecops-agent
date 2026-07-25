"""The run's evidence store.

One store per run, shared by its tasks so that the same question is not asked twice. It is also the
run's audit record — and it is never read back as an input by a later run: a journal that becomes a
source of truth keeps one run's mistake alive forever.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from agent.evidence.record import Evidence, Subject


class EvidenceStore:
    def __init__(self) -> None:
        self._records: list[Evidence] = []
        self._by_question: dict[tuple[str, str], Evidence] = {}

    def __len__(self) -> int:
        return len(self._records)

    def __iter__(self) -> Iterator[Evidence]:
        return iter(self._records)

    def add(self, record: Evidence) -> Evidence:
        """Record a fact. A verified answer replaces an earlier unverified one, never the reverse.

        Within a run the same question about the same subject must have one answer: two conflicting
        answers in one report are worse than none, because a reader cannot tell which was used.
        """
        key = (record.question, record.subject.key())
        existing = self._by_question.get(key)
        if existing is not None and existing.is_verified:
            return existing
        if existing is not None:
            self._records.remove(existing)
        self._records.append(record)
        self._by_question[key] = record
        return record

    def find(self, question: str, subject: Subject) -> Evidence | None:
        return self._by_question.get((question, subject.key()))

    def unverified(self) -> tuple[Evidence, ...]:
        return tuple(record for record in self._records if not record.is_verified)

    def failures(self) -> tuple[Evidence, ...]:
        """Unverified facts whose reason is a failure rather than a documented gap."""
        return tuple(
            record
            for record in self._records
            if not record.is_verified and record.reason is not None and record.reason.is_failure
        )

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(record.as_json(), ensure_ascii=False) for record in self._records]
        path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        return path
