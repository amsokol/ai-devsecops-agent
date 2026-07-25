"""Runs that somebody's comment started.

Everything here is about what a sentence typed by a person is allowed to cause. The refusals are
tested as thoroughly as the successes, because a wake spends money and — for an unlock — grants
permission: "it did not answer its own comment" and "it does not take orders from a reader" are the
properties that keep the mechanism affordable and safe, and an untested property is a hope.

The classifier and the writer are scripted rather than live. What a model would say about "approved,
do it" is not what these tests are for; what the agent does once something has said it is.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml
from agent import patch
from agent.backends.fake import FakeBackend, Scripted
from agent.backends.port import Brief
from agent.config import Config
from agent.domain import Trigger
from agent.errors import ExitCode
from agent.intent import Course, narrow
from agent.library import Library
from agent.orchestrator import Request, RunRecord, run
from agent.overlay import Overlay
from agent.planner import plan_run
from agent.scm import marker
from agent.scm.fake import FakePlatform
from agent.scm.port import Comment, Issue, ScmError, Thread
from agent.wake import Wake

PERSON = "amsokol"
KEY = "capabilities/deps-outdated:ecosystems/python-uv:jinja2:behind"
REMARK = "`jinja2` is 3.1.2 and 3.1.4 has been out for 40 days."
COMMENT = 11


def issue_of(number: int = 7, *, key: str = KEY) -> Issue:
    return Issue(
        number=number,
        key=key,
        title="agent: deps-outdated — jinja2",
        body=marker.stamp(REMARK, key) if key else REMARK,
        reference=f"fake://issue/{number}",
    )


def said(body: str, *, author: str = PERSON, bot: bool = False, parent: int = 0) -> Comment:
    return Comment(
        id=COMMENT,
        author=author,
        bot=bot,
        body=body,
        parent=parent,
        reference=f"fake://comment/{COMMENT}",
    )


def on_issue(
    body: str,
    *,
    key: str = KEY,
    writers: tuple[str, ...] = (PERSON,),
    strangers: tuple[str, ...] = (),
) -> FakePlatform:
    """A platform holding one issue the agent raised, with one comment on it."""
    issue = issue_of(key=key)
    return FakePlatform(
        tracked=[issue],
        labels={issue.number: ("agent",)},
        said={COMMENT: said(body)},
        writers=writers,
        strangers=strangers,
    )


def on_change(
    body: str,
    *,
    key: str = KEY,
    number: int = 4,
    path: str = "",
    line: int = 0,
    fork: str = "",
) -> FakePlatform:
    """A platform holding one of the agent's review threads, with a reply in it.

    `path` and `line` are where the thread hangs. They decide whether a prepared change can be
    offered as a one-click suggestion, so the tests about that shape state them.
    """
    thread = Thread(
        id="thread-1",
        comment="1",
        key=key,
        body=marker.stamp(REMARK, key) if key else REMARK,
        number=number,
        path=path,
        line=line,
    )
    return FakePlatform(
        opened=[thread],
        said={COMMENT: said(body, parent=1)},
        writers=(PERSON,),
        fork=fork,
    )


def scripted(
    intent: str, *, confident: bool = True, reply: str = "Bump it to 3.1.4."
) -> FakeBackend:
    """A classifier and a writer that say what the test needs them to say."""
    return FakeBackend(
        answers={
            "wake-intent": Scripted(
                result={"intent": intent, "confident": confident, "gist": "the person's point"}
            ),
            "wake-answer": Scripted(result={"outcome": "answered", "reply": reply}),
        }
    )


def woken_on_issue(
    repo: Path,
    library_root: Path,
    overlay_root: Path,
    config_dir: Path,
    tmp_path: Path,
    *,
    publish: bool = True,
) -> Request:
    return Request(
        trigger=Trigger.COMMENT_ON_ISSUE,
        repository=repo,
        library_path=library_root,
        overlay_path=overlay_root,
        run_dir=tmp_path / "runs",
        config_dir=config_dir,
        wake=Wake(actor=PERSON, comment=COMMENT, issue=7),
        publish=publish,
    )


def woken_on_change(
    repo: Path,
    library_root: Path,
    overlay_root: Path,
    config_dir: Path,
    tmp_path: Path,
) -> Request:
    return Request(
        trigger=Trigger.COMMENT_ON_CHANGE,
        repository=repo,
        library_path=library_root,
        overlay_path=overlay_root,
        run_dir=tmp_path / "runs",
        config_dir=config_dir,
        base="main",
        change=4,
        wake=Wake(actor=PERSON, comment=COMMENT, change=4),
        publish=True,
    )


def only(record: RunRecord, task: str) -> list[dict[str, object]]:
    return [entry for entry in record.manifest.models if entry.get("task") == task]


def test_a_question_in_a_review_thread_is_answered_there_and_judges_nothing(
    git_repo: Path, library_root: Path, overlay_root: Path, config_dir: Path, tmp_path: Path
) -> None:
    """The scenario this exists for: a person cannot see how to fix a remark and asks in the thread.

    What comes back is a reply, and only a reply. No stance is published on the change — the run
    analysed nothing and has no opinion to revise — and the thread is left unresolved, because a
    sentence of prose is not evidence that anything was fixed.
    """
    platform = on_change("how would I even fix this?")
    record = run(
        woken_on_change(git_repo, library_root, overlay_root, config_dir, tmp_path),
        platform=platform,
        backend=scripted("fix"),
    )

    assert record.exit_code == int(ExitCode.OK)
    assert record.manifest.result == "answered"
    assert record.manifest.wake["intent"] == "fix"
    assert record.manifest.wake["course"] == Course.ANSWER.value
    assert record.manifest.wake["finding"] == KEY
    # The reply carries the marker, so a later run reads this comment as the agent's own and does
    # not wake on it.
    key, note = platform.replies[0]
    assert key == KEY
    assert "Bump it to 3.1.4." in note
    assert marker.read(note) == KEY
    assert not platform.reviews
    assert all(not thread.resolved for thread in platform.opened)
    assert record.manifest.actions["answer"]["posted"] is True
    assert record.manifest.verdict == {}
    # Both sessions are on the books. The classifier's cost is small and constant, which is exactly
    # why leaving it out would be tempting and wrong.
    assert len(only(record, "wake-intent")) == 1
    assert len(only(record, "wake-answer")) == 1
    assert record.manifest.cost["sessions"] == 2


def test_an_unsure_classification_answers_rather_than_acting(
    git_repo: Path, library_root: Path, overlay_root: Path, config_dir: Path, tmp_path: Path
) -> None:
    """An unsure `unlock` is permission nobody gave. Answering leaves the person able to say what
    they meant, and costs one small session instead of a run."""
    platform = on_issue("do it if you think that's right, I guess?")
    record = run(
        woken_on_issue(git_repo, library_root, overlay_root, config_dir, tmp_path),
        platform=platform,
        backend=scripted("unlock", confident=False),
    )

    assert record.manifest.wake["intent"] == "unlock"
    assert record.manifest.wake["confident"] is False
    assert record.manifest.wake["course"] == Course.ANSWER.value
    assert record.manifest.result == "answered"
    assert not record.manifest.findings
    assert [item.what for item in platform.calls] == ["note"]


def test_a_comment_asking_for_nothing_costs_one_classification_and_writes_nothing(
    git_repo: Path, library_root: Path, overlay_root: Path, config_dir: Path, tmp_path: Path
) -> None:
    """ "Thanks!" is the most common comment there is. A machine that answers it is a machine people
    mute, so the run stops after reading — one cheap session, and no comment."""
    platform = on_issue("thanks!")
    record = run(
        woken_on_issue(git_repo, library_root, overlay_root, config_dir, tmp_path),
        platform=platform,
        backend=scripted("unrelated"),
    )

    assert record.exit_code == int(ExitCode.OK)
    assert record.manifest.result == "declined"
    assert record.manifest.wake["course"] == Course.IGNORE.value
    assert any("asks for nothing" in warning for warning in record.manifest.warnings)
    assert not platform.notes
    assert not platform.calls
    assert len(record.manifest.models) == 1


def test_an_unlock_rechecks_only_the_finding_it_was_written_on(
    git_repo: Path, library_root: Path, overlay_root: Path, config_dir: Path, tmp_path: Path
) -> None:
    """Scenario two: a person approves the change the agent said it was holding back.

    The run that follows is narrow on purpose. Somebody who writes on one issue is asking about one
    thing, and a weekly sweep in reply would make approving something the most expensive thing a
    person can do — and would bury the answer under a week of unrelated findings.

    Here the recheck finds the finding gone, so the issue is closed with evidence *and* the person
    is told on the issue they wrote on. Both matter: a run that closed the issue silently reads as
    having ignored them.
    """
    platform = on_issue("approved. do it.")
    record = run(
        woken_on_issue(git_repo, library_root, overlay_root, config_dir, tmp_path),
        platform=platform,
        backend=scripted("unlock"),
    )

    assert record.manifest.wake["course"] == Course.RECHECK.value
    assert [task.id for task in record.manifest.tasks] == ["deps-outdated@python-uv"]
    assert [entry["capability"] for entry in record.manifest.skipped] == [
        "capabilities/code-quality",
        "capabilities/code-vuln",
        "capabilities/deps-vuln",
    ]
    assert all("not what" in entry["reason"] for entry in record.manifest.skipped)
    assert record.manifest.result == "pass"
    assert [item.key for item in platform.closed] == [KEY]
    status = next(body for key, body in platform.notes if "You asked for this" in body)
    assert "no longer among its findings" in status
    assert marker.read(status) == KEY
    assert record.manifest.actions["status"] == {"posted": True, "failure": ""}
    assert len(only(record, "wake-intent")) == 1
    assert not only(record, "wake-answer")


def test_a_recheck_whose_check_is_not_planned_here_changes_nothing(
    git_repo: Path, library_root: Path, overlay_root: Path, config_dir: Path, tmp_path: Path
) -> None:
    """An issue outlives the overlay that raised it. Asked to re-establish something this product no
    longer checks, the run says so rather than running the rest of the sweep to look busy."""
    platform = on_issue("please look again", key="capabilities/licences:ecosystems/npm:x:copyleft")
    record = run(
        woken_on_issue(git_repo, library_root, overlay_root, config_dir, tmp_path),
        platform=platform,
        backend=scripted("recheck"),
    )

    assert record.manifest.result == "declined"
    assert any("nothing to re-establish" in warning for warning in record.manifest.warnings)
    assert not platform.notes
    assert not record.manifest.findings


def test_a_classifier_that_breaks_still_gets_an_answer_out(
    git_repo: Path, library_root: Path, overlay_root: Path, config_dir: Path, tmp_path: Path
) -> None:
    """Silence is the one outcome a person cannot act on. A classification that fails falls back to
    the course that changes nothing, which is the same thing an unsure one does."""
    platform = on_issue("what does this mean?")
    backend = FakeBackend(
        answers={
            "wake-intent": Scripted(result="not json at all"),
            "wake-answer": Scripted(result={"outcome": "answered", "reply": "It means this."}),
        }
    )
    record = run(
        woken_on_issue(git_repo, library_root, overlay_root, config_dir, tmp_path),
        platform=platform,
        backend=backend,
    )

    assert record.manifest.result == "answered"
    assert record.manifest.wake["course"] == Course.ANSWER.value
    assert record.manifest.wake["detail"]
    assert "It means this." in platform.notes[0][1]


def test_an_answer_that_could_not_be_written_says_so_where_it_was_asked(
    git_repo: Path, library_root: Path, overlay_root: Path, config_dir: Path, tmp_path: Path
) -> None:
    """A question left visibly unanswered beats one left silently unanswered: the person can ask
    somebody else, and the comment says nothing was changed."""
    platform = on_issue("what does this mean?")
    backend = FakeBackend(
        answers={
            "wake-intent": Scripted(
                result={"intent": "question", "confident": True, "gist": "asks what it means"}
            ),
            "wake-answer": Scripted(result={"outcome": "unverified", "reason": "unavailable"}),
        }
    )
    record = run(
        woken_on_issue(git_repo, library_root, overlay_root, config_dir, tmp_path),
        platform=platform,
        backend=backend,
    )

    assert record.manifest.result == "answered"
    assert record.manifest.actions["answer"]["outcome"] == "unverified"
    assert "could not answer" in platform.notes[0][1]
    assert "Nothing was changed" in platform.notes[0][1]


def test_a_reader_s_comment_does_not_start_a_run(
    git_repo: Path, library_root: Path, overlay_root: Path, config_dir: Path, tmp_path: Path
) -> None:
    """Without this check, anybody who can type in an issue spends the budget and grants the
    permissions. Refused before the first model call, and recorded as a refusal."""
    platform = on_issue("approved. do it.", writers=())
    record = run(
        woken_on_issue(git_repo, library_root, overlay_root, config_dir, tmp_path),
        platform=platform,
        backend=scripted("unlock"),
    )

    assert record.exit_code == int(ExitCode.OK)
    assert record.manifest.result == "declined"
    assert any("no write access" in warning for warning in record.manifest.warnings)
    assert not record.manifest.models
    assert not platform.notes


def test_write_access_that_cannot_be_established_is_treated_as_no(
    git_repo: Path, library_root: Path, overlay_root: Path, config_dir: Path, tmp_path: Path
) -> None:
    """A credential that may not read collaborators makes every commenter look like a stranger. The
    permissive reading of that would let anyone unlock a major upgrade, and it cannot be taken back
    once it has, so the answer is no — with the reason, because it is a fixable configuration."""
    platform = on_issue("approved. do it.", writers=(), strangers=(PERSON,))
    record = run(
        woken_on_issue(git_repo, library_root, overlay_root, config_dir, tmp_path),
        platform=platform,
        backend=scripted("unlock"),
    )

    assert record.manifest.result == "declined"
    assert any("could not be established" in warning for warning in record.manifest.warnings)
    assert not record.manifest.models


def test_a_comment_on_somebody_else_s_issue_is_not_the_agent_s_to_answer(
    git_repo: Path, library_root: Path, overlay_root: Path, config_dir: Path, tmp_path: Path
) -> None:
    """The marker decides, exactly as it does when publishing. An issue the agent never raised has
    no finding key in it, so there is nothing to recheck and nothing to unlock."""
    platform = on_issue("approved. do it.", key="")
    record = run(
        woken_on_issue(git_repo, library_root, overlay_root, config_dir, tmp_path),
        platform=platform,
        backend=scripted("unlock"),
    )

    assert record.manifest.result == "declined"
    assert any("carries no marker" in warning for warning in record.manifest.warnings)
    assert not record.manifest.models


def test_a_reply_outside_the_agent_s_own_threads_is_declined(
    git_repo: Path, library_root: Path, overlay_root: Path, config_dir: Path, tmp_path: Path
) -> None:
    """A remark the agent never made is not one it can answer for."""
    platform = on_change("how would I fix this?")
    platform.opened = []
    record = run(
        woken_on_change(git_repo, library_root, overlay_root, config_dir, tmp_path),
        platform=platform,
        backend=scripted("fix"),
    )

    assert record.manifest.result == "declined"
    assert any("not in one of the agent's own threads" in w for w in record.manifest.warnings)
    assert not platform.replies


def test_authority_and_words_have_to_belong_to_the_same_account(
    git_repo: Path, library_root: Path, overlay_root: Path, config_dir: Path, tmp_path: Path
) -> None:
    """The two arrive from one event and normally agree. When they do not, something is passing one
    person's write access with another person's words — and the words are what would be acted on."""
    platform = on_issue("approved. do it.")
    platform.said = {COMMENT: said("approved. do it.", author="mallory")}
    record = run(
        woken_on_issue(git_repo, library_root, overlay_root, config_dir, tmp_path),
        platform=platform,
        backend=scripted("unlock"),
    )

    assert record.manifest.result == "declined"
    assert any("has to be the one that spoke" in warning for warning in record.manifest.warnings)
    assert not record.manifest.models


def test_a_comment_the_platform_reports_as_a_machine_s_is_declined(
    git_repo: Path, library_root: Path, overlay_root: Path, config_dir: Path, tmp_path: Path
) -> None:
    """The event's actor can be a person while the comment is not — an integration acting on
    somebody's behalf. What the platform says about the comment wins."""
    platform = on_issue("approved. do it.")
    platform.said = {COMMENT: said("approved. do it.", bot=True)}
    record = run(
        woken_on_issue(git_repo, library_root, overlay_root, config_dir, tmp_path),
        platform=platform,
        backend=scripted("unlock"),
    )

    assert record.manifest.result == "declined"
    assert any("written by a machine" in warning for warning in record.manifest.warnings)


