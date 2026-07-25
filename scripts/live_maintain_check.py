"""Drive the maintenance side of the GitHub adapter: a pushed branch, a change request, an issue.

Not a test: it writes on a hosting platform, so it is run by hand and never from CI. The unit tests
prove the reconciliation and the ordering; only a real platform can say whether the push
authenticates as the agent, whether the change request is recorded as the agent's rather than as the
person who started the run, and whether a label the repository has never seen comes into being.

    uv run python scripts/live_maintain_check.py --repo /path/to/worktree

It creates a scratch branch with one throwaway file, pushes it, proposes it, raises an issue, closes
that issue with a note, stores and re-reads the agent's own memory ref, then puts everything back:
the change request closed, the branch gone locally and remotely. Pass --keep to read it in a
browser.

The memory ref is here for one question a unit test cannot answer: `refs/agent/state` is neither a
branch nor a tag, and whether a platform lets an App push such a ref at all is the platform's own
decision. If it refuses, every scheduled run would warn that it cannot remember what keeps failing.

Whose token is in the environment decides who owns all of it. The account is printed before anything
is written.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import time
from pathlib import Path
from secrets import token_hex

from agent.errors import ConfigError
from agent.repo import Repository, Worktree
from agent.scm import GitHub, ScmError, marker
from agent.scm.port import NewChange, NewIssue
from agent.state import Memory

LABEL = "agent"
REF = "refs/agent/state"
KEY = "capabilities/live-check:scratch"
SCRATCH = "LIVE-CHECK.md"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--keep", action="store_true", help="leave the change request and branch")
    arguments = parser.parse_args()

    repository = Repository.open(arguments.repo)
    try:
        platform = GitHub.of(repository)
    except ScmError as error:
        print(f"nothing can be written: {error}")
        return 1
    identity = platform.identity()
    print(f"platform {platform.name} {platform.slug}")
    print(f"token    {platform.credential.variable}")
    print(f"identity {identity.description}")
    print(f"base     {repository.branch}")

    branch = f"agent/security/live-check-{token_hex(3)}"
    tree = _prepare(repository, branch)
    print(f"\n== branch\n   {branch} at {tree[:12]}")

    try:
        platform.push(repository.path, source=f"refs/heads/{branch}", target=f"refs/heads/{branch}")
    except ScmError as error:
        print(f"   push failed: {error}")
        _forget(repository, branch)
        return 1
    print("   pushed")

    opened = platform.propose(
        NewChange(
            head=branch,
            base=repository.branch,
            title="agent: live check for the maintenance path",
            body=marker.stamp(
                "Opened by `scripts/live_maintain_check.py` to check that a branch the agent "
                "prepared is pushed and proposed under the agent's own account. Nothing here is a "
                "real finding, and the script closes it again.",
                KEY,
            ),
        )
    )
    print(f"\n== change request\n   #{opened.number} by {opened.author or '?'}  {opened.reference}")

    raised = platform.raise_issue(
        NewIssue(
            key=KEY,
            title="agent: live check — scratch",
            body=marker.stamp(
                "Raised by `scripts/live_maintain_check.py`. Not a real finding; closed below.", KEY
            ),
        ),
        label=LABEL,
    )
    print(f"\n== issue\n   #{raised.number}  {raised.reference}")
    print(f"   read back by key after {_settle(platform, raised.number)}")

    platform.note(raised, "Closing: this was a live check of the adapter, not a finding.")
    platform.close_issue(raised)
    print("   closed with a note")

    _remember(repository, platform)

    if arguments.keep:
        print(f"\nleft open: #{opened.number} and branch {branch}")
        return 0
    _gh(platform, ["pr", "close", str(opened.number), "--comment", "Live check finished."])
    _gh(platform, ["api", "-X", "DELETE", f"repos/{platform.slug}/git/refs/heads/{branch}"])
    _forget(repository, branch)
    print(f"\ncleaned up: #{opened.number} closed, {branch} removed here and there")
    return 0


def _remember(repository: Repository, platform: GitHub) -> None:
    """Store a throwaway document in the memory ref, read it back, and leave the ref where it was.

    Left where it was rather than deleted: a repository that is already running the agent has real
    counters there, and a check that wipes them would cost a week of escalation. A scratch key is
    added and then the previous document is written back, so the only trace is two commits in a ref
    nobody reads.
    """
    memory = Memory(repository=repository, ref=REF)
    before = memory.read()
    print(f"\n== memory\n   {REF} holds {len(before)} key(s) before this")
    scratch = dict(before) | {"live_check": token_hex(4)}
    stored, failure = memory.write(scratch, platform=platform, run="live-check")
    if failure:
        print(f"   not stored: {failure}")
        return
    read_back = memory.read()
    print(f"   stored {stored}, read back {read_back == scratch}")
    memory.write(before, platform=platform, run="live-check-restore")
    print(f"   restored to {len(memory.read())} key(s)")


def _settle(platform: GitHub, number: int, *, limit: int = 20) -> str:
    """How long the label listing takes to admit an issue that was just created.

    Worth measuring rather than assuming: this listing is a secondary index and it lagged five
    seconds in the first live run of this path, which read as a bug in the marker. A run is
    unaffected because it reads the listing once before writing, which is the reason for that order.
    """
    for second in range(limit):
        found = [item for item in platform.issues(label=LABEL) if item.key == KEY]
        if any(item.number == number for item in found):
            return f"{second}s"
        time.sleep(1)
    return f"never, within {limit}s"


def _prepare(repository: Repository, branch: str) -> str:
    """A worktree with one throwaway file on it, committed exactly as a fix task's work would be."""
    tree = Worktree.create(repository, branch=branch, at=repository.path / ".agent" / "live-check")
    (tree.path / SCRATCH).write_text(
        "Scratch file from the maintenance live check. Safe to delete.\n", encoding="utf-8"
    )
    commit = tree.commit(
        "security: scratch change for the live maintenance check\n\n"
        "Written by scripts/live_maintain_check.py; not a real fix.\n"
    )
    tree.discard(keep_branch=True)
    return commit


def _forget(repository: Repository, branch: str) -> None:
    for command in (
        ["worktree", "remove", "--force", str(repository.path / ".agent" / "live-check")],
        ["branch", "--delete", "--force", branch],
    ):
        located = shutil.which("git")
        if located is None:
            return
        subprocess.run(  # noqa: S603 - fixed binary, no shell, arguments are ours
            [located, "-C", str(repository.path), *command], capture_output=True, check=False
        )


def _gh(platform: GitHub, arguments: list[str]) -> None:
    """Cleanup only. Closing a change request and deleting a ref are not the agent's to do."""
    located = shutil.which("gh")
    if located is None:
        raise ConfigError("gh is not available on PATH")
    if arguments[0] == "pr":
        arguments = [*arguments, "--repo", platform.slug]
    finished = subprocess.run(  # noqa: S603 - fixed binary, no shell, arguments are ours
        [located, *arguments],
        capture_output=True,
        text=True,
        check=False,
        env=dict(os.environ) | {"GH_TOKEN": platform.credential.token},
    )
    if finished.returncode != 0:
        print(f"   cleanup step failed: {finished.stderr.strip() or finished.stdout.strip()}")


if __name__ == "__main__":
    raise SystemExit(main())
