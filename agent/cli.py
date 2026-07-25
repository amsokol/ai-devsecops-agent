"""Command line: one command with subcommands, usable in CI and locally.

Nothing here is interactive. A run happens in CI, so a prompt for confirmation would be a
configuration error rather than a pause.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from agent import __version__
from agent.domain import Trigger
from agent.errors import AgentError, ExitCode
from agent.library import Library
from agent.manifest import read_manifest
from agent.orchestrator import REPORT, Request, RunRecord, run
from agent.scm.port import Identity

DEFAULT_OVERLAY = ".devsecops"
DEFAULT_RUN_DIR = ".agent/runs"
DEFAULT_LIBRARY = "library"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent", description=__doc__.splitlines()[0])
    parser.add_argument("--version", action="version", version=__version__)
    subcommands = parser.add_subparsers(dest="command", required=True)

    review = subcommands.add_parser("review", help="review a proposed change and produce a verdict")
    _add_common(review)
    review.add_argument("--change", type=int, help="change request number in the hosting platform")
    review.add_argument("--base", default="main", help="branch the change is proposed against")
    review.add_argument(
        "--trigger",
        choices=[Trigger.CHANGE_OPENED.value, Trigger.CHANGE_UPDATED.value],
        default=Trigger.CHANGE_OPENED.value,
    )
    review.add_argument(
        "--publish",
        action="store_true",
        help=(
            "post the decision on the change: one review body, one thread per finding, and threads "
            "of fixed findings resolved. Needs --change and a credential the client can read"
        ),
    )

    maintain = subcommands.add_parser("maintain", help="maintain the default branch")
    _add_common(maintain)
    maintain.add_argument("--wake-issue", type=int, help="issue whose comment woke this run")
    maintain.add_argument(
        "--actor",
        default="",
        help=(
            "login whose comment woke this run. A bot's comment, or the agent's own account, ends "
            "the run before it spends anything: an agent that answers itself answers forever"
        ),
    )
    maintain.add_argument(
        "--scheduled",
        action="store_true",
        help="this run came from a schedule, so the restraint rules for unattended runs apply",
    )
    maintain.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "analyse and say which findings would be fixed, without touching the repository: no "
            "worktree, no branch, no commit"
        ),
    )
    maintain.add_argument(
        "--publish",
        action="store_true",
        help=(
            "track findings as issues: one per finding, brought up to date rather than duplicated, "
            "and closed with evidence when the check that owns it ran clean and found nothing"
        ),
    )

    explain = subcommands.add_parser("explain", help="show a recorded run")
    explain.add_argument("--run", required=True, help="run identifier")
    explain.add_argument("--run-dir", type=Path, default=Path(DEFAULT_RUN_DIR))

    pin = subcommands.add_parser(
        "pin",
        help="print the version and digest of a library, in the form the pin file expects",
    )
    pin.add_argument("--library", type=Path, default=Path(DEFAULT_LIBRARY))

    return parser


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="target repository")
    parser.add_argument(
        "--library",
        type=Path,
        default=Path(DEFAULT_LIBRARY),
        help="knowledge library artefact, unpacked",
    )
    parser.add_argument(
        "--overlay",
        type=Path,
        default=None,
        help=f"product overlay directory (default: <repo>/{DEFAULT_OVERLAY})",
    )
    parser.add_argument("--run-dir", type=Path, default=Path(DEFAULT_RUN_DIR))
    parser.add_argument("--config-dir", type=Path, default=None, help="replace built-in config")
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="build and print the plan without executing tasks; claims nothing about the code",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="ignore the cache of immutable facts, to prove a verdict reproduces without it",
    )
    parser.add_argument(
        "--only",
        action="append",
        metavar="TASK",
        help=(
            "run only this planned task, repeatable. For development: the verdict then covers the "
            "named tasks and nothing else, and the run says so"
        ),
    )
    parser.add_argument(
        "--json", action="store_true", help="print the manifest instead of a summary"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "explain":
            print(json.dumps(read_manifest(arguments.run_dir, arguments.run), indent=2))
            return int(ExitCode.OK)
        if arguments.command == "pin":
            # One implementation of the digest, in the code that verifies it. A second one on the
            # library side would eventually disagree, and then nobody could say which was right.
            library = Library.load(arguments.library, agent_version=__version__)
            print(f"version: {library.identity.version}")
            print(f"digest: {library.digest}")
            return int(ExitCode.OK)
        record = run(_request(arguments))
    except AgentError as error:
        print(f"error: {error}", file=sys.stderr)
        return int(error.exit_code)
    if arguments.json:
        print(json.dumps(record.manifest.as_json(), indent=2, ensure_ascii=False))
    else:
        _print_summary(record)
    return int(record.exit_code)


def _request(arguments: argparse.Namespace) -> Request:
    trigger = (
        _maintenance_trigger(arguments)
        if arguments.command == "maintain"
        else Trigger(arguments.trigger)
    )
    repository = arguments.repo.resolve()
    return Request(
        trigger=trigger,
        repository=repository,
        library_path=arguments.library,
        overlay_path=arguments.overlay or repository / DEFAULT_OVERLAY,
        run_dir=arguments.run_dir,
        config_dir=arguments.config_dir,
        base=getattr(arguments, "base", None),
        change=getattr(arguments, "change", None),
        wake_issue=getattr(arguments, "wake_issue", None),
        actor=getattr(arguments, "actor", "") or "",
        plan_only=arguments.plan_only,
        dry_run=getattr(arguments, "dry_run", False),
        publish=getattr(arguments, "publish", False),
        use_cache=not arguments.no_cache,
        only=tuple(arguments.only or ()),
    )


def _maintenance_trigger(arguments: argparse.Namespace) -> Trigger:
    """A schedule, somebody's comment, or somebody asking directly — in that order of precedence.

    A wake is its own trigger rather than a flag on a request because everything downstream reads
    it: a person is waiting, so it gets the interactive budget, and whose comment it was decides
    whether the run should happen at all.
    """
    if arguments.scheduled:
        return Trigger.MAINTAIN_SCHEDULED
    if arguments.wake_issue is not None:
        return Trigger.HUMAN_COMMENT
    return Trigger.MAINTAIN_REQUESTED


def _print_actions(actions: dict[str, Any]) -> None:
    """What the run did on the platform, including the threads it deliberately left as they were.

    "Unchanged" is worth a line: the whole promise of publishing by finding key is that a rerun does
    not comment again, and a summary that only listed writes could not show it kept the promise.
    """
    if not actions:
        return
    identity = actions.get("identity")
    who = Identity(**identity).description if isinstance(identity, dict) else "an unknown account"
    for part in ("review", "issues", "changes"):
        block = actions.get(part)
        if not isinstance(block, dict):
            continue
        for item in block.get("posted") or []:
            detail = f"  {item['detail']}" if item.get("detail") else ""
            print(f"  {item['what']:<10} {item['key']}{detail}")
    review = actions.get("review")
    if isinstance(review, dict) and review.get("published"):
        print(f"published {review.get('stance')} as {who}  {review.get('reference')}")
    tracked = actions.get("issues")
    if isinstance(tracked, dict):
        print(f"issues    {tracked['raised']} raised, {tracked['closed']} closed, as {who}")
    opened = actions.get("changes")
    if isinstance(opened, dict):
        print(f"changes   {opened['opened']} proposed, as {who}")


def _print_summary(record: RunRecord) -> None:
    manifest = record.manifest
    print(f"run {manifest.run_id}  {manifest.playbook}  trigger {manifest.trigger}")
    print(f"library {manifest.library['version']} ({manifest.library['digest'][:19]}…)")
    for task in manifest.tasks:
        state = task.outcome.value if task.outcome else "planned"
        reason = f" ({task.reason.value})" if task.reason else ""
        print(f"  task {task.id:<28} {state}{reason}  scope: {len(task.scope)} path(s)")
    for entry in manifest.skipped:
        print(f"  n/a  {entry['capability']:<28} {entry['reason']}")
    for warning in manifest.warnings:
        print(f"  warning: {warning}")
    for finding in manifest.findings:
        marker = "block" if finding["action"] == "block" else "note "
        print(f"  {marker} {finding['severity']:<8} {finding['key']}")
    for fix in manifest.fixes:
        where = fix["branch"] or fix["detail"] or fix["outcome"]
        print(f"  {fix['outcome']:<10} {fix['finding']}\n             {where}")
    for entry in manifest.remediation.get("deferred", []):
        print(f"  deferred   {entry['finding']}\n             {entry['reason']}")
    _print_actions(manifest.actions)
    if manifest.cost.get("known"):
        print(f"cost {manifest.cost['tokens']} tokens over {manifest.cost['sessions']} session(s)")
    print(f"result {manifest.result}  exit {int(record.exit_code)}")
    print(f"manifest {record.manifest_path}")
    if record.report:
        print(f"report {record.manifest_path.parent / REPORT}")


if __name__ == "__main__":
    raise SystemExit(main())
