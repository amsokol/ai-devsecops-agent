"""Which backend answers for which role.

Deliberately a lookup and nothing more: the choice is a configuration decision, and an unknown name
is a startup error rather than a silent fallback to whatever happens to be installed.

A backend is created when a role first needs one, not when the run starts. A configuration may bind
a role to an SDK this machine has not installed; as long as no task takes that role, that costs
nothing and says nothing — which is what lets one configuration serve a laptop and a pipeline.
"""

from __future__ import annotations

from agent.backends.fake import FakeBackend
from agent.backends.port import Backend
from agent.config import Binding, Models
from agent.domain import Role
from agent.errors import ConfigError


def make_backend(binding: Binding) -> Backend:
    match binding.backend:
        case "cursor":
            try:
                from agent.backends.cursor import CursorBackend
            except ImportError as error:
                raise ConfigError(
                    f"the cursor backend needs the cursor-sdk package: {error}. Install the agent "
                    "with its 'cursor' extra, or bind the role to another backend in models.yaml"
                ) from None
            return CursorBackend(model=binding.model, sandbox=binding.sandbox)
        case "fake":
            # Shipped, not hidden behind a test-only import: a run that used it says so in the
            # manifest, which is how a report from a fake session is recognisable as one.
            return FakeBackend()
        case other:
            raise ConfigError(f"unknown backend {other!r} bound to {binding.role.value!r}")


class Roster:
    """The backends a run actually uses, one per role, created on first use and closed together.

    Cached by backend and model rather than by role: two roles on the same pair share one session
    factory, because opening a second connection to the same SDK buys nothing.
    """

    def __init__(self, models: Models, *, single: Backend | None = None) -> None:
        self._models = models
        self._single = single
        self._made: dict[tuple[str, str], Backend] = {}
        self._used: dict[Role, Binding] = {}

    @classmethod
    def of(cls, backend: Backend) -> Roster:
        """A roster where every role is answered by one given backend.

        For tests and the eval harness, which care about what the core does with a session's answer
        rather than about which SDK produced it.
        """
        return cls(Models(bindings={}), single=backend)

    def for_role(self, role: Role) -> Backend:
        if self._single is not None:
            return self._single
        binding = self._models.for_role(role)
        self._used[role] = binding
        key = (binding.backend, binding.model)
        if key not in self._made:
            self._made[key] = make_backend(binding)
        return self._made[key]

    def prepare(self, roles: set[Role]) -> None:
        for role in sorted(roles):
            self.for_role(role)

    def used(self) -> list[dict[str, str]]:
        """The pairs this run actually reached, for the manifest."""
        return [self._used[role].as_json() for role in sorted(self._used)]

    async def close(self) -> None:
        for backend in self._made.values():
            await backend.close()
        self._made.clear()
        if self._single is not None:
            await self._single.close()
