"""The fix phase: which findings get a branch, and what it takes for one to survive.

The properties worth proving here are the ones that decide whether a fix branch can be trusted: a
branch appears only when the tree actually changed and verification actually ran, the tree under
maintenance is never touched, and a refusal is recorded rather than swallowed.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from agent.backends import FakeBackend
from agent.backends.fake import Scripted
from agent.backends.port import Brief, Budget
from agent.backends.select import Roster
from agent.budget import Ledger, RunBudget
from agent.cli import main
from agent.domain import FixOutcome, PlannedTask, Role, Trigger
from agent.evidence import Reliability, Subject
from agent.findings import Action, Finding, Klass, Location, Severity
from agent.library import Library
from agent.manifest import Manifest
from agent.orchestrator import _account
from agent.overlay import Overlay
from agent.remediate import Fix, apply, branch_for, plan_fixes
from agent.repo import Repository
from agent.session import Session
from agent.storage import FactCache
from agent.toolkit import Toolkits
from agent.tools import Grants
from agent.verdict import Judged
from agent.verification import Surfaces

NO_VERIFICATION = """\
schema: 1
review:
  models:
    analyst: fake/none
  limits:
    tokens_per_run: 9
    minutes_per_task: 15
    tasks_at_once: 4
maintenance:
  models:
    analyst: fake/none
    fixer: fake/none
  limits:
    tokens_per_run: 9
    minutes_per_task: 10
    tasks_at_once: 2
  queue:
    max_new_issues_per_run: 5
    max_open_fix_requests: 3
ecosystems:
  - ecosystems/python-uv
hotspots:
  - src
quarantine:
  days: 7
"""

OTHER_ECOSYSTEM_ONLY = (
    NO_VERIFICATION
    + """\
verification:
  cargo:
    - [cargo, --version]
