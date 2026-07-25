"""How the agent recognises its own comment a week later.

The contract promises that a repeated run updates a thread instead of opening a second one, and that
promise needs an anchor the platform keeps and a human never has to look at. An HTML comment holding
the finding key is that anchor: invisible where the comment is rendered, unchanged when a line
moves, and readable in the raw body by anyone debugging why a thread was reused.

Matching by the key rather than by anything the platform assigns is deliberate. Comment identifiers
change when a thread is recreated, review identifiers change every run, and a line number changes
whenever somebody edits the file above it. The key is the only thing that does not move.
"""

from __future__ import annotations

import re

PREFIX = "<!-- agent:key="
SUFFIX = " -->"
_MARKER = re.compile(r"<!--\s*agent:key=(?P<key>[^>]*?)\s*-->")


def render(key: str) -> str:
    return f"{PREFIX}{key}{SUFFIX}"


def stamp(body: str, key: str) -> str:
    """A body with its marker, at the end where it stays out of the way of a quoted diff."""
    return f"{body.rstrip()}\n\n{render(key)}\n"


def read(body: str) -> str:
    """The key a body claims, or an empty string when there is no marker.

    An absent marker means the comment is not the agent's to touch. Guessing — by author, by a
    phrase, by position — is how an agent ends up editing a human's comment.
    """
    found = _MARKER.search(body)
    return found.group("key").strip() if found else ""
