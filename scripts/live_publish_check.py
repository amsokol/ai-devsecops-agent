"""Drive the GitHub adapter against a real pull request, one reconciliation state at a time.

Not a test: it writes on a hosting platform, so it is run by hand against a scratch change and never
from CI. It exists because the unit tests prove the reconciliation logic while saying nothing about
whether GitHub accepts what the adapter sends — the shape of a review payload, which errors mean
"you cannot review your own change", and the fact that resolving a thread lives only in GraphQL.

    uv run python scripts/live_publish_check.py --repo /path/to/worktree --change 12 --path file.md

It publishes, then reruns, rewords, clears and brings back the same finding, printing what each pass
did. Read the pull request afterwards: one thread, one marker, five states.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path

from agent.domain import Outcome, RunResult
from agent.evidence import Reliability, Subject
from agent.findings import Action, Finding, Klass, Location, Severity
from agent.publish import Publication, publish_review
from agent.repo import ChangeView, Repository
from agent.scm import GitHub
from agent.verdict import Judged, TaskOutcome, Verdict

CAPABILITY = "capabilities/deps-vuln"


def finding(path: str, line: int, *, remediation: str) -> Finding:
    return Finding(
        capability=CAPABILITY,
        klass=Klass.SECURITY,
        severity=Severity.HIGH,
        subject=Subject(ecosystem="ecosystems/python-uv", package="scratch", version="0.0.1"),
        summary="scratch 0.0.1 is affected by SCRATCH-0 (live check, not a real advisory)",
        rationale="Published by scripts/live_publish_check.py to exercise the GitHub adapter.",
        remediation=remediation,
        advisory="SCRATCH-0",
        location=Location(path=path, line=line),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--change", type=int, required=True)
    parser.add_argument("--base", default="main")
    parser.add_argument("--path", required=True, help="a file this change touches")
    parser.add_argument("--line", type=int, default=1, help="a line this change adds")
    arguments = parser.parse_args()

    repository = Repository.open(arguments.repo)
    platform = GitHub.of(repository)
    change = ChangeView.of(repository, arguments.base)
    proposed = platform.change(arguments.change)
    print(f"platform {platform.name}")
    print(f"change   {proposed.as_json()}")
    print(f"head     {repository.head}")

    first = finding(arguments.path, arguments.line, remediation="Move the pin to 0.0.2.")
    reworded = replace(first, remediation="Move the pin to 0.0.3.")
    blocking = Judged(
        finding=first, action=Action.BLOCK, reliability=Reliability.REPRODUCIBLE, capped=False
    )
    revised = replace(blocking, finding=reworded)
    found = TaskOutcome(
        id="deps-vuln", capability=CAPABILITY, required=True, outcome=Outcome.FINDINGS
    )
    clean = replace(found, outcome=Outcome.CLEAN)

    def publish(label: str, verdict: Verdict, outcome: TaskOutcome) -> Publication:
        record = publish_review(
            platform,
            number=arguments.change,
            verdict=verdict,
            report=f"## Live check — {label}\n\nPublished by the adapter's live check.\n",
            head=repository.head,
            outcomes=(outcome,),
            change=change,
        )
        print(f"\n== {label}")
        print(f"   published {record.published} stance {record.stance} {record.reference}")
        for item in record.posted:
            print(f"   {item.what:<10} {item.key} {item.detail}")
        if record.withheld or record.failure:
            print(f"   withheld {record.withheld!r} failure {record.failure!r}")
        return record

    blocked = Verdict(result=RunResult.BLOCKED, judged=(blocking,), blocking=(blocking,))
    publish("first pass: a new thread", blocked, found)
    publish("second pass: nothing to say", blocked, found)
    publish(
        "third pass: the wording changed",
        Verdict(result=RunResult.BLOCKED, judged=(revised,), blocking=(revised,)),
        found,
    )
    publish("fourth pass: fixed and clean", Verdict(result=RunResult.PASS), clean)
    publish("fifth pass: it came back", blocked, found)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