"""
)

VERIFY = ("uv", "--version")
SECOND = ("uv", "cache", "dir")
BROKEN = ("uv", "--no-such-option")
SURFACES: Surfaces = {"python-uv": (VERIFY,), "broken": (BROKEN,)}
PAIRED: Surfaces = {"python-uv": (VERIFY, SECOND)}
ONLY_RED_WHEN_CHANGED = (
    "python3",
    "-c",
    "import pathlib, sys; sys.exit('2.32.4' in pathlib.Path('pyproject.toml').read_text())",
)
"""Green on the unchanged head and red once the fix is in the tree: a failure the change caused."""


def judged(
    *,
    klass: Klass = Klass.SECURITY,
    severity: Severity = Severity.HIGH,
    package: str = "requests",
    remediation: str = "Move the pin to 2.32.4.",
    reliability: Reliability = Reliability.REPRODUCIBLE,
    advisory: str = "",
    target: str = "2.32.4",
) -> Judged:
    finding = Finding(
        capability="capabilities/deps-vuln",
        klass=klass,
        severity=severity,
        subject=Subject(ecosystem="ecosystems/python-uv", package=package, version="2.31.0"),
        summary=f"{package} is affected by an advisory.",
        rationale="The advisory covers the pinned version.",
        evidence=("advisories|ecosystems/python-uv|requests|2.31.0|",),
        remediation=remediation,
        location=Location(path="pyproject.toml", line=12),
        advisory=advisory or f"PYSEC-{package}",
        target=target,
    )
    return Judged(
        finding=finding,
        action=Action.BLOCK,
        reliability=reliability,
        capped=reliability is not Reliability.REPRODUCIBLE,
    )


def without_verification(root: Path, library: Library, overlay: Overlay) -> Overlay:
    (root / "agent.yaml").write_text(NO_VERIFICATION, encoding="utf-8")
    return Overlay.load(root, library=library, notes_limit=100_000)


def with_verification_for_another_ecosystem(
    root: Path, library: Library, overlay: Overlay
) -> Overlay:
    del overlay  # same trigger/notes path as the fixture; only the values file changes
    (root / "agent.yaml").write_text(OTHER_ECOSYSTEM_ONLY, encoding="utf-8")
    return Overlay.load(root, library=library, notes_limit=100_000)


def test_a_branch_name_follows_the_subject_and_never_the_run_or_the_advisory() -> None:
    """Two runs must reuse one branch for one subject, and never share one between two.

    Advisory-independence is the load-bearing half. The group on a branch is every finding about one
    pin, and which of them is strictest changes as advisories appear and get fixed; a branch named
    after one of them would move under the same repository state and open a second pull request.
    """
    first, second = judged(), judged()
    assert branch_for(first) == branch_for(second)
    assert branch_for(first).startswith("agent/security/")
    assert branch_for(judged(advisory="PYSEC-2026-9999")) == branch_for(first)
    assert branch_for(judged(package="urllib3")) != branch_for(first)
    assert branch_for(judged(klass=Klass.ROUTINE)).startswith("agent/routine/")


def test_only_a_demonstrated_finding_with_a_remedy_is_fixed(
    library: Library, overlay: Overlay, git_repo: Path
) -> None:
    """Changing shipping code on a guess is worse than commenting on one."""
    queue = plan_fixes(
        (
            judged(package="requests"),
            judged(package="urllib3", reliability=Reliability.HEURISTIC),
            judged(package="idna", remediation=""),
        ),
        library=library,
        overlay=overlay,
        playbook="playbooks/maintain",
        repository=Repository.open(git_repo),
        max_open_fix_requests=5,
    )
    assert [job.judged.finding.subject.package for job in queue.jobs] == ["requests"]
    deferred = dict(queue.deferred)
    assert "heuristic" in deferred[judged(package="urllib3").finding.key]
    assert "no remediation" in deferred[judged(package="idna").finding.key]


def test_a_pin_with_nowhere_to_move_is_reported_and_not_fixed(
    library: Library, overlay: Overlay, git_repo: Path
) -> None:
    """Quarantine makes these weekly: real, reported, and with no cleared version to move to.

    The first live run queued one anyway, and the session invented a move — a major downgrade of an
    action, which nobody asked for and no evidence supported.
    """
    waiting = judged(
        package="actions/setup-python",
        remediation="Wait until v7.0.0 clears quarantine before this pin is adoptable.",
        target="",
    )

    queue = plan_fixes(
        (judged(package="requests"), waiting),
        library=library,
        overlay=overlay,
        playbook="playbooks/maintain",
        repository=Repository.open(git_repo),
        max_open_fix_requests=5,
    )

    assert [job.judged.finding.subject.package for job in queue.jobs] == ["requests"]
    assert "no version to move to" in dict(queue.deferred)[waiting.finding.key]


def test_security_is_queued_first_and_the_queue_has_a_ceiling(
    library: Library, overlay: Overlay, git_repo: Path
) -> None:
    """A backlog of routine bumps must never crowd out an advisory."""
    queue = plan_fixes(
        (
            judged(klass=Klass.ROUTINE, severity=Severity.LOW, package="rich"),
            judged(klass=Klass.ROUTINE, severity=Severity.HIGH, package="click"),
            judged(package="requests"),
        ),
        library=library,
        overlay=overlay,
        playbook="playbooks/maintain",
        repository=Repository.open(git_repo),
        max_open_fix_requests=2,
    )
    assert [job.judged.finding.subject.package for job in queue.jobs] == ["requests", "click"]
    assert (
        "the queue allows 2 open fix request(s)"
        in dict(queue.deferred)[
            judged(klass=Klass.ROUTINE, severity=Severity.LOW, package="rich").finding.key
        ]
    )


def test_findings_on_one_subject_become_one_job(
    library: Library, overlay: Overlay, git_repo: Path
) -> None:
    """Three advisories against one pin are one bump, not three branches carrying the same edit."""
    queue = plan_fixes(
        (
            judged(advisory="PYSEC-2026-1872"),
            judged(advisory="PYSEC-2026-1873", severity=Severity.CRITICAL),
            judged(advisory="PYSEC-2026-2275"),
            judged(package="urllib3"),
        ),
        library=library,
        overlay=overlay,
        playbook="playbooks/maintain",
        repository=Repository.open(git_repo),
        max_open_fix_requests=5,
    )
    assert len(queue.jobs) == 2
    grouped = queue.jobs[0]
    # The strictest of the group leads it, and the other two travel with it.
    assert grouped.judged.finding.advisory == "PYSEC-2026-1873"
    assert len(grouped.keys) == 3
    assert grouped.branch == branch_for(judged())
    assert queue.deferred == ()


def test_a_full_queue_defers_a_whole_group(
    library: Library, overlay: Overlay, git_repo: Path
) -> None:
    """A ceiling counts change requests, so every finding of a deferred one says why it waited."""
    queue = plan_fixes(
        (
            judged(),
            judged(package="urllib3", advisory="PYSEC-A"),
            judged(package="urllib3", advisory="PYSEC-B"),
        ),
        library=library,
        overlay=overlay,
        playbook="playbooks/maintain",
        repository=Repository.open(git_repo),
        max_open_fix_requests=1,
    )
    assert len(queue.jobs) == 1
    assert len(queue.deferred) == 2
    assert all("the queue allows 1 open fix request(s)" in reason for _, reason in queue.deferred)


def test_a_subject_already_under_review_is_left_alone(
    library: Library, overlay: Overlay, git_repo: Path
) -> None:
    """The branch name is stable per subject, so an open change request carrying it is this same
    fix. Preparing it again would ask a reviewer to read one edit twice."""
    first = judged()
    queue = plan_fixes(
        (first, judged(package="urllib3")),
        library=library,
        overlay=overlay,
        playbook="playbooks/maintain",
        repository=Repository.open(git_repo),
        max_open_fix_requests=5,
        proposed=(branch_for(first),),
    )
    assert [job.judged.finding.subject.package for job in queue.jobs] == ["urllib3"]
    assert "already under review" in dict(queue.deferred)[first.finding.key]


def test_the_queue_counts_the_change_requests_already_open(
    library: Library, overlay: Overlay, git_repo: Path
) -> None:
    """The limit is on a team's attention, so what is already waiting for them counts against it."""
    queue = plan_fixes(
        (judged(package="urllib3"),),
        library=library,
        overlay=overlay,
        playbook="playbooks/maintain",
        repository=Repository.open(git_repo),
        max_open_fix_requests=1,
        proposed=(branch_for(judged()),),
    )
    assert not queue.jobs
    assert "1 are open" in dict(queue.deferred)[judged(package="urllib3").finding.key]


