"""`http_get`: a GET to an allowlisted host, with no credentials attached.

Redirects are followed only while they stay on the allowlist, because a redirect off it is exactly
how an allowlist gets bypassed. No mutating verb exists: changes to the hosting platform happen
through the action layer, never from a task.
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

    def get(self, url: str) -> Response:
        self._check(url)
        request = urllib.request.Request(  # noqa: S310 - scheme is validated in _check
            url, method="GET", headers={"User-Agent": USER_AGENT, "Accept": "*/*"}
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
        return super().redirect_request(req, fp, code, msg, headers, newurl)


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