def test_a_wake_with_no_credential_reads_nothing_and_says_so(
    git_repo: Path, library_root: Path, overlay_root: Path, config_dir: Path, tmp_path: Path
) -> None:
    """A run woken by a comment has to read that comment. Guessing what it said from the event alone
    would mean acting on a sentence nobody has seen."""
    request = woken_on_issue(
        git_repo, library_root, overlay_root, config_dir, tmp_path, publish=False
    )
    record = run(request, backend=scripted("unlock"))

    assert record.manifest.result == "declined"
    assert any("no credential" in warning for warning in record.manifest.warnings)
    assert not record.manifest.models


def test_an_answer_nobody_asked_to_publish_stays_in_the_record(
    git_repo: Path, library_root: Path, overlay_root: Path, config_dir: Path, tmp_path: Path
) -> None:
    """A dry look at what the agent would say. The reply is written and kept, and the record says
    why nothing was posted — the same shape every other unpublished decision has."""
    platform = on_issue("what does this mean?")
    request = woken_on_issue(
        git_repo, library_root, overlay_root, config_dir, tmp_path, publish=False
    )
    record = run(request, platform=platform, backend=scripted("question"))

    assert record.manifest.result == "answered"
    assert record.manifest.actions["answer"]["posted"] is False
    assert any("not asked to publish" in warning for warning in record.manifest.warnings)
    assert not platform.notes
    assert record.report is not None
    assert "Bump it to 3.1.4." in record.report


