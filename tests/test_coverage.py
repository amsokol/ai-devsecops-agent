"""What a run examined, as opposed to what it had something to say about.

The distinction earned its own module the hard way. Two live maintenance runs over one unchanged
repository disagreed about how many of its six action pins existed: the first recorded facts for
four, the second for all six, and both reported `findings` and looked equally thorough. Under the
closure rule those two runs in a row would have closed a live issue as fixed.
"""

from __future__ import annotations

from datetime import UTC, datetime

from agent.coverage import Coverage, previous
from agent.evidence import Evidence, Origin, Subject

WHEN = datetime(2026, 7, 26, 4, 45, tzinfo=UTC)
CAPABILITY = "capabilities/deps-outdated"
ECOSYSTEM = "ecosystems/github-actions"


def fact(
    package: str, *, capability: str = CAPABILITY, question: str = "latest-version"
) -> Evidence:
    return Evidence.verified(
        question=question,
        subject=Subject(ecosystem=ECOSYSTEM, package=package),
        value="v5",
        origin=Origin.API,
        source="https://api.github.com/",
        observed_at=WHEN,
        recipe=f"{capability}@fetch",
    )


def gap(package: str) -> Evidence:
    from agent.domain import Reason

    return Evidence.unverified(
        question="latest-version",
        subject=Subject(ecosystem=ECOSYSTEM, package=package),
        reason=Reason.UNAVAILABLE,
        origin=Origin.API,
        source="https://api.github.com/",
        observed_at=WHEN,
        recipe=f"{CAPABILITY}@fetch",
    )


def key(package: str, *, kind: str = "outdated", capability: str = CAPABILITY) -> str:
    return f"{capability}:{ECOSYSTEM}:{package}:{kind}"


def test_the_examined_set_is_the_subjects_the_run_recorded_facts_about() -> None:
    covered = Coverage.of([fact("actions/checkout"), fact("actions/setup-go")])

    assert covered.as_json() == {
        f"{CAPABILITY}:{ECOSYSTEM}": ["actions/checkout", "actions/setup-go"]
    }


def test_a_package_the_run_never_asked_about_was_not_examined() -> None:
    """The live failure, in one assertion: four pins recorded, a fifth silently skipped."""
    covered = Coverage.of([fact("actions/checkout")])

    assert covered.looked_at(key("actions/checkout")) is True
    assert covered.looked_at(key("Swatinem/rust-cache")) is False


def test_a_fact_that_could_not_be_established_is_not_a_look() -> None:
    """A gap is an honest answer to its own question and no answer at all about the pin."""
    covered = Coverage.of([fact("actions/checkout"), gap("actions/setup-go")])

    assert covered.looked_at(key("actions/setup-go")) is False


def test_an_ecosystem_with_no_facts_at_all_is_not_read_as_a_short_sweep() -> None:
    """An ecosystem whose last pin was removed records nothing, and its issue ought to close.

    So the gate calibrates itself: it speaks only about buckets the check has put something in,
    which is what tells "recorded a fact per pin and missed one" from "records no per-pin facts".
    """
    covered = Coverage.of([fact("actions/checkout")])

    assert covered.looked_at(key("golang.org/x/net", capability="capabilities/deps-vuln")) is None


def test_coverage_says_nothing_about_findings_that_name_a_file() -> None:
    """Nobody records "this file is clean" per file, so requiring it would freeze those issues."""
    covered = Coverage.of([fact("actions/checkout")])

    assert covered.looked_at("capabilities/code-vuln:src/app.py:sql-injection:handler") is None


def test_coverage_says_nothing_about_an_escalation() -> None:
    covered = Coverage.of([fact("actions/checkout")])

    assert covered.looked_at(f"{CAPABILITY}:failure:unavailable") is None


def test_facts_are_grouped_by_the_check_that_recorded_them() -> None:
    """Two capabilities examining one package are two answers, and neither covers for the other."""
    covered = Coverage.of(
        [fact("actions/checkout"), fact("eclipse-temurin", capability="capabilities/deps-vuln")]
    )

    assert covered.looked_at(key("eclipse-temurin")) is False
    assert covered.looked_at(key("eclipse-temurin", capability="capabilities/deps-vuln")) is True


def test_a_run_that_examined_less_than_the_last_one_says_which_pins_it_missed() -> None:
    stored = Coverage.of(
        [fact("actions/checkout"), fact("actions/setup-go"), fact("Swatinem/rust-cache")]
    ).document({})

    said = Coverage.of([fact("actions/checkout")]).shortfall(previous(stored))

    assert len(said) == 1
    assert "Swatinem/rust-cache, actions/setup-go were not looked at" in said[0]
    assert "examined 1 package(s)" in said[0] and "against 3" in said[0]


def test_examining_more_than_the_last_run_is_not_a_shortfall() -> None:
    stored = Coverage.of([fact("actions/checkout")]).document({})

    assert (
        Coverage.of([fact("actions/checkout"), fact("actions/setup-go")]).shortfall(
            previous(stored)
        )
        == ()
    )


def test_an_ecosystem_this_run_did_not_enter_is_not_reported_as_a_shortfall() -> None:
    """`--only` narrows a run to one ecosystem, and that is a supported way to run the agent.

    Comparing a narrowed run against a full one would put a coverage complaint on every use of the
    flag, and a warning that fires on ordinary use is one people learn to scroll past.
    """
    stored = Coverage.of([fact("actions/checkout")]).document({})
    stored["coverage"][f"{CAPABILITY}:ecosystems/cargo"] = ["serde", "tokio"]

    assert Coverage.of([fact("actions/checkout")]).shortfall(previous(stored)) == ()


def test_what_a_narrowed_run_did_not_look_at_survives_in_the_memory_it_writes() -> None:
    """Otherwise one narrowed run erases the record a full run left, and nothing is ever short."""
    stored = Coverage.of([fact("actions/checkout")]).document({})
    stored["coverage"][f"{CAPABILITY}:ecosystems/cargo"] = ["serde"]

    after = Coverage.of([fact("actions/checkout"), fact("actions/setup-go")]).document(stored)

    assert after["coverage"][f"{CAPABILITY}:ecosystems/cargo"] == ["serde"]
    assert after["coverage"][f"{CAPABILITY}:{ECOSYSTEM}"] == [
        "actions/checkout",
        "actions/setup-go",
    ]


def test_a_shortfall_names_a_few_and_counts_the_rest() -> None:
    stored = Coverage.of([fact(f"owner/action-{index}") for index in range(9)]).document({})

    said = Coverage.of([fact("owner/action-0")]).shortfall(previous(stored))

    assert "and 2 more" in said[0]
