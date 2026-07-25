"""The tools a subagent calls, and the guarantees they enforce rather than request."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from agent.domain import PlannedTask, Role
from agent.evidence import Origin, Reliability
from agent.session import Session
from agent.storage import FactCache
from agent.toolkit import Refused, Toolkit
from agent.tools import Grants

MOMENT = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
TASK = PlannedTask(
    id="deps-vuln@python-uv",
    capability="capabilities/deps-vuln",
    role=Role.ANALYST,
    required=True,
    ecosystem="ecosystems/python-uv",
)
SUBJECT = {"ecosystem": "ecosystems/python-uv", "package": "httpx", "version": "0.28.1"}


def session_of(
    tmp_path: Path,
    *,
    binaries: frozenset[str] = frozenset({"git"}),
    hosts: frozenset[str] = frozenset({"pypi.org"}),
    never_send: tuple[str, ...] = ("**/*.pem",),
    cache: Path | None = None,
) -> Session:
    return Session(
        repository=tmp_path,
        grants=Grants(binaries=binaries, hosts=hosts),
        cache=FactCache(cache, writable=cache is not None),
        scratch_root=tmp_path / "scratch",
        never_send=never_send,
    )


def toolkit(tmp_path: Path, **kwargs: Any) -> Toolkit:
    return Toolkit(session=session_of(tmp_path, **kwargs), task=TASK, now=MOMENT, quarantine_days=7)


def a_call(kit: Toolkit, tmp_path: Path) -> str:
    (tmp_path / "uv.lock").write_text('version = 1\nname = "httpx"\n', encoding="utf-8")
    answer: dict[str, Any] = kit.call("read_file", {"path": "uv.lock"})
    return str(answer["call"])


def test_every_tool_declares_a_schema(tmp_path: Path) -> None:
    for tool in toolkit(tmp_path).tools():
        assert tool.description
        assert tool.schema["type"] == "object"
        assert tool.schema["additionalProperties"] is False


def test_an_unknown_tool_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(Refused, match="no tool named"):
        toolkit(tmp_path).call("delete_everything", {})


def test_reading_a_file_yields_a_citable_call(tmp_path: Path) -> None:
    kit = toolkit(tmp_path)

    identifier = a_call(kit, tmp_path)

    assert identifier == "c1"
    assert kit.calls[0].origin is Origin.TOOL
    assert kit.calls[0].ok


def test_a_never_send_path_is_refused_and_the_refusal_is_recorded(tmp_path: Path) -> None:
    (tmp_path / "server.pem").write_text("-----BEGIN PRIVATE KEY-----\n", encoding="utf-8")
    kit = toolkit(tmp_path)

    with pytest.raises(Refused, match="never-send"):
        kit.call("read_file", {"path": "server.pem"})
    assert kit.calls[0].ok is False
    assert kit.calls[0].detail == "never-send"


def test_a_search_cannot_read_out_a_never_send_file(tmp_path: Path) -> None:
    (tmp_path / "server.pem").write_text("PRIVATE KEY material\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("PRIVATE KEY handling is documented\n", encoding="utf-8")

    answer = toolkit(tmp_path).call("search_text", {"pattern": "PRIVATE KEY"})

    assert [match["path"] for match in answer["matches"]] == ["README.md"]


def test_leaving_the_repository_is_refused(tmp_path: Path) -> None:
    (tmp_path.parent / "secret.txt").write_text("token\n", encoding="utf-8")

    with pytest.raises(Refused, match="outside the repository"):
        toolkit(tmp_path).call("read_file", {"path": "../secret.txt"})


def test_an_undeclared_binary_is_refused(tmp_path: Path) -> None:
    with pytest.raises(Refused, match="not permitted"):
        toolkit(tmp_path).call("run_command", {"command": ["curl", "https://pypi.org"]})


def test_a_command_is_not_a_shell_expression(tmp_path: Path) -> None:
    with pytest.raises(Refused, match="non-empty list of strings"):
        toolkit(tmp_path).call("run_command", {"command": "git --version"})


def test_a_host_outside_the_allowlist_is_refused(tmp_path: Path) -> None:
    with pytest.raises(Refused, match="not permitted"):
        toolkit(tmp_path).call("fetch", {"url": "https://evil.example/httpx"})


def test_a_fact_must_come_from_a_call(tmp_path: Path) -> None:
    kit = toolkit(tmp_path)

    with pytest.raises(Refused, match="at least one call"):
        kit.call(
            "record_fact",
            {"question": "advisories", "subject": SUBJECT, "value": [], "calls": []},
        )


def test_an_invented_call_identifier_is_refused(tmp_path: Path) -> None:
    kit = toolkit(tmp_path)
    a_call(kit, tmp_path)

    with pytest.raises(Refused, match="were made in this task"):
        kit.call(
            "record_fact",
            {"question": "advisories", "subject": SUBJECT, "value": [], "calls": ["c9"]},
        )


def test_a_recorded_fact_returns_a_key_a_finding_can_cite(tmp_path: Path) -> None:
    kit = toolkit(tmp_path)
    identifier = a_call(kit, tmp_path)

    answer = kit.call(
        "record_fact",
        {
            "question": "declared-pin",
            "subject": SUBJECT,
            "value": "0.28.1",
            "calls": [identifier],
        },
    )

    assert answer["key"] == "declared-pin|ecosystems/python-uv|httpx|0.28.1|"
    assert answer["reliability"] == Reliability.REPRODUCIBLE.value


def test_an_invented_question_is_refused(tmp_path: Path) -> None:
    kit = toolkit(tmp_path)
    identifier = a_call(kit, tmp_path)

    with pytest.raises(Refused, match="not a question this run asks"):
        kit.call(
            "record_fact",
            {
                "question": "pip-audit vulnerabilities in httpx 0.28.1",
                "subject": SUBJECT,
                "value": [],
                "calls": [identifier],
            },
        )


def test_the_ecosystem_of_a_subject_comes_from_the_task(tmp_path: Path) -> None:
    kit = toolkit(tmp_path)
    identifier = a_call(kit, tmp_path)

    answer = kit.call(
        "record_fact",
        {
            "question": "advisories",
            "subject": {"ecosystem": "python-uv", "package": "httpx"},
            "value": [],
            "calls": [identifier],
        },
    )

    assert answer["key"] == "advisories|ecosystems/python-uv|httpx||"


def test_a_fact_needs_a_value(tmp_path: Path) -> None:
    kit = toolkit(tmp_path)
    identifier = a_call(kit, tmp_path)

    with pytest.raises(Refused, match="needs a value"):
        kit.call(
            "record_fact",
            {"question": "advisories", "subject": SUBJECT, "value": None, "calls": [identifier]},
        )


def test_a_subject_must_name_a_package_or_a_path(tmp_path: Path) -> None:
    kit = toolkit(tmp_path)
    identifier = a_call(kit, tmp_path)

    with pytest.raises(Refused, match="package or a path"):
        kit.call(
            "record_fact",
            {
                "question": "advisories",
                "subject": {"ecosystem": "ecosystems/npm"},
                "value": [],
                "calls": [identifier],
            },
        )


def test_a_subject_is_a_package_or_a_path_but_not_both(tmp_path: Path) -> None:
    kit = toolkit(tmp_path)
    identifier = a_call(kit, tmp_path)

    with pytest.raises(Refused, match="not both"):
        kit.call(
            "record_fact",
            {
                "question": "advisories",
                "subject": {"package": "httpx", "version": "0.28.1", "path": "pyproject.toml"},
                "value": [],
                "calls": [identifier],
            },
        )


def test_a_gap_may_be_recorded_without_any_call(tmp_path: Path) -> None:
    session = session_of(tmp_path)
    kit = Toolkit(session=session, task=TASK, now=MOMENT, quarantine_days=7)

    answer = kit.call(
        "record_gap",
        {"question": "advisories", "subject": SUBJECT, "reason": "no-tooling"},
    )

    assert answer["recorded"] is True
    assert len(session.evidence.unverified()) == 1


def test_a_task_cannot_state_the_agents_own_reasons(tmp_path: Path) -> None:
    with pytest.raises(Refused, match="not one a task may state"):
        toolkit(tmp_path).call(
            "record_gap",
            {"question": "advisories", "subject": SUBJECT, "reason": "invalid-result"},
        )


def test_version_order_comes_from_the_tool(tmp_path: Path) -> None:
    answer = toolkit(tmp_path).call(
        "compare_versions",
        {"ecosystem": "ecosystems/python-uv", "left": "0.28.1", "right": "0.27.0"},
    )

    assert answer["order"] == 1
    assert answer["step"] == "minor"


def test_quarantine_uses_the_products_window_and_the_runs_clock(tmp_path: Path) -> None:
    kit = toolkit(tmp_path)

    waiting = kit.call("check_quarantine", {"published_at": "2026-07-22T00:00:00Z"})
    cleared = kit.call("check_quarantine", {"published_at": "2026-07-01T00:00:00Z"})

    assert waiting["cleared"] is False
    assert waiting["window_days"] == 7
    assert cleared["cleared"] is True


def test_a_heuristic_timestamp_needs_an_unambiguous_margin(tmp_path: Path) -> None:
    kit = toolkit(tmp_path)
    borderline = {"published_at": "2026-07-18T06:00:00Z"}

    assert kit.call("check_quarantine", borderline)["cleared"] is True
    assert kit.call("check_quarantine", borderline | {"heuristic": True})["cleared"] is False


def test_a_bad_timestamp_is_refused_rather_than_guessed(tmp_path: Path) -> None:
    with pytest.raises(Refused, match="ISO 8601"):
        toolkit(tmp_path).call("check_quarantine", {"published_at": "last tuesday"})


def test_a_known_fact_is_reported_instead_of_reacquired(tmp_path: Path) -> None:
    kit = toolkit(tmp_path)
    identifier = a_call(kit, tmp_path)
    kit.call(
        "record_fact",
        {
            "question": "declared-pin",
            "subject": SUBJECT,
            "value": "0.28.1",
            "calls": [identifier],
        },
    )

    answer = kit.call("known_fact", {"question": "declared-pin", "subject": SUBJECT})

    assert answer["found"] is True
    assert answer["value"] == "0.28.1"


def test_an_unknown_fact_says_so(tmp_path: Path) -> None:
    answer = toolkit(tmp_path).call("known_fact", {"question": "advisories", "subject": SUBJECT})

    assert answer["found"] is False


def test_an_immutable_fact_reaches_the_cache(tmp_path: Path) -> None:
    session = session_of(tmp_path, cache=tmp_path / "cache")
    kit = Toolkit(session=session, task=TASK, now=MOMENT, quarantine_days=7)
    identifier = a_call(kit, tmp_path)

    kit.call(
        "record_fact",
        {
            "question": "publish-time",
            "subject": SUBJECT,
            "value": "2026-06-01T00:00:00Z",
            "calls": [identifier],
        },
    )

    assert session.cache.stats.stored == 1


def test_the_ledger_records_every_call_for_the_manifest(tmp_path: Path) -> None:
    kit = toolkit(tmp_path)
    a_call(kit, tmp_path)
    kit.call("compare_versions", {"ecosystem": "x", "left": "1.0.0", "right": "1.0.1"})

    ledger = kit.as_json()

    assert [entry["tool"] for entry in ledger] == ["read_file", "compare_versions"]
    assert all(entry["at"] == MOMENT.isoformat() for entry in ledger)