class LockedIssue(FakePlatform):
    """A platform that answers everything a wake needs to read, and then refuses to be written to.

    A real state: an issue can be locked between the comment that woke the run and the reply to it.
    """

    def note(self, issue: Issue, body: str) -> None:
        raise ScmError("the issue is locked")


def test_a_platform_that_will_not_take_the_reply_keeps_the_run(
    git_repo: Path, library_root: Path, overlay_root: Path, config_dir: Path, tmp_path: Path
) -> None:
    """The answer exists either way. Losing the run over a comment that would not post would make
    the agent less reliable than the platform it talks to."""
    issue = issue_of()
    platform = LockedIssue(
        tracked=[issue],
        labels={issue.number: ("agent",)},
        said={COMMENT: said("what does this mean?")},
        writers=(PERSON,),
    )
    record = run(
        woken_on_issue(git_repo, library_root, overlay_root, config_dir, tmp_path),
        platform=platform,
        backend=scripted("question"),
    )

    assert record.exit_code == int(ExitCode.OK)
    assert record.manifest.result == "answered"
    assert any("was not posted" in warning for warning in record.manifest.warnings)
    assert record.manifest.actions["answer"]["failure"]
    assert record.report is not None
    assert "Bump it to 3.1.4." in record.report


SETTINGS = """\
[project]
name = "product"
version = "0.1.0"
dependencies = ["requests==2.31.0"]

[tool.ruff]
line-length = 100

[tool.pytest.ini_options]
testpaths = ["tests"]
"""
PIN = 4
"""Which line of that file the pin is on, which is where a thread about it would hang."""
VERIFY = ("uv", "--version")
"""A verification command that really runs and really passes, so "verified" means what it says."""


