"""Publishing a review: one thread per finding, kept across runs, resolved only when earned."""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from agent.domain import Outcome, Reason, RunResult
from agent.evidence import Reliability, Subject
from agent.findings import Action, Finding, Klass, Location, Severity
from agent.publish import Publication, publish_review
from agent.repo import ChangeView, Repository
from agent.scm import GitHub, Identity, ScmError, Stance, credential
from agent.scm.fake import FakePlatform
from agent.scm.github import Credential
from agent.scm.marker import read, stamp
from agent.verdict import Judged, TaskOutcome, Verdict

HEAD = "0f1e2d3c4b5a69788796a5b4c3d2e1f009182736"
CAPABILITY = "capabilities/deps-vuln"
CLEAN = TaskOutcome(
    id="deps-vuln@python-uv", capability=CAPABILITY, required=True, outcome=Outcome.CLEAN
)


def finding(*, advisory: str = "PYSEC-2026-1", line: int | None = None) -> Finding:
    return Finding(
        capability=CAPABILITY,
        klass=Klass.SECURITY,
        severity=Severity.HIGH,
        subject=Subject(ecosystem="ecosystems/python-uv", package="jinja2", version="3.1.3"),
        summary=f"jinja2 3.1.3 is affected by {advisory}",
        rationale="pip-audit reports it against the resolved pin.",
        evidence=("advisories|ecosystems/python-uv|jinja2|3.1.3|",),
        remediation="Bump jinja2 to 3.1.6.",
        advisory=advisory,
        location=Location(path="pyproject.toml", line=line) if line else None,
    )


def judged(item: Finding, *, action: Action = Action.BLOCK, capped: bool = False) -> Judged:
    return Judged(finding=item, action=action, reliability=Reliability.REPRODUCIBLE, capped=capped)


def verdict_of(*items: Judged, result: RunResult = RunResult.BLOCKED) -> Verdict:
    blocking = tuple(item for item in items if item.action is Action.BLOCK)
    return Verdict(result=result, judged=items, blocking=blocking)


def publish(
    platform: FakePlatform,
    verdict: Verdict,
    *,
    outcomes: tuple[TaskOutcome, ...] = (CLEAN,),
    change: ChangeView | None = None,
    head: str = HEAD,
) -> Publication:
    return publish_review(
        platform,
        number=7,
        verdict=verdict,
        report="## No blocking findings\n",
        head=head,
        outcomes=outcomes,
        change=change,
        identity=platform.identity(),
    )


def what(record: Publication) -> list[str]:
    return [item.what for item in record.posted]


def _token() -> Credential:
    return Credential(token="not-a-real-token", variable="AGENT_GITHUB_TOKEN")  # noqa: S106


@pytest.fixture
def platform() -> FakePlatform:
    return FakePlatform(head=HEAD)


@pytest.fixture
def touched(git_repo: Path) -> ChangeView:
    """A change that adds lines to a manifest, so a comment has somewhere to attach."""

    def git(*arguments: str) -> None:
        subprocess.run(
            ["git", "-C", str(git_repo), *arguments],
            check=True,
            capture_output=True,
            env={
                "GIT_AUTHOR_NAME": "test",
                "GIT_AUTHOR_EMAIL": "test@example.com",
                "GIT_COMMITTER_NAME": "test",
                "GIT_COMMITTER_EMAIL": "test@example.com",
                "PATH": "/usr/bin:/bin",
                "HOME": str(git_repo.parent),
            },
        )

    git("checkout", "--quiet", "-b", "proposal")
    (git_repo / "pyproject.toml").write_text(
        '[project]\ndependencies = ["jinja2==3.1.3"]\n', encoding="utf-8"
    )
    git("add", "pyproject.toml")
    git("commit", "--quiet", "-m", "pin jinja2")
    return ChangeView.of(Repository.open(git_repo), "main")


