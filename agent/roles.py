"""What each role needs from whatever backend runs it.

These requirements live in code rather than in the configuration on purpose. A role's needs follow
from what its code does — an analyst that cannot call the tool registry has nothing to establish a
fact with — so they are not a knob. Mapping a role to a backend and a model *is* a decision, and
that part is configuration.

The check itself happens at startup. A `fixer` bound to an adapter that cannot modify files must
fail before the run opens issues and branches, not halfway through a maintenance run that then
reports it changed nothing.
"""

from __future__ import annotations

from enum import StrEnum

from agent.domain import Role


class Ability(StrEnum):
    """Something an adapter either implements or does not. Only stated where it is known."""

    TOOLS = "tools"
    """Can expose the agent's tool registry to a session."""

    WRITES = "writes"
    """Can let a session modify files in the workspace it was given."""

    STRUCTURED_OUTPUT = "structured-output"
    """Can constrain a session's answer to a schema.

    Recorded rather than required: results arrive as a file the core validates, so no role depends
    on this. Adapters still declare it, because an eval comparing two SDKs needs to know.
    """

    TOKEN_ACCOUNTING = "token-accounting"  # noqa: S105 - an ability, not a credential
    """Reports what a session spent. Without it the run's token ceiling cannot bind."""


NEEDS: dict[Role, frozenset[Ability]] = {
    Role.INTENT: frozenset(),
    Role.ANALYST: frozenset({Ability.TOOLS}),
    Role.FIXER: frozenset({Ability.TOOLS, Ability.WRITES}),
    Role.WRITER: frozenset(),
}
"""What a role cannot work without.

`intent` and `writer` need nothing special: one classifies a short text, the other formulates one,
and both answer in a file like everybody else. `analyst` needs the registry, because a finding must
rest on a call. `fixer` needs both: it establishes what is wrong and then changes it.
"""


def needs(role: Role) -> frozenset[Ability]:
    return NEEDS[role]