def settled(repo: Path) -> None:
    """Put a file worth editing in the repository, committed, so a patch shows up as a diff."""
    (repo / "pyproject.toml").write_text(SETTINGS, encoding="utf-8")
    env = {
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "PATH": "/usr/bin:/bin",
        "HOME": str(repo.parent),
    }
    subprocess.run(["git", "-C", str(repo), "add", "--all"], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "--quiet", "-m", "settings"], check=True, env=env
    )


def fixing(overlay_root: Path) -> Path:
    """The same overlay with a fixing model bound to *reviews*, and a verification that passes.

    Two separate decisions, and a product makes both. Without the binding a review can only explain;
    without commands there is nothing a patch could be checked against, and it would be offered with
    no label worth reading.
    """
    path = overlay_root / "agent.yaml"
    values = yaml.safe_load(path.read_text(encoding="utf-8"))
    values["review"]["models"]["fixer"] = "fake/composer-2.5"
    values["verification"] = {"python-uv": [list(VERIFY)]}
    path.write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")
    return overlay_root


def patcher(
    *,
    edits: tuple[tuple[str, str, str], ...] = (("pyproject.toml", "2.31.0", "2.32.4"),),
    verify: tuple[str, ...] | None = VERIFY,
    outcome: str = "fixed",
    notes: str = "Moving the pin to 2.32.4 drops the vulnerable range entirely.",
) -> FakeBackend:
    """A classifier that reads a comment as `fix`, and a fixing session that edits and verifies."""

    def act(brief: Brief) -> None:
        if brief.task.id != "wake-patch":
            return
        for path, find, replace in edits:
            brief.toolkit.call("edit_file", {"path": path, "find": find, "replace": replace})
        if verify is not None:
            brief.toolkit.call("run_command", {"command": list(verify)})

    return FakeBackend(
        answers={
            "wake-intent": Scripted(
                result={"intent": "fix", "confident": True, "gist": "asks how to fix it"}
            ),
            "wake-patch": Scripted(result={"outcome": outcome, "notes": notes}),
        },
        on_execute=act,
    )


