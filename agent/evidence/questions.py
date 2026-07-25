"""The vocabulary of questions a run may ask about a subject.

Fixed rather than free-form, and this is not tidiness. A question name is half of an evidence key,
so a task that invented "pip-audit vulnerabilities in jinja2 3.1.3" produced a record no cache could
ever hit, no later run could recognise, and no reader could compare with last week's answer to the
same question. The value of a fact store is that the same question has the same name.

Adding a question is a change to the agent, deliberately: the set of things the run knows how to
establish is part of what the run can promise, not something a model decides mid-task.
"""

from __future__ import annotations

from enum import StrEnum


class Question(StrEnum):
    DECLARED_PIN = "declared-pin"
    """What a manifest asks for, as written."""

    RESOLVED_VERSION = "resolved-version"
    """What the lock actually resolved to."""

    LATEST_VERSION = "latest-version"
    AVAILABLE_VERSIONS = "available-versions"
    PUBLISH_TIME = "publish-time"
    ARTIFACT_DIGEST = "artifact-digest"
    ADVISORIES = "advisories"

    REACHABILITY = "reachability"
    """Whether the affected code is actually used here, which decides how urgent an advisory is."""

    CHANGED_LINES = "changed-lines"
    """What the change itself added or removed in a file, which is what a review judges."""

    HOLD = "hold"
    """A stated reason not to move something, found in the repository rather than assumed."""

    BUNDLE = "bundle"
    """Packages a product requires to move together."""

    CODE_OBSERVATION = "code-observation"
    """Something read in the source that a finding about the code rests on."""

    CONFIG_OBSERVATION = "config-observation"
    """The same, for workflow and configuration files."""

    TOOL_OUTPUT = "tool-output"
    """A scanner's answer that is not one of the questions above, kept honest by naming the tool in
    `source` rather than by inventing a question name."""


CACHEABLE = frozenset({Question.PUBLISH_TIME, Question.ARTIFACT_DIGEST})
"""Questions whose answer, once true, stays true. Everything else is re-established every run: a
cached advisory list is how a weekly run stops noticing new advisories."""
