"""Binding a role to a backend and a model, and refusing a binding that cannot work.

The pairs come from the product's overlay: the agent ships no model, in code or in configuration. So
these tests build the choices a product would write and check what the agent does with them.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from agent.backends import FakeBackend
from agent.backends.abilities import ABILITIES, CURSOR, Abilities
from agent.backends.select import Roster
from agent.config import BUILTIN_CONFIG_DIR, Config, Models
from agent.domain import Role
from agent.errors import ConfigError
from agent.overlay import Choice
from agent.roles import NEEDS, Ability

WHERE = "agent.yaml (models)"


def chosen(pairs: dict[Role, Choice], options: dict[str, object] | None = None) -> Models:
    return Models.chosen(pairs, options=options or {}, where=WHERE)


def test_the_shipped_configuration_names_no_model_anywhere() -> None:
    """The property the whole arrangement rests on, asserted rather than trusted.

    A model named in the agent — in code or in a file it ships — makes switching provider a fork of
    the agent, and makes the agent decide what somebody else's run costs. Comments may discuss
    models; keys may not name one.
    """
    config = Config.load()
    assert set(config.backend_options) <= set(ABILITIES)
    for settings in config.backend_options.values():
        assert "model" not in settings
    for path in sorted(BUILTIN_CONFIG_DIR.rglob("*.yaml")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            key = line.split("#", 1)[0].strip().split(":", 1)[0]
            assert key not in {"model", "models"}, f"{path}:{number} names a model"


def test_a_pair_from_an_overlay_is_checked_against_what_the_adapter_can_do() -> None:
    models = chosen({Role.ANALYST: Choice(backend="cursor", model="composer-2.5")})
    binding = models.for_role(Role.ANALYST)
    assert (binding.backend, binding.model) == ("cursor", "composer-2.5")
    assert not ABILITIES[binding.backend].missing(NEEDS[binding.role])


def test_every_declared_ability_is_one_a_role_or_an_eval_can_ask_about() -> None:
    """A backend claiming an ability nobody names is a table entry drifting from the code."""
    for abilities in ABILITIES.values():
        assert abilities.has <= set(Ability)


def test_a_role_on_a_backend_that_cannot_do_its_work_is_refused_at_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The point of the check: a run that spends its budget before discovering the mismatch.

    No shipped adapter is missing `tools` today, so the incompatible backend is introduced here. The
    check is worth proving anyway: it is what stands between a typo in an overlay and a maintenance
    run that reports findings and silently ships nothing.
    """
    toolless = Abilities(name="toolless", has=frozenset({Ability.TOKEN_ACCOUNTING}))
    monkeypatch.setitem(ABILITIES, toolless.name, toolless)
    with pytest.raises(ConfigError) as error:
        chosen({Role.FIXER: Choice(backend="toolless", model="none")})
    assert "tools" in str(error.value)
    assert "fixer" in str(error.value)


def test_mutation_is_a_tool_of_ours_rather_than_an_ability_of_a_backend() -> None:
    """A `fixer` needs the registry and nothing more, because `edit_file` lives in the registry.

    Declaring a `writes` ability per adapter looked like a real gate and was not: every backend that
    can expose our tools can expose the one that edits a worktree.
    """
    assert NEEDS[Role.FIXER] == frozenset({Ability.TOOLS})
    assert not CURSOR.missing(NEEDS[Role.FIXER])


def test_an_unknown_backend_is_an_error_naming_the_file_that_chose_it() -> None:
    with pytest.raises(ConfigError) as error:
        chosen({Role.ANALYST: Choice(backend="claude", model="sonnet")})
    assert "unknown backend 'claude'" in str(error.value)
    assert WHERE in str(error.value)


def test_a_model_without_a_backend_is_not_an_address() -> None:
    with pytest.raises(ConfigError, match="needs both a backend and a model"):
        chosen({Role.ANALYST: Choice(backend="", model="composer-2.5")})


def test_a_role_nobody_bound_says_so_instead_of_guessing() -> None:
    models = chosen({Role.ANALYST: Choice(backend="fake", model="none")})
    with pytest.raises(ConfigError) as error:
        models.for_role(Role.WRITER)
    assert "'writer'" in str(error.value)
    assert "analyst" in str(error.value)


def test_backend_settings_come_from_the_agent_and_not_from_the_product() -> None:
    """A product chooses what it pays for; how tightly that runs is the agent's business."""
    loosened = chosen(
        {Role.ANALYST: Choice(backend="cursor", model="composer-2.5")},
        {"cursor": {"sandbox": False}},
    )
    assert loosened.for_role(Role.ANALYST).sandbox is False
    shipped = Config.load().backend_options
    assert shipped["cursor"]["sandbox"] is True


def test_two_roles_on_the_same_pair_share_one_backend(tmp_path: Path) -> None:
    models = chosen(
        {
            Role.ANALYST: Choice(backend="fake", model="none"),
            Role.WRITER: Choice(backend="fake", model="none"),
        }
    )
    roster = Roster(models)
    assert roster.for_role(Role.ANALYST) is roster.for_role(Role.WRITER)
    assert roster.used() == [
        {"role": "analyst", "backend": "fake", "model": "none"},
        {"role": "writer", "backend": "fake", "model": "none"},
    ]


def test_one_given_backend_answers_for_every_role() -> None:
    """How tests and the eval harness run the core without naming an SDK."""
    backend = FakeBackend()
    roster = Roster.of(backend)
    assert roster.for_role(Role.ANALYST) is backend
    assert roster.for_role(Role.FIXER) is backend