def branches(repo: Path) -> set[str]:
    output = subprocess.run(
        ["git", "-C", str(repo), "branch", "--format=%(refname:short)"],
        check=True,
        capture_output=True,
        text=True,
    )
    return {line.strip() for line in output.stdout.splitlines() if line.strip()}


def test_a_question_about_fixing_is_answered_with_the_change_itself(
    git_repo: Path, library_root: Path, overlay_root: Path, config_dir: Path, tmp_path: Path
) -> None:
    """The first scenario this whole mechanism exists for, end to end.

    A person cannot see how to fix a remark and asks in the thread. What comes back is the edit,
    tried in a scratch checkout with the product's own verification over it, offered as a block the
    platform can apply — and nothing else. No branch, no commit, no push, no stance on the change.
    """
    settled(git_repo)
    platform = on_change("how would I even fix this?", path="pyproject.toml", line=PIN)
    record = run(
        woken_on_change(git_repo, library_root, fixing(overlay_root), config_dir, tmp_path),
        platform=platform,
        backend=patcher(),
    )

    assert record.exit_code == int(ExitCode.OK)
    assert record.manifest.result == "answered"
    assert record.manifest.wake["course"] == Course.PATCH.value
    key, note = platform.replies[0]
    assert key == KEY
    # The paragraph is the session's, addressed to the person; the block under it is git's.
    assert "drops the vulnerable range" in note
    assert '```suggestion\ndependencies = ["requests==2.32.4"]\n```' in note
    assert "passed in full" in note
    assert "Nothing was committed and nothing was pushed" in note
    assert marker.read(note) == KEY
    # Nothing about the repository moved: not the tree under review, not a branch, not a worktree.
    assert "2.31.0" in (git_repo / "pyproject.toml").read_text(encoding="utf-8")
    assert branches(git_repo) == {"main"}
    assert not platform.reviews and not platform.pushed
    assert record.manifest.verdict == {}
    prepared = record.manifest.actions["answer"]["prepared"]
    assert prepared["form"] == "suggestion"
    assert prepared["changed"] == ["pyproject.toml"]
    assert prepared["verification"]["passed"] is True
    # Two sessions: reading the comment, and making the change. No analysis was paid for.
    assert len(only(record, "wake-intent")) == 1
    assert len(only(record, "wake-patch")) == 1
    assert record.manifest.cost["sessions"] == 2


