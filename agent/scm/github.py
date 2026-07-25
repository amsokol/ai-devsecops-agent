"""GitHub, through its own command-line client.

`gh` rather than raw HTTP, for reasons that are all about not reimplementing something that exists:
it already knows how to retry a rate-limited call, follow pagination and speak GraphQL — and thread
resolution has no REST endpoint at all, so GraphQL is not optional. The cost is a binary dependency,
which CI runners ship and the ceiling already permits.

The one thing `gh` is *not* allowed to decide is who the agent is. Left alone it falls back to
whatever account the machine is logged in as, and the first live check published a machine's review
under a person's name. The token is therefore resolved here, from named variables, and passed in the
environment — never in an argument, so a command line in a log cannot leak it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

from agent.errors import ConfigError
from agent.repo import Repository
from agent.scm import marker
from agent.scm.port import (
    Change,
    Identity,
    Issue,
    NewChange,
    NewIssue,
    NewThread,
    Proposal,
    Review,
    ScmError,
    Stance,
    Thread,
)

CLIENT = "gh"
TIMEOUT_SECONDS = 60
THREAD_PAGE = 50
PAGE = 100
MAX_PAGES = 20

TOKEN_VARIABLES = ("AGENT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")
"""Where the credential is read from, in order. `AGENT_GITHUB_TOKEN` comes first so a machine that
also has a developer's `GH_TOKEN` publishes as the agent; the other two follow the client's own
precedence, so nobody has to learn a second rule. A stored login is deliberately not among them."""

ACTIONS_IDENTITY = "github-actions[bot]"

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
class Credential:
    """A token and the variable it came from. The variable is safe to record; the token is not."""

    token: str
    variable: str


def credential(environment: Mapping[str, str] | None = None) -> Credential:
    """The agent's own token, or a refusal to publish at all.

    Deliberately not "whatever the client can find". `gh` falls back to the account stored on the
    machine, and on a laptop that is the developer: this adapter's first live check posted five
    machine-written reviews under a person's name. Two things are wrong with that beyond the name.
    The decision reads as a colleague's opinion, so nobody can tell judgement from tooling. And a
    workflow that wakes on human comments and filters bots cannot filter a human account — the
    agent's own comment wakes the agent, which comments, which wakes it again.
    """
    found = environment if environment is not None else os.environ
    for variable in TOKEN_VARIABLES:
        token = found.get(variable, "").strip()
        if token:
            return Credential(token=token, variable=variable)
    named = ", ".join(TOKEN_VARIABLES)
    raise ScmError(
        f"no credential for the agent in the environment ({named}). Publishing under whatever "
        "account this machine is logged in as would sign a machine's review with a person's name; "
        "in CI, pass the workflow's GITHUB_TOKEN"
    )


@dataclass(frozen=True, slots=True)
class GitHub:
    owner: str
    repository: str
    credential: Credential
    _labelled: set[str] = field(default_factory=set, compare=False, repr=False)
    """Labels this instance has already made sure of, so one run asks once."""

    @property
    def name(self) -> str:
        return f"github:{self.slug}"

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repository}"

    @classmethod
    def at(cls, url: str, *, token: Credential | None = None) -> Self:
        """The repository a remote URL names, in any of the forms git writes it."""
        found = _REMOTE.match(url.strip())
        if found is None:
            raise ScmError(f"not a GitHub remote: {url}")
        return cls(
            owner=found.group("owner"),
            repository=found.group("name"),
            credential=token or credential(),
        )

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
        try:
            url = repository.remote(remote)
        except ConfigError as error:
            # Reported as a platform problem rather than a configuration one, because by the time
            # this is asked the run has already paid for its analysis. A checkout with no remote
            # costs the comments, not the verdict.
            raise ScmError(f"this checkout has no remote named {remote}: {error}") from None
        return cls.at(url)

    def identity(self) -> Identity:
        """Whose account this token speaks for, asked rather than assumed.

        An installation token — a workflow's default, and an App's — is refused by `/user` with
        "resource not accessible by integration", and that refusal is the answer: only an
        integration is told that, so the caller is a machine. Its name is another matter. A
        workflow's own token is `github-actions[bot]` and can be named here; an App's bot is named
        after the App, which this side cannot ask about, so the name is left for the platform to
        state on the first thing published rather than guessed at now.
        """
        try:
            got = self._api("user")
        except ScmError as refusal:
            return self._integration(refusal)
        login = str(got.get("login", ""))
        return Identity(login=login, bot=got.get("type") == "Bot" or login.endswith("[bot]"))

    def _integration(self, refusal: ScmError) -> Identity:
        if "not accessible by integration" not in str(refusal).lower():
            return Identity(login="", bot=False, known=False)
        workflow = (
            self.credential.variable == "GITHUB_TOKEN"
            and os.environ.get("GITHUB_ACTIONS") == "true"
        )
        return Identity(login=ACTIONS_IDENTITY if workflow else "", bot=True)

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
        wrote = got.get("user")
        return Review(
            reference=str(got.get("html_url") or got.get("id") or ""),
            stance=stance,
            author=str(wrote.get("login", "")) if isinstance(wrote, dict) else "",
        )

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

    def issues(self, *, label: str) -> tuple[Issue, ...]:
        """Open issues that carry both the label and a marker.

        Pull requests are dropped: this endpoint answers with both, and a change request treated
        as a tracked finding would be edited and closed as one. The marker is what proves
        authorship — anyone can apply a label, and the agent must not close a human's issue.
        """
        found: list[Issue] = []
        for item in self._paged(f"repos/{self.slug}/issues", query=f"state=open&labels={label}"):
            if "pull_request" in item:
                continue
            body = str(item.get("body") or "")
            key = marker.read(body)
            if not key:
                continue
            found.append(
                Issue(
                    number=int(item.get("number", 0)),
                    key=key,
                    title=str(item.get("title") or ""),
                    body=body,
                    reference=str(item.get("html_url") or ""),
                )
            )
        return tuple(found)

    def raise_issue(self, new: NewIssue, *, label: str) -> Issue:
        self._ensure_label(label)
        got = self._api(
            f"repos/{self.slug}/issues",
            method="POST",
            body={"title": new.title, "body": new.body, "labels": [label]},
        )
        return Issue(
            number=int(got.get("number", 0)),
            key=new.key,
            title=new.title,
            body=new.body,
            reference=str(got.get("html_url") or ""),
        )

    def edit_issue(self, issue: Issue, body: str) -> None:
        self._api(f"repos/{self.slug}/issues/{issue.number}", method="PATCH", body={"body": body})

    def note(self, issue: Issue, body: str) -> None:
        self._api(
            f"repos/{self.slug}/issues/{issue.number}/comments", method="POST", body={"body": body}
        )

    def close_issue(self, issue: Issue) -> None:
        self._api(
            f"repos/{self.slug}/issues/{issue.number}",
            method="PATCH",
            body={"state": "closed", "state_reason": "completed"},
        )

    def proposals(self, *, prefix: str) -> tuple[Proposal, ...]:
        found: list[Proposal] = []
        for item in self._paged(f"repos/{self.slug}/pulls", query="state=open"):
            head = item.get("head")
            reference = str(head.get("ref") or "") if isinstance(head, dict) else ""
            if not reference.startswith(prefix):
                continue
            found.append(
                Proposal(
                    number=int(item.get("number", 0)),
                    head=reference,
                    reference=str(item.get("html_url") or ""),
                )
            )
        return tuple(found)

    def push(self, path: Path, branch: str) -> None:
        """Send the branch over HTTPS with the run's own credential, and never force.

        The token reaches git through a credential helper that reads it from the environment, so it
        appears in no command line: an argument is visible to every process on the machine, and a
        token in CI output is a token that has to be rotated. The configured helpers are cleared
        first, because a machine with a keychain would otherwise answer with a developer's login —
        the very substitution this whole path exists to prevent.

        The URL is built rather than taken from the remote: a checkout cloned over SSH would
        authenticate with somebody's key and push as them.
        """
        located = shutil.which("git")
        if located is None:
            raise ScmError("git is not available on PATH, so nothing can be pushed")
        helper = 'f() { echo username=x-access-token; echo "password=$AGENT_PUSH_TOKEN"; }; f'
        finished = subprocess.run(  # noqa: S603 - fixed binary, no shell, arguments are ours
            [
                located,
                "-C",
                str(path),
                "-c",
                "credential.helper=",
                "-c",
                f"credential.helper=!{helper}",
                "push",
                f"https://github.com/{self.slug}.git",
                f"refs/heads/{branch}:refs/heads/{branch}",
            ],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
            env=dict(os.environ) | {"AGENT_PUSH_TOKEN": self.credential.token},
        )
        if finished.returncode != 0:
            detail = finished.stderr.strip().splitlines()[-1:] or ["no reason given"]
            raise ScmError(f"pushing {branch} failed: {detail[0]}")

    def propose(self, new: NewChange) -> Proposal:
        got = self._api(
            f"repos/{self.slug}/pulls",
            method="POST",
            body={
                "title": new.title,
                "body": new.body,
                "head": new.head,
                "base": new.base,
            },
        )
        return Proposal(
            number=int(got.get("number", 0)),
            head=new.head,
            reference=str(got.get("html_url") or ""),
        )

    def _ensure_label(self, label: str) -> None:
        """Create the label once per run, and treat "it already exists" as success.

        Asked for rather than assumed, because a label the repository does not have is dropped
        silently when an issue is created — and the whole reconciliation then reads an empty list
        next week and opens every issue again.
        """
        if label in self._labelled:
            return
        self._labelled.add(label)
        try:
            self._api(
                f"repos/{self.slug}/labels",
                method="POST",
                body={
                    "name": label,
                    "color": "ededed",
                    "description": "Raised by the DevSecOps agent",
                },
            )
        except ScmError as error:
            if "already_exists" not in str(error).lower():
                raise

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
                env=self._environment(),
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

    def _paged(self, path: str, *, query: str) -> list[dict[str, Any]]:
        """Every page of a listing, because the first page is not the answer.

        A repository with more open findings than one page holds would otherwise look, to the
        reconciliation, like one where the rest were fixed: they are absent from the list, so they
        get raised again as duplicates. The page cap guards against a listing that never shortens
        rather than limiting anything anyone should reach.
        """
        items: list[dict[str, Any]] = []
        for page in range(1, MAX_PAGES + 1):
            got = self._api(f"{path}?{query}&per_page={PAGE}&page={page}")
            block = got.get("data")
            if not isinstance(block, list) or not block:
                return items
            items += [item for item in block if isinstance(item, dict)]
            if len(block) < PAGE:
                return items
        return items

    def _environment(self) -> dict[str, str]:
        """The client's environment, with the identity question already settled.

        The token is stated rather than left to be found. `gh` prefers `GH_TOKEN` over everything
        else, so setting it is also what takes the machine's stored login out of the picture: the
        credential the run resolved is the credential the call uses, on a laptop and in CI alike.
        """
        return dict(os.environ) | {"GH_TOKEN": self.credential.token}


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
