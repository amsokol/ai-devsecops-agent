"""Binding a role to a backend and a model, and refusing a binding that cannot work."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from agent.backends import FakeBackend
from agent.backends.abilities import ABILITIES, CURSOR
from agent.backends.select import Roster
from agent.config import BUILTIN_CONFIG_DIR, Config
from agent.domain import Role
from agent.errors import ConfigError
from agent.roles import NEEDS, Ability


def config_with(models: str, tmp_path: Path) -> Config:
    directory = tmp_path / "config"
    shutil.copytree(BUILTIN_CONFIG_DIR, directory, dirs_exist_ok=True)
    (directory / "models.yaml").write_text(models, encoding="utf-8")
    return Config.load(directory)


def test_the_shipped_configuration_binds_the_analyst_and_nothing_it_cannot_run() -> None:
    """What a release enforces, asserted without a --config-dir standing in for it."""
    models = Config.load().models
    assert models.for_role(Role.ANALYST).backend == "cursor"
    for binding in models.bindings.values():
        abilities = ABILITIES[binding.backend]
        assert not abilities.missing(NEEDS[binding.role])


def test_every_declared_ability_is_one_a_role_or_an_eval_can_ask_about() -> None:
    """A backend claiming an ability nobody names is a table entry drifting from the code."""
    for abilities in ABILITIES.values():
        assert abilities.has <= set(Ability)


def test_a_fixer_on_a_backend_that_cannot_write_is_refused_at_startup(tmp_path: Path) -> None:
    """The point of the check: a maintenance run that opens issues and then changes nothing."""
    assert Ability.WRITES not in CURSOR.has
    with pytest.raises(ConfigError) as error:
        config_with("roles:\n  fixer:\n    backend: cursor\n    model: composer-2.5\n", tmp_path)
    assert "writes" in str(error.value)
    assert "fixer" in str(error.value)


def test_an_unknown_role_or_backend_is_a_startup_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="unknown role 'reviewer'"):
        config_with("roles:\n  reviewer:\n    backend: fake\n    model: none\n", tmp_path)
    with pytest.raises(ConfigError, match="unknown backend 'claude'"):
        config_with("roles:\n  analyst:\n    backend: claude\n    model: sonnet\n", tmp_path)


def test_a_model_without_a_backend_is_not_an_address(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="needs both a backend and a model"):
        config_with("roles:\n  analyst:\n    model: composer-2.5\n", tmp_path)


def test_a_configuration_that_binds_nobody_could_run_no_task(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="no role is bound"):
        config_with("roles: {}\n", tmp_path)


def test_a_role_nobody_bound_says_so_instead_of_guessing(tmp_path: Path) -> None:
    models = config_with(
        "roles:\n  analyst:\n    backend: fake\n    model: none\n", tmp_path
    ).models
    with pytest.raises(ConfigError) as error:
        models.for_role(Role.WRITER)
    assert "'writer'" in str(error.value)
    assert "analyst" in str(error.value)


def test_backend_options_reach_the_adapter(tmp_path: Path) -> None:
    models = config_with(
        "roles:\n  analyst:\n    backend: cursor\n    model: composer-2.5\n"
        "backends:\n  cursor:\n    sandbox: false\n",
        tmp_path,
    ).models
    assert models.for_role(Role.ANALYST).sandbox is False
    assert Config.load().models.for_role(Role.ANALYST).sandbox is True


def test_two_roles_on_the_same_pair_share_one_backend(tmp_path: Path) -> None:
    models = config_with(
        "roles:\n"
        "  analyst:\n    backend: fake\n    model: none\n"
        "  writer:\n    backend: fake\n    model: none\n",
        tmp_path,
    ).models
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