def test_without_verification_commands_nothing_can_be_fixed(
    library: Library, overlay: Overlay, overlay_root: Path, git_repo: Path
) -> None:
    """A product that cannot verify gets findings reported, not branches nobody can trust."""
    bare = without_verification(overlay_root, library, overlay)
    queue = plan_fixes(
        (judged(),),
        library=library,
        overlay=bare,
        playbook="playbooks/maintain",
        repository=Repository.open(git_repo),
        max_open_fix_requests=5,
    )
    assert queue.jobs == ()
    assert "no verification commands" in dict(queue.deferred)[judged().finding.key]
    assert "pull request" in dict(queue.deferred)[judged().finding.key]


def test_without_a_surface_for_the_ecosystem_the_finding_is_human_only(
    library: Library, overlay: Overlay, overlay_root: Path, git_repo: Path
) -> None:
    """Omitting one ecosystem's surface is how a product says do not fix those findings."""
    partial = with_verification_for_another_ecosystem(overlay_root, library, overlay)
    queue = plan_fixes(
        (judged(),),
        library=library,
        overlay=partial,
        playbook="playbooks/maintain",
        repository=Repository.open(git_repo),
        max_open_fix_requests=5,
    )
    assert queue.jobs == ()
    reason = dict(queue.deferred)[judged().finding.key]
    assert "no verification surface for `python-uv`" in reason
    assert "pull request" in reason


