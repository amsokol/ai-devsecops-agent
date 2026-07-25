"""`http_get`: a GET to an allowlisted host.

Redirects are followed only while they stay on the allowlist, because a redirect off it is exactly
how an allowlist gets bypassed. No mutating verb exists: changes to the hosting platform happen
through the action layer, never from a task.

One credential may travel: the hosting platform's own read token, on requests to the platform's own
hosts, because anonymous access there is rate-limited to the point where the facts a decision needs
go missing. It is attached here rather than handed to anything: no session sees it, no command's
environment carries it, and a redirect that changes host drops it before following.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass
from http.client import HTTPResponse
from typing import Any
from urllib.parse import urlparse

from agent.tools.ceiling import Grants

MAX_BODY_BYTES = 2_000_000
DEFAULT_TIMEOUT_SECONDS = 20
USER_AGENT = "ai-devsecops-agent"
AUTHORIZATION = "Authorization"

BEARING_HOSTS = frozenset({"api.github.com", "github.com", "raw.githubusercontent.com"})
"""Hosts a platform read token may be sent to. Deliberately a fixed list rather than every granted
host: a registry has no business receiving the agent's credential, and neither has a redirect."""


class HostNotPermitted(Exception):
    """The host is not granted for this run, or a redirect tried to leave the allowlist."""


@dataclass(frozen=True, slots=True)
class Response:
    url: str
    status: int
    headers: dict[str, str]
    body: str
    truncated: bool


@dataclass(frozen=True, slots=True)
class HttpClient:
    grants: Grants
    timeout: int = DEFAULT_TIMEOUT_SECONDS
    token: str = ""
    """The hosting platform's read credential, for `BEARING_HOSTS` only. Empty means anonymous."""

    def get(self, url: str) -> Response:
        self._check(url)
        headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
        if self.token and (urlparse(url).hostname or "").lower() in BEARING_HOSTS:
            headers[AUTHORIZATION] = f"Bearer {self.token}"
        request = urllib.request.Request(  # noqa: S310 - scheme is validated in _check
            url, method="GET", headers=headers
        )
        opener = urllib.request.build_opener(_GuardedRedirects(self.grants))
        with opener.open(request, timeout=self.timeout) as response:
            return _read(response)

    def _check(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https":
            raise HostNotPermitted(f"{url!r} is not https")
        host = (parsed.hostname or "").lower()
        if not self.grants.allows_host(host):
            raise HostNotPermitted(
                f"host {host!r} is not permitted for this run. Hosts are declared by ecosystem "
                "documents and granted within the agent's ceiling."
            )


class _GuardedRedirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, grants: Grants) -> None:
        self.grants = grants

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        parsed = urlparse(newurl)
        if parsed.scheme != "https" or not self.grants.allows_host((parsed.hostname or "").lower()):
            raise HostNotPermitted(f"redirect to {newurl!r} leaves the allowlist")
        following = super().redirect_request(req, fp, code, msg, headers, newurl)
        if following is not None and (parsed.hostname or "").lower() != (req.host or "").lower():
            # A redirect within the allowlist is still a different host, and the credential was
            # granted to one of them. urllib copies headers onto the new request, so this is where
            # the copy has to be undone.
            following.remove_header(AUTHORIZATION)
        return following


def _read(response: HTTPResponse) -> Response:
    data = response.read(MAX_BODY_BYTES + 1)
    truncated = len(data) > MAX_BODY_BYTES
    return Response(
        url=response.geturl(),
        status=response.status,
        headers={key.lower(): value for key, value in response.getheaders()},
        body=data[:MAX_BODY_BYTES].decode("utf-8", errors="replace"),
        truncated=truncated,
    )
