"""Mint an installation token for the agent's GitHub App, for use in a live check by hand.

CI does not need this: `actions/create-github-app-token` does the same thing in one step. A laptop
has no such step, and the alternative is what this whole change exists to prevent — publishing under
the account the machine is logged in as.

    AGENT_GITHUB_TOKEN="$(uv run python scripts/app_token.py --app-id 123 --key ~/agent.pem \
        --repo amsokol/ai-devsecops-agent)" uv run python scripts/live_publish_check.py ...

Signing is done by `openssl`, which every machine that has `git` also has, rather than by adding a
cryptography dependency to a project that needs none at runtime. The token it prints lasts an hour
and speaks for the App: keep it in a variable, out of shell history and out of logs.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

CLAIM_SECONDS = 540
"""GitHub refuses a JWT that claims more than ten minutes. Nine leaves room for a slow clock."""

BACKDATE_SECONDS = 60
"""Issued slightly in the past, because a laptop clock a few seconds ahead is otherwise a rejection
with a message about the future that nobody reads as "your clock is fast"."""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-id", required=True, help="the App's numeric id, from its settings")
    parser.add_argument(
        "--key", required=True, type=Path, help="the App's private key, a .pem file"
    )
    parser.add_argument("--repo", required=True, help="owner/name of a repository the App is on")
    arguments = parser.parse_args()

    for binary in ("openssl", "gh"):
        if shutil.which(binary) is None:
            print(f"{binary} is not installed", file=sys.stderr)
            return 1
    if not arguments.key.is_file():
        print(f"no private key at {arguments.key}", file=sys.stderr)
        return 1

    claim = _jwt(arguments.app_id, arguments.key)
    installed = _api(claim, f"repos/{arguments.repo}/installation")
    if "id" not in installed:
        print(
            f"the App is not installed on {arguments.repo}: install it there first",
            file=sys.stderr,
        )
        return 1
    granted = _api(claim, f"app/installations/{installed['id']}/access_tokens", method="POST")
    token = granted.get("token")
    if not token:
        print("the installation granted no token", file=sys.stderr)
        return 1
    print(token)
    return 0


def _jwt(app_id: str, key: Path) -> str:
    """A short-lived assertion that this is the App, signed with the App's own key."""
    now = int(time.time())
    header = _segment({"alg": "RS256", "typ": "JWT"})
    payload = _segment({"iat": now - BACKDATE_SECONDS, "exp": now + CLAIM_SECONDS, "iss": app_id})
    signed = f"{header}.{payload}"
    signature = subprocess.run(  # noqa: S603 - fixed binary, no shell, arguments are ours
        ["openssl", "dgst", "-sha256", "-sign", str(key)],  # noqa: S607 - resolved above
        input=signed.encode(),
        capture_output=True,
        check=True,
    ).stdout
    return f"{signed}.{_encode(signature)}"


def _segment(claim: dict[str, object]) -> str:
    return _encode(json.dumps(claim, separators=(",", ":")).encode())


def _encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _api(claim: str, path: str, *, method: str = "GET") -> dict[str, object]:
    """Asked through `gh`, with the assertion in place of a token.

    The client sends whatever `GH_TOKEN` holds as a bearer credential, which is exactly what these
    two endpoints want — so the App's own endpoints need no second HTTP client.
    """
    finished = subprocess.run(  # noqa: S603 - fixed binary, no shell, arguments are ours
        ["gh", "api", path, "--method", method],  # noqa: S607 - resolved in main
        capture_output=True,
        text=True,
        check=False,
        env=dict(os.environ) | {"GH_TOKEN": claim},
    )
    if finished.returncode != 0:
        print(finished.stderr.strip() or finished.stdout.strip(), file=sys.stderr)
        return {}
    got = json.loads(finished.stdout)
    return got if isinstance(got, dict) else {}


if __name__ == "__main__":
    raise SystemExit(main())