def test_an_approval_lets_a_human_only_finding_prepare_a_ci_pr(
    library: Library, overlay: Overlay, overlay_root: Path, git_repo: Path
) -> None:
    """A write-access unlock on the issue is how a person asks for a PR when there is no surface."""
    from agent.unlock import Approval

    partial = with_verification_for_another_ecosystem(overlay_root, library, overlay)
    item = judged()
    queue = plan_fixes(
        (item,),
        library=library,
        overlay=partial,
        playbook="playbooks/maintain",
        repository=Repository.open(git_repo),
        max_open_fix_requests=5,
        approvals={item.finding.key: Approval(by="alice", comment=7, at="2026-07-27")},
    )
    assert len(queue.jobs) == 1
    assert queue.jobs[0].awaiting_ci is True
    assert queue.deferred == ()


def _apply(
    queue_backend: FakeBackend,
    *,
    library: Library,
    overlay: Overlay,
    git_repo: Path,
    tmp_path: Path,
    findings: tuple[Judged, ...],
    surfaces: Surfaces = SURFACES,
    binaries: frozenset[str] = frozenset({"uv"}),
) -> list[Fix]:
    repository = Repository.open(git_repo)
    queue = plan_fixes(
        findings,
        library=library,
        overlay=overlay,
        playbook="playbooks/maintain",
        repository=repository,
        max_open_fix_requests=5,
    )
    session = Session(
        repository=repository.path,
        grants=Grants(binaries=binaries, hosts=frozenset()),
        cache=FactCache(tmp_path / "cache", writable=False),
        scratch_root=tmp_path / "scratch",
    )
    toolkits = Toolkits(session=session, now=datetime.now(UTC), quarantine_days=7)
    return asyncio.run(
        apply(
            queue,
            repository=repository,
            roster=Roster.of(queue_backend),
            library=library,
            notes="",
            surfaces=surfaces,
            trees_dir=tmp_path / "fixes",
            tasks_dir=tmp_path / "tasks",
            budget=Budget(seconds=30, steps=20),
            toolkits=toolkits,
            ledger=Ledger(RunBudget(max_parallel=2, tokens=1_000_000)),
            run="run-test",
        )
    )