def test_the_same_question_on_a_fork_is_answered_in_prose_and_says_why(
    git_repo: Path, library_root: Path, overlay_root: Path, config_dir: Path, tmp_path: Path
) -> None:
    """A change from outside the repository is read and never executed, so there is nothing to
    verify a patch against — and a session that prepared one would have to run in the same place the
    credentials are. The person still gets an answer, and it says why it is only an answer.

    The fixer is bound here exactly as in the test above. What differs is one fact from the
    platform, which is the point: nothing about the product's configuration changes.
    """
    settled(git_repo)
    platform = on_change(
        "how do I fix this?", path="pyproject.toml", line=PIN, fork="stranger/product"
    )
    record = run(
        woken_on_change(git_repo, library_root, fixing(overlay_root), config_dir, tmp_path),
        platform=platform,
        backend=scripted("fix"),
    )

    assert record.exit_code == int(ExitCode.OK)
    assert record.manifest.wake["intent"] == "fix"
    assert record.manifest.wake["course"] == Course.ANSWER.value
    assert record.manifest.posture["executes"] is False
    note = platform.replies[0][1]
    assert "Bump it to 3.1.4." in note
    assert "```suggestion" not in note and "```diff" not in note
    assert "comes from outside the repository" in note
    assert not record.manifest.actions["answer"]["prepared"]
    assert branches(git_repo) == {"main"}


