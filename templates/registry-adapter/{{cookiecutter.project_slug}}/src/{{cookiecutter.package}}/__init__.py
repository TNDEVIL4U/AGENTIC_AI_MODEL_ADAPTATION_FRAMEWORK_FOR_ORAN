"""Registry adapter ``{{ cookiecutter.adapter_name }}`` for oran-adapt.

This module is loaded whenever oran-adapt lists or resolves registry adapters, so it imports
neither the backend SDK nor ``registry.py``: the factory imports them when the adapter is
selected (REGISTRY_BACKEND={{ cookiecutter.adapter_name }}).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from oran_adapt.ports import AdapterSpec, Capability

if TYPE_CHECKING:
    from oran_adapt.core.config import Settings
    from oran_adapt.ports import ModelRegistryPort


def _build(settings: Settings) -> ModelRegistryPort:
    from {{ cookiecutter.package }}.registry import {{ cookiecutter.class_name }}

    return {{ cookiecutter.class_name }}.from_settings(settings)


SPEC = AdapterSpec(
    capability=Capability(
        port="registry",
        adapter="{{ cookiecutter.adapter_name }}",
        description="{{ cookiecutter.project_name }}",
        # Flags the core may test for; keep the ones your backend really supports.
        features=frozenset({"aliases", "version_tags", "version_metrics"}),
        # For display (GET /api/v1/capabilities, `oran-adapt config effective`). This adapter's
        # own keys live in {{ cookiecutter.class_name }}Settings ({{ cookiecutter.env_prefix }}*),
        # which validates them when the adapter is built at startup. required_keys and
        # production_keys may only name core Settings fields.
        config_keys=("{{ cookiecutter.env_prefix.lower() }}root",),
        distributions=("{{ cookiecutter.project_slug }}",),
    ),
    factory=_build,
)