def test_one_finding_becomes_one_thread_and_a_rerun_leaves_it_where_it_is(
    platform: FakePlatform, touched: ChangeView
) -> None:
    """The whole promise of publishing by key: a second run on one head says nothing twice."""
    verdict = verdict_of(judged(finding(line=2)))

    first = publish(platform, verdict, change=touched)
    second = publish(platform, verdict, change=touched)

    assert what(first) == ["thread"]
    assert what(second) == ["unchanged"]
    assert len(platform.opened) == 1
    assert [call.what for call in platform.calls].count("thread") == 1


def test_a_finding_that_changed_its_wording_updates_the_thread_it_already_has(
    platform: FakePlatform, touched: ChangeView
) -> None:
    """Same key, new detail: the current text arrives where the reader was already reading."""
    publish(platform, verdict_of(judged(finding(line=2))), change=touched)

    revised = replace(finding(line=2), remediation="Bump jinja2 to 3.1.7.")
    again = publish(platform, verdict_of(judged(revised)), change=touched)

    assert what(again) == ["updated"]
    assert len(platform.opened) == 1
    assert "3.1.7" in platform.opened[0].body


def test_a_thread_is_resolved_when_the_capability_that_owns_it_finished_clean(
    platform: FakePlatform, touched: ChangeView
) -> None:
    publish(platform, verdict_of(judged(finding(line=2))), change=touched)

    cleared = publish(platform, verdict_of(result=RunResult.PASS), change=touched)

    assert what(cleared) == ["resolved"]
    assert platform.opened[0].resolved
    assert "no longer present" in platform.replies[0][1]


def test_a_finding_that_comes_back_reopens_the_thread_it_was_resolved_on(
    platform: FakePlatform, touched: ChangeView
) -> None:
    """A problem that returned to a resolved thread is a problem nobody sees."""
    verdict = verdict_of(judged(finding(line=2)))
    publish(platform, verdict, change=touched)
    publish(platform, verdict_of(result=RunResult.PASS), change=touched)

    again = publish(platform, verdict, change=touched)

    assert what(again) == ["reopened"]
    assert not platform.opened[0].resolved
    assert "present again" in platform.replies[-1][1]
    assert len(platform.opened) == 1


@pytest.mark.parametrize(
    ("outcomes", "expected"),
    [
        ((), "did not run"),
        (
            (replace(CLEAN, outcome=Outcome.UNVERIFIED, reason=Reason.EXHAUSTED),),
            "finished unverified",
        ),
        ((replace(CLEAN, outcome=Outcome.EXHAUSTED),), "finished exhausted"),
    ],
)
def test_a_thread_stays_open_when_the_run_did_not_actually_look(
    platform: FakePlatform, touched: ChangeView, outcomes: tuple[TaskOutcome, ...], expected: str
) -> None:
    """Absence in a run that failed to check is not a fix. Resolving on it retracts a real problem
    in a way nobody notices."""
    publish(platform, verdict_of(judged(finding(line=2))), change=touched)

    cleared = publish(
        platform, verdict_of(result=RunResult.INCONCLUSIVE), change=touched, outcomes=outcomes
    )

    kept = [item for item in cleared.posted if item.what == "kept-open"]
    assert len(kept) == 1
    assert expected in kept[0].detail
    assert not platform.opened[0].resolved


def test_a_finding_off_the_lines_of_this_change_is_reported_in_the_body_instead(
    platform: FakePlatform, touched: ChangeView
) -> None:
    """The platform refuses a comment outside the diff, and if it did not, the comment would blame
    whoever wrote that line years ago."""
    off = publish(platform, verdict_of(judged(finding(line=900))), change=touched)

    assert what(off) == ["in-body"]
    assert not platform.opened
    assert platform.reviews


def test_a_finding_with_no_location_at_all_is_reported_in_the_body(
    platform: FakePlatform, touched: ChangeView
) -> None:
    nowhere = publish(platform, verdict_of(judged(finding())), change=touched)

    assert what(nowhere) == ["in-body"]


