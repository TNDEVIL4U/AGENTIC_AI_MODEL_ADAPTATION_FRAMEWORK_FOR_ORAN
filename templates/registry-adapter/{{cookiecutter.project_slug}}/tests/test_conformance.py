"""The oran-adapt registry conformance suite, run against this adapter.

Every check must pass before the adapter is used. A check failing with ConformanceFailure names
the rule broken; docs/adapters/registry.md in oran-adapt explains each rule.
"""

from __future__ import annotations

import pytest

from oran_adapt import plugins
from oran_adapt.conformance.registry import CHECKS, Context
from oran_adapt.core.config import Settings
from oran_adapt.core.errors import ConfigurationError

ADAPTER = "{{ cookiecutter.adapter_name }}"
ROOT_ENV = "{{ cookiecutter.env_prefix }}ROOT"


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv(ROOT_ENV, str(tmp_path / "registry"))
    return plugins.adapters("registry")[ADAPTER].factory(Settings(_env_file=None))


def test_the_adapter_is_installed_under_its_entry_point() -> None:
    assert ADAPTER in plugins.adapters("registry")


@pytest.mark.parametrize("check", sorted(CHECKS))
def test_conformance(registry, check, tmp_path) -> None:
    CHECKS[check](registry, Context(tmp_path / "work"))


def test_a_missing_key_fails_naming_it(monkeypatch) -> None:
    monkeypatch.delenv(ROOT_ENV, raising=False)
    with pytest.raises(ConfigurationError, match=ROOT_ENV):
        plugins.adapters("registry")[ADAPTER].factory(Settings(_env_file=None))
