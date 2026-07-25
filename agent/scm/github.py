"""GitHub, through its own command-line client.

`gh` rather than raw HTTP, for reasons that are all about not reimplementing something that exists:
it already knows how to read a token from the environment, retry a rate-limited call, follow
pagination and speak GraphQL — and thread resolution has no REST endpoint at all, so GraphQL is not
optional. The cost is a binary dependency, which CI runners ship and the ceiling already permits.

The token is never in an argument, only in the environment `gh` inherits, so a command line that
ends up in a log or a manifest cannot leak it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Self

from agent.repo import Repository
from agent.scm import marker
from agent.scm.port import Change, NewThread, Review, ScmError, Stance, Thread

CLIENT = "gh"
TIMEOUT_SECONDS = 60
THREAD_PAGE = 50

_REMOTE = re.compile(
    r"^(?:git@github\.com:|ssh://git@github\.com/|https://(?:[^@/]+@)?github\.com/)"
    r"(?P<owner>[^/]+)/(?P<name>[^/]+?)(?:\.git)?/?$"
)

_EVENTS = {
    Stance.APPROVE: "APPROVE",
    Stance.REQUEST_CHANGES: "REQUEST_CHANGES",
    Stance.COMMENT: "COMMENT",
}

_THREADS = f"""\
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {{
  repository(owner: $owner, name: $name) {{
    pullRequest(number: $number) {{
      reviewThreads(first: {THREAD_PAGE}, after: $cursor) {{
        pageInfo {{ hasNextPage endCursor }}
        nodes {{
          id
          isResolved
          comments(first: 1) {{ nodes {{ databaseId body }} }}
        }}
      }}
    }}
  }}
}}"""

_RESOLVE = """\
mutation($id: ID!) {
  resolveReviewThread(input: { threadId: $id }) { thread { isResolved } }
}"""

_UNRESOLVE = """\
mutation($id: ID!) {
  unresolveReviewThread(input: { threadId: $id }) { thread { isResolved } }
}"""


@dataclass(frozen=True, slots=True)
class GitHub:
    owner: str
    repository: str

    @property
    def name(self) -> str:
        return f"github:{self.slug}"

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repository}"

    @classmethod
    def at(cls, url: str) -> Self:
        """The repository a remote URL names, in any of the forms git writes it."""
        found = _REMOTE.match(url.strip())
        if found is None:
            raise ScmError(f"not a GitHub remote: {url}")
        return cls(owner=found.group("owner"), repository=found.group("name"))

    @classmethod
    def of(cls, repository: Repository, *, remote: str = "origin") -> Self:
        """Read the target from the checkout's own remote, never from configuration.

        A repository slug in a config file is one that will eventually disagree with the checkout,
        and the failure would be a review posted on a different repository's pull request.
        """
        if shutil.which(CLIENT) is None:
            raise ScmError(
                f"{CLIENT} is not installed, so nothing can be published. Install it, or run "
                "without --publish and read the report instead"
            )
        return cls.at(repository.remote(remote))

    def change(self, number: int) -> Change:
        got = self._api(f"repos/{self.slug}/pulls/{number}")
        head = got.get("head") or {}
        user = got.get("user") or {}
        return Change(
            number=number,
            head=str(head.get("sha", "")),
            author=str(user.get("login", "")),
            draft=bool(got.get("draft", False)),
        )

    def threads(self, number: int) -> tuple[Thread, ...]:
        """Every thread whose first comment carries a marker, resolved ones included.

        Resolved threads are returned rather than filtered out because they answer a question the
        caller has: a finding that came back needs its old thread updated, not a second thread next
        to a resolved one saying the same thing.
        """
        found: list[Thread] = []
        cursor: str | None = None
        while True:
            page = self._graphql(
                _THREADS,
                {"owner": self.owner, "name": self.repository, "number": number},
                cursor=cursor,
            )
            block = (page.get("data", {}).get("repository", {}).get("pullRequest", {}) or {}).get(
                "reviewThreads", {}
            )
            for node in block.get("nodes") or []:
                comments = (node.get("comments") or {}).get("nodes") or []
                if not comments:
                    continue
                body = str(comments[0].get("body", ""))
                key = marker.read(body)
                if not key:
                    continue
                found.append(
                    Thread(
                        id=str(node.get("id", "")),
                        comment=str(comments[0].get("databaseId", "")),
                        key=key,
                        body=body,
                        number=number,
                        resolved=bool(node.get("isResolved", False)),
                    )
                )
            info = block.get("pageInfo") or {}
            if not info.get("hasNextPage"):
                return tuple(found)
            cursor = str(info.get("endCursor"))

    def review(
        self, number: int, *, body: str, stance: Stance, head: str, threads: Sequence[NewThread]
    ) -> Review:
        """One review, carrying its new threads, on the commit the run actually analysed.

        `commit_id` is stated rather than left to default. Without it the platform attaches the
        review to whatever is newest, which on a pull request that moved mid-run means lines the run
        never read.
        """
        payload: dict[str, Any] = {
            "commit_id": head,
            "body": body,
            "event": _EVENTS[stance],
            "comments": [
                {"path": item.path, "line": item.line, "side": "RIGHT", "body": item.body}
                for item in threads
            ],
        }
        try:
            got = self._api(
                f"repos/{self.slug}/pulls/{number}/reviews", method="POST", body=payload
            )
        except ScmError as error:
            if stance is Stance.COMMENT or not _own_change(error):
                raise
            # GitHub lets nobody review their own pull request, approving or otherwise, and a run on
            # a change the agent opened is exactly that. The decision is not lost: the same text
            # arrives as a comment, and merge authority was never the review event anyway.
            payload["event"] = _EVENTS[Stance.COMMENT]
            got = self._api(
                f"repos/{self.slug}/pulls/{number}/reviews", method="POST", body=payload
            )
            stance = Stance.COMMENT
        return Review(reference=str(got.get("html_url") or got.get("id") or ""), stance=stance)

    def edit(self, thread: Thread, body: str) -> None:
        self._api(
            f"repos/{self.slug}/pulls/comments/{thread.comment}",
            method="PATCH",
            body={"body": body},
        )

    def reply(self, thread: Thread, note: str) -> None:
        self._api(
            f"repos/{self.slug}/pulls/{thread.number}/comments/{thread.comment}/replies",
            method="POST",
            body={"body": note},
        )

    def resolve(self, thread: Thread) -> None:
        self._graphql(_RESOLVE, {"id": thread.id})

    def unresolve(self, thread: Thread) -> None:
        self._graphql(_UNRESOLVE, {"id": thread.id})

    def _graphql(
        self, query: str, variables: dict[str, Any], *, cursor: str | None = None
    ) -> dict[str, Any]:
        arguments = ["graphql", "-f", f"query={query}"]
        for name, value in variables.items():
            arguments += ["-F", f"{name}={value}"]
        if cursor is not None:
            arguments += ["-F", f"cursor={cursor}"]
        got = self._run(arguments)
        errors = got.get("errors")
        if errors:
            detail = "; ".join(str(item.get("message", item)) for item in errors)
            raise ScmError(f"GraphQL refused the request: {detail}")
        return got

    def _api(
        self, path: str, *, method: str = "GET", body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        arguments = [path, "--method", method]
        if body is not None:
            arguments += ["--input", "-"]
        return self._run(arguments, stdin=json.dumps(body) if body is not None else None)

    def _run(self, arguments: list[str], *, stdin: str | None = None) -> dict[str, Any]:
        located = shutil.which(CLIENT)
        if located is None:
            raise ScmError(f"{CLIENT} is not installed")
        try:
            finished = subprocess.run(  # noqa: S603 - fixed binary, no shell, arguments are ours
                [located, "api", *arguments],
                input=stdin,
                capture_output=True,
                text=True,
                timeout=TIMEOUT_SECONDS,
                check=False,
                env=_environment(),
            )
        except subprocess.TimeoutExpired:
            raise ScmError(f"{CLIENT} api {arguments[0]} timed out") from None
        if finished.returncode != 0:
            raise ScmError(f"{CLIENT} api {arguments[0]} failed: {_complaint(finished)}")
        if not finished.stdout.strip():
            return {}
        try:
            got = json.loads(finished.stdout)
        except json.JSONDecodeError:
            raise ScmError(f"{CLIENT} api {arguments[0]} did not answer with JSON") from None
        return got if isinstance(got, dict) else {"data": got}


def _complaint(finished: subprocess.CompletedProcess[str]) -> str:
    """What GitHub actually objected to.

    The client prints its own summary — "Unprocessable Entity (HTTP 422)" — on the error stream and
    leaves the response body, which holds the reason, on the output stream. Reading only the former
    is how "you cannot request changes on your own pull request" turns into a status code nobody can
    act on, and the fallback that exists for exactly that case never fires.
    """
    detail: list[str] = []
    try:
        body = json.loads(finished.stdout)
    except json.JSONDecodeError:
        body = None
    if isinstance(body, dict):
        message = body.get("message")
        if isinstance(message, str):
            detail.append(message)
        errors = body.get("errors")
        if isinstance(errors, list):
            detail += [
                str(item.get("message", item)) if isinstance(item, dict) else str(item)
                for item in errors
            ]
    if not detail:
        detail = [line.strip() for line in finished.stderr.splitlines() if line.strip()][-2:]
    return "; ".join(detail) or "no reason given"


def _own_change(error: ScmError) -> bool:
    return "own pull request" in str(error).lower()


def _environment() -> dict[str, str]:
    """The environment `gh` needs, and not a token this code passes around itself.

    Inherited wholesale rather than assembled: `gh` reads GH_TOKEN, GITHUB_TOKEN, a host
    configuration file and a credential helper, and a hand-built environment would silently support
    only the one the author happened to test with.
    """
    return dict(os.environ)