def test_a_change_that_moved_while_the_run_worked_is_not_commented_on(
    platform: FakePlatform, touched: ChangeView
) -> None:
    """Comments derived from one commit, posted on another, point at lines nobody proposed."""
    record = publish(platform, verdict_of(judged(finding(line=2))), change=touched, head="other")

    assert not record.published
    assert "moved while this run was working" in record.withheld
    assert not platform.reviews


def test_a_draft_change_is_left_alone(platform: FakePlatform, touched: ChangeView) -> None:
    platform.draft = True

    record = publish(platform, verdict_of(judged(finding(line=2))), change=touched)

    assert not record.published
    assert "draft" in record.withheld
    assert not platform.reviews


@pytest.mark.parametrize(
    ("result", "stance"),
    [
        (RunResult.PASS, Stance.APPROVE),
        (RunResult.BLOCKED, Stance.REQUEST_CHANGES),
        (RunResult.INCONCLUSIVE, Stance.COMMENT),
    ],
)
def test_the_review_carries_the_stance_the_result_earns(
    platform: FakePlatform, result: RunResult, stance: Stance
) -> None:
    """A run that could not complete comments rather than requesting changes: the check refuses the
    merge either way, and "changes requested" is a claim about the code."""
    record = publish(platform, verdict_of(result=result))

    assert record.stance is stance
    assert platform.reviews[0][0] is stance


@pytest.mark.parametrize("result", [RunResult.PASS, RunResult.BLOCKED])
def test_the_agent_s_own_change_gets_the_same_words_as_a_plain_comment(
    platform: FakePlatform, result: RunResult
) -> None:
    """Nobody reviews their own pull request on GitHub — approving or requesting changes alike — and
    a run on a change the agent opened is exactly that. The decision was never the review event: it
    is the text, and the check that carries the authority."""
    platform.refuse_own_review = True

    record = publish(platform, verdict_of(result=result))

    assert platform.reviews[0][0] is Stance.COMMENT
    assert platform.reviews[0][1].startswith("## No blocking findings")
    assert record.stance is Stance.COMMENT, (
        "the run records what the platform did, not what it asked"
    )
    if result is RunResult.BLOCKED:
        assert what(record) == ["commented-instead"]


def test_a_platform_that_refuses_to_publish_does_not_cost_the_run_its_verdict(
    platform: FakePlatform,
) -> None:
    """The analysis is the expensive part and it is already done. A failed comment is a warning."""
    identity = platform.identity()
    platform.fail = "the token cannot see this repository"

    record = publish_review(
        platform,
        number=7,
        verdict=verdict_of(judged(finding())),
        report="## No blocking findings\n",
        head=HEAD,
        outcomes=(CLEAN,),
        change=None,
        identity=identity,
    )

    assert not record.published
    assert "token cannot see" in record.failure


def test_a_comment_a_human_wrote_is_never_edited_or_resolved(
    platform: FakePlatform, touched: ChangeView
) -> None:
    """The marker is the only reason the agent claims a thread. Guessing by author or wording is how
    an agent ends up rewriting somebody's question."""
    publish(platform, verdict_of(judged(finding(line=2))), change=touched)
    platform.opened.append(
        replace(platform.opened[0], id="human", comment="human", key="", body="why?")
    )

    publish(platform, verdict_of(result=RunResult.PASS), change=touched)

    human = next(item for item in platform.opened if item.id == "human")
    assert human.body == "why?"
    assert not human.resolved


def test_a_thread_body_says_what_it_is_and_carries_its_key(
    platform: FakePlatform, touched: ChangeView
) -> None:
    publish(
        platform,
        verdict_of(judged(finding(line=2), action=Action.COMMENT, capped=True)),
        change=touched,
    )

    body = platform.opened[0].body
    assert "**high**" in body
    assert "Bump jinja2 to 3.1.6." in body
    assert "does not block the merge" in body
    assert read(body) == finding().key


def test_a_bot_identity_is_recorded_and_needs_no_warning(platform: FakePlatform) -> None:
    record = publish(platform, verdict_of(result=RunResult.PASS))

    assert record.identity is not None
    assert record.identity.login == "devsecops-agent[bot]"
    assert not record.caution