def test_a_patch_nobody_could_verify_is_still_offered_and_says_so(
    git_repo: Path, library_root: Path, overlay_root: Path, config_dir: Path, tmp_path: Path
) -> None:
    """The question was how to fix it, so an unverified answer beats no answer — labelled.

    The label is not a formality. A person clicking "commit suggestion" is trusting it, and it is
    read off the commands the session actually ran rather than off what the session claimed.
    """
    settled(git_repo)
    platform = on_change("how do I fix this?", path="pyproject.toml", line=PIN)
    record = run(
        woken_on_change(git_repo, library_root, fixing(overlay_root), config_dir, tmp_path),
        platform=platform,
        backend=patcher(verify=None),
    )

    note = platform.replies[0][1]
    assert "```suggestion" in note
    assert "could not be shown safe" in note
    assert "passed in full" not in note
    assert record.manifest.actions["answer"]["prepared"]["verification"]["passed"] is False


def test_a_change_that_touches_more_than_the_remark_arrives_as_a_diff(
    git_repo: Path, library_root: Path, overlay_root: Path, config_dir: Path, tmp_path: Path
) -> None:
    """A suggestion replaces one range of one file and nothing else. Anything wider is a perfectly
    good change and a wrong suggestion: the person would click apply and get a different edit."""
    settled(git_repo)
    platform = on_change("how do I fix this?", path="pyproject.toml", line=PIN)
    record = run(
        woken_on_change(git_repo, library_root, fixing(overlay_root), config_dir, tmp_path),
        platform=platform,
        backend=patcher(
            edits=(
                ("pyproject.toml", "2.31.0", "2.32.4"),
                ("README.md", "product", "product, patched"),
            )
        ),
    )

    note = platform.replies[0][1]
    assert "```diff" in note
    assert "```suggestion" not in note
    assert record.manifest.actions["answer"]["prepared"]["form"] == "diff"
    assert sorted(record.manifest.actions["answer"]["prepared"]["changed"]) == [
        "README.md",
        "pyproject.toml",
    ]


