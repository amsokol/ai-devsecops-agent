"""The cross-run cache: what it stores, and what it refuses to store."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from agent.domain import Reason
from agent.evidence import Evidence, Origin, Subject
from agent.session import Session
from agent.storage import FactCache
from agent.tools import Grants

MOMENT = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
PACKAGE = Subject(ecosystem="ecosystems/python-uv", package="httpx", version="0.28.1")


def publish_time(recipe: str = "pypi-json@1") -> Evidence:
    return Evidence.verified(
        question="publish-time",
        subject=PACKAGE,
        value="2026-06-01T00:00:00Z",
        origin=Origin.API,
        source="https://pypi.org/pypi/httpx/json",
        observed_at=MOMENT,
        recipe=recipe,
    )


def test_stores_and_returns_an_immutable_fact(tmp_path: Path) -> None:
    cache = FactCache(tmp_path, writable=True)

    assert cache.put(publish_time())
    assert cache.get("publish-time", PACKAGE) == publish_time()
    assert cache.stats.stored == 1
    assert cache.stats.hits == 1


def test_a_changing_answer_is_never_cached(tmp_path: Path) -> None:
    cache = FactCache(tmp_path, writable=True)
    advisories = Evidence.verified(
        question="advisories",
        subject=PACKAGE,
        value=[],
        origin=Origin.TOOL,
        source="pip-audit",
        observed_at=MOMENT,
    )

    assert not cache.put(advisories)
    assert cache.get("advisories", PACKAGE) is None
    assert cache.stats.refused == 1


def test_failures_are_not_facts(tmp_path: Path) -> None:
    cache = FactCache(tmp_path, writable=True)
    record = Evidence.unverified(
        question="publish-time",
        subject=PACKAGE,
        reason=Reason.UNAVAILABLE,
        origin=Origin.API,
        source="https://pypi.org/pypi/httpx/json",
        observed_at=MOMENT,
    )

    assert not cache.put(record)
    assert cache.get("publish-time", PACKAGE) is None


def test_a_review_run_may_read_but_not_write(tmp_path: Path) -> None:
    FactCache(tmp_path, writable=True).put(publish_time())
    review = FactCache(tmp_path, writable=False)
    other = Subject(ecosystem="ecosystems/npm", package="left-pad", version="1.3.0")

    assert review.get("publish-time", PACKAGE) is not None
    assert not review.put(
        Evidence.verified(
            question="publish-time",
            subject=other,
            value="2026-07-24T00:00:00Z",
            origin=Origin.API,
            source="registry.npmjs.org",
            observed_at=MOMENT,
        )
    )
    assert review.get("publish-time", other) is None


def test_a_fact_without_an_exact_version_is_not_immutable(tmp_path: Path) -> None:
    cache = FactCache(tmp_path, writable=True)
    floating = Evidence.verified(
        question="publish-time",
        subject=Subject(ecosystem="ecosystems/npm", package="left-pad"),
        value="2026-07-24T00:00:00Z",
        origin=Origin.API,
        source="registry.npmjs.org",
        observed_at=MOMENT,
    )

    assert not cache.put(floating)


def test_a_changed_recipe_invalidates_what_the_old_one_produced(tmp_path: Path) -> None:
    cache = FactCache(tmp_path, writable=True)
    cache.put(publish_time("pypi-json@1"))

    assert cache.get("publish-time", PACKAGE, recipe="pypi-json@2") is None
    assert cache.get("publish-time", PACKAGE, recipe="pypi-json@1") is not None


def test_a_corrupt_entry_is_a_miss(tmp_path: Path) -> None:
    cache = FactCache(tmp_path, writable=True)
    cache.put(publish_time())
    entry = next(tmp_path.rglob("*.json"))
    entry.write_text("{not json", encoding="utf-8")

    assert cache.get("publish-time", PACKAGE) is None


def test_disabled_cache_is_transparent() -> None:
    cache = FactCache(None, writable=True)

    assert not cache.enabled
    assert not cache.put(publish_time())
    assert cache.get("publish-time", PACKAGE) is None


def test_session_asks_once_per_run(tmp_path: Path) -> None:
    session = Session(
        repository=tmp_path,
        grants=Grants(binaries=frozenset(), hosts=frozenset()),
        cache=FactCache(tmp_path / "cache", writable=True),
        scratch_root=tmp_path / "scratch",
    )
    calls = 0

    def acquire() -> Evidence:
        nonlocal calls
        calls += 1
        return publish_time()

    for _ in range(3):
        session.fact(
            question="publish-time", subject=PACKAGE, recipe="pypi-json@1", acquire=acquire
        )

    assert calls == 1
    assert len(session.evidence) == 1


def test_session_retries_after_a_failure_but_records_it(tmp_path: Path) -> None:
    session = Session(
        repository=tmp_path,
        grants=Grants(binaries=frozenset(), hosts=frozenset()),
        cache=FactCache(tmp_path / "cache", writable=True),
        scratch_root=tmp_path / "scratch",
    )
    answers = [
        Evidence.unverified(
            question="publish-time",
            subject=PACKAGE,
            reason=Reason.UNAVAILABLE,
            origin=Origin.API,
            source="pypi",
            observed_at=MOMENT,
        ),
        publish_time(),
    ]

    def acquire() -> Evidence:
        return answers.pop(0)

    first = session.fact(
        question="publish-time", subject=PACKAGE, recipe="pypi-json@1", acquire=acquire
    )
    second = session.fact(
        question="publish-time", subject=PACKAGE, recipe="pypi-json@1", acquire=acquire
    )

    assert not first.is_verified
    assert second.is_verified
    assert len(session.evidence) == 1