def fixer(
    *, edit: bool = True, verify: tuple[str, ...] | None = VERIFY, outcome: str = "fixed"
) -> FakeBackend:
    """A scripted fix session: it edits the worktree and runs verification through the toolkit."""

    def act(brief: Brief) -> None:
        if edit:
            brief.toolkit.call(
                "edit_file",
                {"path": "pyproject.toml", "find": "2.31.0", "replace": "2.32.4"},
            )
        if verify is not None:
            brief.toolkit.call("run_command", {"command": list(verify)})

    return FakeBackend(
        default=Scripted(result={"outcome": outcome, "notes": "Moved the pin to 2.32.4."}),
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


def pinned(repo: Path, branch: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "show", f"{branch}:pyproject.toml"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def test_a_verified_fix_lands_on_its_own_branch_and_leaves_no_worktree(
    library: Library, overlay: Overlay, git_repo: Path, tmp_path: Path
) -> None:
    write_pin(git_repo)
    fixes = _apply(
        fixer(),
        library=library,
        overlay=overlay,
        git_repo=git_repo,
        tmp_path=tmp_path,
        findings=(judged(),),
    )
    fix = fixes[0]
    assert fix.outcome is FixOutcome.FIXED
    assert fix.branch == branch_for(judged())
    assert fix.branch in branches(git_repo)
    assert "2.32.4" in pinned(git_repo, fix.branch)
    assert fix.verification is not None and fix.verification.passed
    # The tree under maintenance is untouched: the change exists only on the fix branch.
    assert "2.31.0" in (git_repo / "pyproject.toml").read_text(encoding="utf-8")
    assert not (tmp_path / "fixes" / fix.job.task.id).exists()
    message = subprocess.run(
        ["git", "-C", str(git_repo), "log", "-1", "--format=%B", fix.branch],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert judged().finding.key in message
    assert "run-test" in message


def test_a_fix_nobody_verified_does_not_ship(
    library: Library, overlay: Overlay, git_repo: Path, tmp_path: Path
) -> None:
    """The claim is not the evidence: `fixed` without a verification call is refused."""
    write_pin(git_repo)
    fix = _apply(
        fixer(verify=None),
        library=library,
        overlay=overlay,
        git_repo=git_repo,
        tmp_path=tmp_path,
        findings=(judged(),),
    )[0]
    assert fix.outcome is FixOutcome.REFUSED
    assert "none of the overlay's verification commands ran" in fix.detail
    assert fix.branch == ""
    assert branch_for(judged()) not in branches(git_repo)


def test_half_a_verification_surface_does_not_ship(
    library: Library, overlay: Overlay, git_repo: Path, tmp_path: Path
) -> None:
    """A surface counts whole or not at all.

    The rule this replaces was "at least one command ran", and its first live run showed why that is
    not a rule: three sessions made the same change, two ran the cheapest command of a five-command
    surface and shipped, one ran all five, hit a failure and refused. The gate was deciding nothing
    except how thorough each session happened to feel.
    """
    write_pin(git_repo)
    fix = _apply(
        fixer(),
        library=library,
        overlay=overlay,
        git_repo=git_repo,
        tmp_path=tmp_path,
        findings=(judged(),),
        surfaces=PAIRED,
    )[0]
    assert fix.outcome is FixOutcome.REFUSED
    assert "no verification surface was run in full" in fix.detail
    assert "uv cache dir" in fix.detail
    assert branch_for(judged()) not in branches(git_repo)


def test_a_fix_task_is_handed_its_group_and_the_exact_commands(
    library: Library, overlay: Overlay, git_repo: Path, tmp_path: Path
) -> None:
    """What to run is not left to be discovered in the overlay: the agent already knows it."""
    write_pin(git_repo)
    prompts: list[str] = []

    def act(brief: Brief) -> None:
        prompts.append(brief.prompt)
        brief.toolkit.call(
            "edit_file", {"path": "pyproject.toml", "find": "2.31.0", "replace": "2.32.4"}
        )
        brief.toolkit.call("run_command", {"command": list(VERIFY)})

    backend = FakeBackend(
        default=Scripted(result={"outcome": "fixed", "notes": "Moved the pin."}), on_execute=act
    )
    fixes = _apply(
        backend,
        library=library,
        overlay=overlay,
        git_repo=git_repo,
        tmp_path=tmp_path,
        findings=(judged(advisory="PYSEC-A"), judged(advisory="PYSEC-B")),
    )
    assert len(fixes) == 1
    prompt = prompts[0]
    assert "PYSEC-A" in prompt and "PYSEC-B" in prompt
    assert "`uv --version`" in prompt and "`uv --no-such-option`" in prompt
    assert "only in full" in prompt
    message = subprocess.run(
        ["git", "-C", str(git_repo), "log", "-1", "--format=%B", fixes[0].branch],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "PYSEC-A" in message and "PYSEC-B" in message


def test_a_failure_that_predates_the_change_is_named_as_such(
    library: Library, overlay: Overlay, git_repo: Path, tmp_path: Path
) -> None:
    """A red surface still blocks the fix, but the team is told whose failure it is.

    Without this, a repository whose lint or tests were already failing would produce a week of
    refusals reading "verification failed", and the obvious conclusion — the agent cannot bump our
    dependencies — would be the wrong one.
    """
    write_pin(git_repo)
    fix = _apply(
        fixer(verify=BROKEN),
        library=library,
        overlay=overlay,
        git_repo=git_repo,
        tmp_path=tmp_path,
        findings=(judged(),),
    )[0]
    assert fix.outcome is FixOutcome.REFUSED
    assert fix.verification is not None
    assert fix.verification.pre_existing == (BROKEN,)
    assert fix.verification.blocked_by_base
    assert "predate this change" in fix.detail
    assert branch_for(judged()) not in branches(git_repo)


def test_a_failure_the_change_caused_stays_the_change_s_own(
    library: Library, overlay: Overlay, git_repo: Path, tmp_path: Path
) -> None:
    """The other half of the same question: this command is green on the base and red here."""
    write_pin(git_repo)

    def act(brief: Brief) -> None:
        brief.toolkit.call(
            "edit_file", {"path": "pyproject.toml", "find": "2.31.0", "replace": "2.32.4"}
        )
        brief.toolkit.call("run_command", {"command": list(ONLY_RED_WHEN_CHANGED)})

    backend = FakeBackend(
        default=Scripted(result={"outcome": "fixed", "notes": "Moved the pin."}), on_execute=act
    )
    fix = _apply(
        backend,
        library=library,
        overlay=overlay,
        git_repo=git_repo,
        tmp_path=tmp_path,
        findings=(judged(),),
        surfaces={"python-uv": (ONLY_RED_WHEN_CHANGED,)},
        binaries=frozenset({"uv", "python3"}),
    )[0]
    assert fix.outcome is FixOutcome.REFUSED
    assert fix.verification is not None and fix.verification.pre_existing == ()
    assert not fix.verification.blocked_by_base
    assert "verification failed" in fix.detail
    assert "predate" not in fix.detail


def test_a_fix_that_changed_nothing_does_not_ship(
    library: Library, overlay: Overlay, git_repo: Path, tmp_path: Path
) -> None:
    """A session that concluded it was done without touching the tree has fixed nothing."""
    write_pin(git_repo)
    fix = _apply(
        fixer(edit=False),
        library=library,
        overlay=overlay,
        git_repo=git_repo,
        tmp_path=tmp_path,
        findings=(judged(),),
    )[0]
    assert fix.outcome is FixOutcome.REFUSED
    assert "worktree is unchanged" in fix.detail


def test_a_refusal_keeps_its_reason_and_leaves_no_branch(
    library: Library, overlay: Overlay, git_repo: Path, tmp_path: Path
) -> None:
    write_pin(git_repo)
    fix = _apply(
        fixer(outcome="refused"),
        library=library,
        overlay=overlay,
        git_repo=git_repo,
        tmp_path=tmp_path,
        findings=(judged(),),
    )[0]
    assert fix.outcome is FixOutcome.REFUSED
    assert fix.notes == "Moved the pin to 2.32.4."
    assert fix.branch == ""
    assert branch_for(judged()) not in branches(git_repo)


def test_a_fix_task_edits_its_worktree_and_cannot_leave_it(
    library: Library, overlay: Overlay, git_repo: Path, tmp_path: Path
) -> None:
    """Everything the task reads and writes is the copy, including a path trying to escape."""
    write_pin(git_repo)
    offered: set[str] = set()
    refused = ""

    def act(brief: Brief) -> None:
        nonlocal refused
        offered.update(tool.name for tool in brief.toolkit.tools())
        try:
            brief.toolkit.call(
                "edit_file",
                {"path": "../README.md", "find": "product", "replace": "owned"},
            )
        except Exception as error:
            refused = str(error)
        brief.toolkit.call(
            "edit_file", {"path": "pyproject.toml", "find": "2.31.0", "replace": "2.32.4"}
        )
        brief.toolkit.call("run_command", {"command": list(VERIFY)})

    backend = FakeBackend(
        default=Scripted(result={"outcome": "fixed", "notes": "Moved the pin."}), on_execute=act
    )
    fix = _apply(
        backend,
        library=library,
        overlay=overlay,
        git_repo=git_repo,
        tmp_path=tmp_path,
        findings=(judged(),),
    )[0]
    assert fix.outcome is FixOutcome.FIXED
    assert "outside the repository" in refused
    assert "edit_file" in offered
    # No `read_change`: a maintenance run has no change to scope itself to, and no git tool either.
    assert not {"read_change", "git_ops", "scm_write"} & offered
    assert (git_repo / "README.md").read_text(encoding="utf-8") == "product\n"


def test_the_cost_of_a_run_includes_what_the_fixes_spent(
    library: Library, overlay: Overlay, git_repo: Path, tmp_path: Path
) -> None:
    """A cost figure that leaves out the fix sessions is a number a team would budget with.

    The first live maintenance run reported one accounted session while the ledger, which admits
    everything it is told about, had four — the fixes were two thirds of the spend and invisible.
    """
    write_pin(git_repo)
    manifest = Manifest(
        run_id="run-test",
        agent_version="test",
        trigger=Trigger.MAINTAIN_SCHEDULED,
        playbook="playbooks/maintain",
        repository=str(git_repo),
        head="HEAD",
        change=None,
        library={},
        overlay={},
        started_at=datetime.now(UTC).isoformat(),
    )
    manifest.models.append(
        {"task": "deps-vuln", "attempt": 1, "usage": {"known": True, "total_tokens": 500}}
    )
    fixes = _apply(
        fixer(),
        library=library,
        overlay=overlay,
        git_repo=git_repo,
        tmp_path=tmp_path,
        findings=(judged(),),
    )
    _account(manifest, fixes)
    assert manifest.cost["sessions"] == 2
    assert manifest.cost["accounted_sessions"] == 2
    assert manifest.cost["tokens"] == 1500


def test_an_analysis_task_has_no_way_to_write(
    library: Library, overlay: Overlay, git_repo: Path, tmp_path: Path
) -> None:
    """No worktree, no `edit_file`: an analyst cannot modify the tree it is judging."""
    repository = Repository.open(git_repo)
    session = Session(
        repository=repository.path,
        grants=Grants(binaries=frozenset({"uv"}), hosts=frozenset()),
        cache=FactCache(tmp_path / "cache", writable=False),
        scratch_root=tmp_path / "scratch",
    )
    toolkit = Toolkits(session=session, now=datetime.now(UTC), quarantine_days=7).for_task(
        PlannedTask(
            id="deps-vuln", capability="capabilities/deps-vuln", role=Role.ANALYST, required=True
        )
    )
    assert "edit_file" not in {tool.name for tool in toolkit.tools()}


def write_pin(repo: Path) -> None:
    path = repo / "pyproject.toml"
    path.write_text('[project]\ndependencies = ["requests==2.31.0"]\n', encoding="utf-8")
    (repo / "uv.lock").write_text("version = 1\n", encoding="utf-8")
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
        ["git", "-C", str(repo), "commit", "--quiet", "-m", "pin requests"], check=True, env=env
    )


def test_a_dry_run_says_what_it_would_fix_and_creates_nothing(
    git_repo: Path, library_root: Path, overlay_root: Path, config_dir: Path, tmp_path: Path
) -> None:
    """The whole point of a dry run: nothing is created, and no fixer session is even started."""
    write_pin(git_repo)
    run_dir = tmp_path / "runs"
    main(
        [
            "maintain",
            "--repo",
            str(git_repo),
            "--library",
            str(library_root),
            "--overlay",
            str(overlay_root),
            "--run-dir",
            str(run_dir),
            "--config-dir",
            str(config_dir),
            "--dry-run",
        ]
    )
    manifest = json.loads(next(run_dir.glob("*/manifest.json")).read_text(encoding="utf-8"))
    assert manifest["fixes"] == []
    assert manifest["remediation"] == {}
    assert [role["role"] for role in manifest["roles"]] == ["analyst"]
    assert not any(name.startswith("agent/") for name in branches(git_repo))