def test_a_change_somewhere_else_in_the_file_is_not_offered_as_a_suggestion(
    git_repo: Path, library_root: Path, overlay_root: Path, config_dir: Path, tmp_path: Path
) -> None:
    """A suggestion is applied to the lines the thread hangs on, whatever the patch touched. When
    those are not the same lines, the diff is shown instead of an offer that would edit the wrong
    place."""
    settled(git_repo)
    platform = on_change("how do I fix this?", path="pyproject.toml", line=PIN + 5)
    record = run(
        woken_on_change(git_repo, library_root, fixing(overlay_root), config_dir, tmp_path),
        platform=platform,
        backend=patcher(),
    )

    assert record.manifest.actions["answer"]["prepared"]["form"] == "diff"
    assert "```suggestion" not in platform.replies[0][1]


def test_a_change_too_long_to_read_is_described_rather_than_pasted(
    git_repo: Path,
    library_root: Path,
    overlay_root: Path,
    config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A regenerated lock file is thousands of lines. Half of one looks like a whole one, so the
    comment names the files and leaves the paragraph to say what the change does."""
    settled(git_repo)
    monkeypatch.setattr(patch, "MAX_DIFF_LINES", 1)
    platform = on_change("how do I fix this?", path="pyproject.toml", line=PIN + 5)
    record = run(
        woken_on_change(git_repo, library_root, fixing(overlay_root), config_dir, tmp_path),
        platform=platform,
        backend=patcher(),
    )

    note = platform.replies[0][1]
    assert "too much to read in a comment" in note
    assert "`pyproject.toml`" in note
    assert "```diff" not in note
    assert record.manifest.actions["answer"]["prepared"]["form"] == "described"


def test_a_session_that_arrived_at_no_change_answers_in_prose(
    git_repo: Path, library_root: Path, overlay_root: Path, config_dir: Path, tmp_path: Path
) -> None:
    """A refusal is an answer too: the person learns why the obvious fix is not one. What must not
    happen is a reply that reads as an offer with nothing behind it."""
    settled(git_repo)
    platform = on_change("how do I fix this?", path="pyproject.toml", line=PIN)
    record = run(
        woken_on_change(git_repo, library_root, fixing(overlay_root), config_dir, tmp_path),
        platform=platform,
        backend=patcher(
            edits=(),
            outcome="refused",
            notes="Nothing here can move: 3.1.4 needs a Python this product does not run yet.",
        ),
    )

    note = platform.replies[0][1]
    assert "needs a Python this product does not run yet" in note
    assert "```suggestion" not in note and "```diff" not in note
    assert "did not arrive at one" in note
    assert record.manifest.actions["answer"]["prepared"]["form"] == "none"


def test_narrowing_keeps_only_the_check_that_owns_the_finding(
    library: Library, overlay: Overlay, config: Config
) -> None:
    """The arithmetic on its own. The ecosystem is matched as a whole segment of the key, so a
    package named after one cannot pull in that ecosystem's task."""
    plan = plan_run(
        scenario=config.scenario_for(Trigger.MAINTAIN_REQUESTED),
        trigger=Trigger.MAINTAIN_REQUESTED,
        library=library,
        overlay=overlay,
        change=None,
    )
    kept, why = narrow(plan, KEY)

    assert not why
    assert [task.id for task in kept.tasks] == ["deps-outdated@python-uv"]

    _, refused = narrow(plan, "capabilities/nothing-like-this:x:y")
    assert "not part of this run's plan" in refused
