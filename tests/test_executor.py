"""Running tasks through a backend: what happens when a session or a result goes wrong.

These are the paths that decide whether the gate is fail-closed, which is why they are asserted
against a scripted backend rather than a model.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from agent.backends import Brief, Budget, Failure, FakeBackend, Scripted, SessionResult
from agent.domain import Outcome, Plan, PlannedTask, Reason, Role, Trigger
from agent.evidence import Evidence, EvidenceStore, Origin, Subject
from agent.executor import Executed, execute
from agent.library import Library
from agent.session import Session
from agent.storage import FactCache
from agent.toolkit import Toolkits
from agent.tools import Grants

BUDGET = Budget(seconds=60)
MOMENT = datetime(2026, 7, 25, tzinfo=UTC)
SUBJECT = Subject(ecosystem="ecosystems/python-uv", package="httpx", version="0.28.1")


def store_with_a_fact() -> EvidenceStore:
    store = EvidenceStore()
    store.add(
        Evidence.verified(
            question="advisories",
            subject=SUBJECT,
            value=["GHSA-xxxx"],
            origin=Origin.TOOL,
            source="pip-audit",
            observed_at=MOMENT,
        )
    )
    return store


def plan_of(task_id: str = "deps-vuln") -> Plan:
    return Plan(
        playbook="playbooks/pr-review",
        trigger=Trigger.CHANGE_OPENED,
        tasks=(
            PlannedTask(
                id=task_id,
                capability=f"capabilities/{task_id}",
                role=Role.ANALYST,
                required=True,
                ecosystem="ecosystems/python-uv",
                knowledge=(f"capabilities/{task_id}", "policy/verdicts"),
            ),
        ),
    )


def run(
    backend: FakeBackend, library: Library, tmp_path: Path, *, evidence: EvidenceStore | None = None
) -> list[Executed]:
    # `or` would be wrong here: an empty store is falsy, which is exactly the case one test needs.
    store = store_with_a_fact() if evidence is None else evidence
    session = Session(
        repository=tmp_path,
        grants=Grants(binaries=frozenset(), hosts=frozenset()),
        cache=FactCache(None, writable=False),
        scratch_root=tmp_path / "scratch",
    )
    session.evidence = store
    return asyncio.run(
        execute(
            plan_of(),
            backend=backend,
            library=library,
            notes="Only the API service is in scope.",
            evidence=store,
            tasks_dir=tmp_path / "tasks",
            budget=BUDGET,
            toolkits=Toolkits(session=session, now=MOMENT, quarantine_days=7),
        )
    )


def finding() -> dict[str, object]:
    return {
        "class": "security",
        "severity": "high",
        "subject": {"ecosystem": "ecosystems/python-uv", "package": "httpx", "version": "0.28.1"},
        "summary": "httpx 0.28.1 is affected by GHSA-xxxx.",
        "rationale": "The advisory covers the pinned version.",
        "evidence": ["advisories|ecosystems/python-uv|httpx|0.28.1|"],
        "advisory": "GHSA-xxxx",
    }


def test_the_prompt_carries_the_knowledge_slice_and_the_notes(
    library: Library, tmp_path: Path
) -> None:
    backend = FakeBackend()

    run(backend, library, tmp_path)

    prompt = backend.briefs[0].prompt
    assert "capabilities/deps-vuln" in prompt
    assert "policy/verdicts" in prompt
    assert "Only the API service is in scope." in prompt
    assert str(backend.briefs[0].result_path) in prompt


def test_a_valid_result_becomes_the_tasks_outcome(library: Library, tmp_path: Path) -> None:
    backend = FakeBackend(default=Scripted(result={"outcome": "findings", "findings": [finding()]}))

    executed = run(backend, library, tmp_path)

    assert executed[0].outcome.outcome is Outcome.FINDINGS
    assert len(executed[0].findings) == 1


def test_an_invalid_result_is_retried_once_and_then_recorded_as_not_run(
    library: Library, tmp_path: Path
) -> None:
    backend = FakeBackend(default=Scripted(result={"outcome": "clean", "confidence": 0.9}))

    executed = run(backend, library, tmp_path)
    outcome = executed[0].outcome

    assert len(backend.briefs) == 2
    assert outcome.outcome is Outcome.UNVERIFIED
    assert outcome.reason is Reason.INVALID_RESULT
    assert outcome.failed
    assert "unknown field" in executed[0].attempts[0].rejected


def test_the_retry_is_told_what_was_wrong(library: Library, tmp_path: Path) -> None:
    backend = FakeBackend(default=Scripted(result={"outcome": "clean", "confidence": 0.9}))

    run(backend, library, tmp_path)

    assert "This is a retry" in backend.briefs[1].prompt
    assert "unknown field" in backend.briefs[1].prompt


def test_a_missing_file_is_never_read_as_clean(library: Library, tmp_path: Path) -> None:
    backend = FakeBackend(default=Scripted(result=None))

    executed = run(backend, library, tmp_path)

    assert executed[0].outcome.reason is Reason.INVALID_RESULT


def test_a_second_attempt_that_works_is_accepted(library: Library, tmp_path: Path) -> None:
    answers = [
        Scripted(result={"outcome": "clean", "confidence": 0.9}),
        Scripted(result={"outcome": "clean"}),
    ]

    class Sequenced(FakeBackend):
        async def execute(self, brief: Brief) -> SessionResult:
            self.default = answers[brief.attempt - 1]
            return await super().execute(brief)

    executed = run(Sequenced(), library, tmp_path)

    assert executed[0].outcome.outcome is Outcome.CLEAN


def test_a_session_that_never_started_is_retried(library: Library, tmp_path: Path) -> None:
    backend = FakeBackend(
        default=Scripted(result=None, failure=Failure.NOT_STARTED, detail="no api key")
    )

    executed = run(backend, library, tmp_path)

    assert len(backend.briefs) == 2
    assert executed[0].outcome.reason is Reason.UNAVAILABLE


def test_a_session_that_failed_mid_flight_is_not_retried(library: Library, tmp_path: Path) -> None:
    """It already spent budget and left a transcript; a second attempt is more likely to cost than
    to help."""
    backend = FakeBackend(default=Scripted(result=None, failure=Failure.FAILED, detail="crashed"))

    executed = run(backend, library, tmp_path)

    assert len(backend.briefs) == 1
    assert executed[0].outcome.outcome is Outcome.UNVERIFIED


def test_a_timed_out_session_is_exhausted_not_clean(library: Library, tmp_path: Path) -> None:
    backend = FakeBackend(default=Scripted(result=None, failure=Failure.TIMED_OUT))

    executed = run(backend, library, tmp_path)
    outcome = executed[0].outcome

    assert outcome.outcome is Outcome.EXHAUSTED
    assert outcome.reason is Reason.EXHAUSTED
    assert outcome.failed


def test_a_finding_citing_evidence_the_run_never_collected_is_rejected(
    library: Library, tmp_path: Path
) -> None:
    backend = FakeBackend(default=Scripted(result={"outcome": "findings", "findings": [finding()]}))

    executed = run(backend, library, tmp_path, evidence=EvidenceStore())

    assert executed[0].outcome.reason is Reason.INVALID_RESULT
