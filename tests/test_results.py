"""Reading a subagent's result file: what is accepted, and what is refused."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from agent.domain import Outcome, Reason
from agent.findings import Klass, Severity
from agent.results import InvalidResult, read_result

CAPABILITY = "capabilities/deps-vuln"
KEY = "advisories|ecosystems/python-uv|httpx|0.28.1|"
KNOWN = frozenset({KEY})


def finding(**overrides: Any) -> dict[str, Any]:
    base = {
        "class": "security",
        "severity": "high",
        "subject": {"ecosystem": "ecosystems/python-uv", "package": "httpx", "version": "0.28.1"},
        "summary": "httpx 0.28.1 is affected by GHSA-xxxx.",
        "rationale": "The advisory covers the version pinned in uv.lock.",
        "evidence": [KEY],
        "advisory": "GHSA-xxxx",
    }
    return base | overrides


def write(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = payload if isinstance(payload, str) else json.dumps(payload)
    path.write_text(text, encoding="utf-8")
    return path


def read(path: Path) -> Any:
    return read_result(path, capability=CAPABILITY, known_evidence=KNOWN)


def test_a_well_formed_result_is_read(tmp_path: Path) -> None:
    path = write(tmp_path / "r.json", {"outcome": "findings", "findings": [finding()]})

    result = read(path)

    assert result.outcome is Outcome.FINDINGS
    assert len(result.findings) == 1
    only = result.findings[0]
    assert only.klass is Klass.SECURITY
    assert only.severity is Severity.HIGH
    assert only.capability == CAPABILITY
    assert only.key == "capabilities/deps-vuln:ecosystems/python-uv:httpx:GHSA-xxxx"


def test_a_missing_file_is_a_failure_not_an_empty_result(tmp_path: Path) -> None:
    with pytest.raises(InvalidResult, match="was not written"):
        read(tmp_path / "absent.json")


def test_broken_json_is_refused(tmp_path: Path) -> None:
    with pytest.raises(InvalidResult):
        read(write(tmp_path / "r.json", "{not json"))


def test_an_unknown_field_is_refused(tmp_path: Path) -> None:
    path = write(tmp_path / "r.json", {"outcome": "clean", "confidence": 0.9})

    with pytest.raises(InvalidResult, match="unknown field"):
        read(path)


def test_clean_cannot_carry_findings(tmp_path: Path) -> None:
    path = write(tmp_path / "r.json", {"outcome": "clean", "findings": [finding()]})

    with pytest.raises(InvalidResult, match="cannot carry findings"):
        read(path)


def test_findings_requires_a_finding(tmp_path: Path) -> None:
    path = write(tmp_path / "r.json", {"outcome": "findings", "findings": []})

    with pytest.raises(InvalidResult, match="requires at least one"):
        read(path)


def test_unverified_must_say_why(tmp_path: Path) -> None:
    path = write(tmp_path / "r.json", {"outcome": "unverified"})

    with pytest.raises(InvalidResult, match="requires a reason"):
        read(path)


def test_a_task_may_not_claim_the_agents_own_reasons(tmp_path: Path) -> None:
    path = write(tmp_path / "r.json", {"outcome": "unverified", "reason": "invalid-result"})

    with pytest.raises(InvalidResult, match="not one a task may state"):
        read(path)


def test_a_documented_gap_is_read_as_such(tmp_path: Path) -> None:
    path = write(tmp_path / "r.json", {"outcome": "unverified", "reason": "no-tooling"})

    assert read(path).reason is Reason.NO_TOOLING


def test_partial_work_may_keep_its_findings(tmp_path: Path) -> None:
    """A host that died halfway does not throw away what was already established."""
    path = write(
        tmp_path / "r.json",
        {"outcome": "unverified", "reason": "unavailable", "findings": [finding()]},
    )

    result = read(path)

    assert result.outcome is Outcome.UNVERIFIED
    assert len(result.findings) == 1


def test_a_fabricated_evidence_reference_invalidates_the_result(tmp_path: Path) -> None:
    path = write(
        tmp_path / "r.json",
        {"outcome": "findings", "findings": [finding(evidence=["advisories|invented"])]},
    )

    with pytest.raises(InvalidResult, match="never recorded by this run"):
        read(path)


def test_a_finding_must_name_a_package_or_a_path(tmp_path: Path) -> None:
    path = write(
        tmp_path / "r.json",
        {"outcome": "findings", "findings": [finding(subject={"ecosystem": "ecosystems/npm"})]},
    )

    with pytest.raises(InvalidResult, match="must name a package or a path"):
        read(path)


def test_an_empty_summary_is_refused(tmp_path: Path) -> None:
    path = write(tmp_path / "r.json", {"outcome": "findings", "findings": [finding(summary="  ")]})

    with pytest.raises(InvalidResult, match="summary must be a non-empty string"):
        read(path)


def test_a_nonsense_line_number_is_refused(tmp_path: Path) -> None:
    path = write(
        tmp_path / "r.json",
        {"outcome": "findings", "findings": [finding(location={"path": "a.py", "line": 0})]},
    )

    with pytest.raises(InvalidResult, match="positive integer"):
        read(path)
