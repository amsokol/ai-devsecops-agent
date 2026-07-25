"""Evidence records and the run store."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from agent.domain import Reason
from agent.evidence import Evidence, EvidenceStore, Origin, Reliability, Status, Subject

MOMENT = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
PACKAGE = Subject(ecosystem="ecosystems/python-uv", package="httpx", version="0.28.1")


def verified(value: object = "2026-06-01T00:00:00Z", origin: Origin = Origin.API) -> Evidence:
    return Evidence.verified(
        question="publish-time",
        subject=PACKAGE,
        value=value,
        origin=origin,
        source="https://pypi.org/pypi/httpx/json",
        observed_at=MOMENT,
        recipe="pypi-json@1",
    )


def unverified(reason: Reason = Reason.UNAVAILABLE) -> Evidence:
    return Evidence.unverified(
        question="publish-time",
        subject=PACKAGE,
        reason=reason,
        origin=Origin.API,
        source="https://pypi.org/pypi/httpx/json",
        observed_at=MOMENT,
    )


def test_reliability_follows_origin() -> None:
    assert verified(origin=Origin.TOOL).reliability is Reliability.REPRODUCIBLE
    assert verified(origin=Origin.API).reliability is Reliability.REPRODUCIBLE
    assert verified(origin=Origin.WEB).reliability is Reliability.HEURISTIC
    assert verified(origin=Origin.MODEL).reliability is Reliability.HEURISTIC


def test_a_verified_fact_cannot_carry_a_reason() -> None:
    with pytest.raises(ValueError, match="cannot carry a reason"):
        Evidence(
            question="publish-time",
            subject=PACKAGE,
            origin=Origin.API,
            source="x",
            observed_at=MOMENT,
            status=Status.VERIFIED,
            value=1,
            reason=Reason.UNAVAILABLE,
        )


def test_an_unverified_fact_must_say_why() -> None:
    with pytest.raises(ValueError, match="must carry a reason"):
        Evidence(
            question="publish-time",
            subject=PACKAGE,
            origin=Origin.API,
            source="x",
            observed_at=MOMENT,
            status=Status.UNVERIFIED,
        )


def test_one_question_has_one_answer_and_verified_wins() -> None:
    store = EvidenceStore()
    store.add(unverified())
    store.add(verified())
    store.add(unverified())

    assert len(store) == 1
    found = store.find("publish-time", PACKAGE)
    assert found is not None and found.is_verified


def test_failures_exclude_documented_gaps() -> None:
    store = EvidenceStore()
    store.add(unverified(Reason.NO_TOOLING))
    store.add(
        Evidence.unverified(
            question="advisories",
            subject=PACKAGE,
            reason=Reason.UNAVAILABLE,
            origin=Origin.API,
            source="osv",
            observed_at=MOMENT,
        )
    )

    assert len(store.unverified()) == 2
    assert [record.question for record in store.failures()] == ["advisories"]


def test_round_trip_through_json(tmp_path: Path) -> None:
    store = EvidenceStore()
    store.add(verified())
    path = store.write(tmp_path / "evidence.jsonl")

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    restored = Evidence.from_json(json.loads(lines[0]))
    assert restored == verified()
