"""Capability descriptors and adapter specs - how an adapter tells the core what it is.

Every adapter is published as an :class:`AdapterSpec` under the entry-point group
``oran_adapt.<port>`` (see ``oran_adapt.plugins``). The spec pairs a static :class:`Capability`
descriptor, readable without importing the adapter's vendor SDK, with a factory that builds the
adapter from the validated settings. The composition root resolves each port exactly once.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Generic, TypeVar

if TYPE_CHECKING:
    from oran_adapt.core.config import Settings

T_co = TypeVar("T_co", covariant=True)


@dataclass(frozen=True)
class Capability:
    """What one adapter of one port offers.

    ``features`` are short, port-specific capability flags (e.g. ``"aliases"``, ``"streaming"``)
    the core may test for instead of branching on the adapter's name. ``config_keys`` are the
    settings the adapter reads; ``required_keys`` must be set when the adapter is selected, and
    startup fails naming them otherwise. ``production_keys`` have a default pointing at a local
    development path; ENVIRONMENT=production refuses to start until they are set explicitly.
    ``distributions`` are the optional Python packages the adapter needs installed.
    """

    port: str
    adapter: str
    description: str
    features: frozenset[str] = field(default_factory=frozenset)
    config_keys: tuple[str, ...] = ()
    required_keys: tuple[str, ...] = ()
    production_keys: tuple[str, ...] = ()
    distributions: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "port": self.port,
            "adapter": self.adapter,
            "description": self.description,
            "features": sorted(self.features),
            "config_keys": list(self.config_keys),
            "required_keys": list(self.required_keys),
            "production_keys": list(self.production_keys),
            "distributions": list(self.distributions),
        }


@dataclass(frozen=True)
class AdapterSpec(Generic[T_co]):
    """An entry point's target: the descriptor plus a factory taking the validated settings."""

    capability: Capability
    factory: Callable[[Settings], T_co]