def test_publishing_under_a_human_account_is_said_out_loud(platform: FakePlatform) -> None:
    """Not refused: a dedicated machine account is a legitimate setup, and the platform shows it as
    an ordinary user. But a workflow that filters bot comments cannot filter it, so the agent's own
    comment would wake the agent, and that is worth a sentence in the run's own record."""
    platform.login = "amsokol"
    platform.is_bot = False

    record = publish(platform, verdict_of(result=RunResult.PASS))

    assert record.published
    assert "shows as a person" in record.caution
    assert "filter that login" in record.caution


def test_an_unreadable_identity_is_a_caution_rather_than_a_guess(platform: FakePlatform) -> None:
    platform.known_identity = False
    platform.login = ""
    platform.is_bot = False

    record = publish(platform, verdict_of(result=RunResult.PASS))

    assert record.published
    assert "could not be read" in record.caution


def test_an_app_learns_its_own_name_from_what_it_published(platform: FakePlatform) -> None:
    """An installation token proves the caller is an integration without saying which one, so the
    name comes back off the review rather than from asking the credential about itself."""
    platform.login = "ai-devsecops-agent[bot]"
    platform.nameless = True
    assert platform.identity().login == ""

    record = publish(platform, verdict_of(result=RunResult.PASS))

    assert record.identity is not None
    assert record.identity.login == "ai-devsecops-agent[bot]"
    assert not record.caution


def test_a_nameless_bot_is_described_rather_than_called_unreadable() -> None:
    assert Identity(login="", bot=True).description == "an app installation"
    assert Identity(login="", bot=False, known=False).description == "an unreadable account"


def test_the_agent_s_token_is_read_from_its_own_variable_before_any_other() -> None:
    """A laptop with a developer's `GH_TOKEN` in the environment still publishes as the agent."""
    found = credential(
        {"AGENT_GITHUB_TOKEN": "agent", "GH_TOKEN": "mine", "GITHUB_TOKEN": "workflow"}
    )

    assert (found.token, found.variable) == ("agent", "AGENT_GITHUB_TOKEN")


@pytest.mark.parametrize(
    ("environment", "variable"),
    [
        ({"GH_TOKEN": "one", "GITHUB_TOKEN": "two"}, "GH_TOKEN"),
        ({"GITHUB_TOKEN": "two"}, "GITHUB_TOKEN"),
        ({"AGENT_GITHUB_TOKEN": "   ", "GITHUB_TOKEN": "two"}, "GITHUB_TOKEN"),
    ],
)
def test_the_remaining_variables_follow_the_client_s_own_precedence(
    environment: dict[str, str], variable: str
) -> None:
    assert credential(environment).variable == variable


def test_with_no_credential_nothing_is_published_at_all() -> None:
    """The client would fall back to whatever account this machine is logged in as, and this
    adapter's first live check did exactly that: five reviews under a person's name."""
    with pytest.raises(ScmError, match="no credential for the agent"):
        credential({"UNRELATED": "x"})


def test_a_body_without_a_marker_belongs_to_nobody() -> None:
    assert read("looks like a finding but is not") == ""
    assert read(stamp("hello", "capabilities/x:a:b")) == "capabilities/x:a:b"


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:amsokol/ai-devsecops-agent.git",
        "https://github.com/amsokol/ai-devsecops-agent.git",
        "https://token@github.com/amsokol/ai-devsecops-agent",
        "ssh://git@github.com/amsokol/ai-devsecops-agent.git",
    ],
)
def test_the_target_is_read_from_the_remote_in_every_form_git_writes_it(url: str) -> None:
    assert GitHub.at(url, token=_token()).slug == "amsokol/ai-devsecops-agent"


def test_a_remote_that_is_not_github_is_refused_rather_than_guessed() -> None:
    with pytest.raises(ScmError, match="not a GitHub remote"):
        GitHub.at("git@gitlab.com:someone/else.git", token=_token())
