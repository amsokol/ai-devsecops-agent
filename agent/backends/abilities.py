"""What each adapter can do, declared by name and without importing its SDK.

Kept apart from the adapters themselves so that compatibility can be checked while reading the
configuration, on a machine where the optional SDK is not installed. Binding a role to a backend is
then either accepted or refused at startup; nothing is discovered mid-run.

An ability is stated only where it is known. There is no entry for prompt caching: what the Cursor
SDK does about it has not been measured here, and a boolean invented for a table looks exactly like
one that was verified.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent.roles import Ability


@dataclass(frozen=True, slots=True)
class Abilities:
    name: str
    has: frozenset[Ability]

    def missing(self, required: frozenset[Ability]) -> tuple[Ability, ...]:
        return tuple(sorted(required - self.has))

    def as_json(self) -> dict[str, object]:
        return {"backend": self.name, "abilities": sorted(ability.value for ability in self.has)}


CURSOR = Abilities(
    name="cursor",
    has=frozenset({Ability.TOOLS, Ability.TOKEN_ACCOUNTING}),
)
"""The Cursor adapter as it stands.

No `structured-output`: results arrive as a file the core validates, so the adapter has never needed
to constrain an answer to a schema and does not claim it can.
"""

FAKE = Abilities(
    name="fake",
    has=frozenset({Ability.TOOLS, Ability.STRUCTURED_OUTPUT, Ability.TOKEN_ACCOUNTING}),
)
"""The scripted backend claims everything, because standing in for any backend is its whole purpose.

What it cannot fake is being mistaken for real: every session it runs is recorded as `fake` in the
manifest, which is how a report produced without a model is recognisable as one.
"""

ABILITIES: dict[str, Abilities] = {CURSOR.name: CURSOR, FAKE.name: FAKE}
