"""Entry-point plugin resolution.

Adapters are published by any installed distribution under the entry-point group
``oran_adapt.<port>`` (port names: ``oran_adapt.ports.PORTS``), the entry-point name being the
adapter name and its object an :class:`~oran_adapt.ports.AdapterSpec`. First-party adapters are
declared the same way in this project's ``pyproject.toml``, so a third-party adapter is a
``pip install`` plus one config key - no change to the core.

Loading is strict: a spec of the wrong type, for another port, or under a mismatching name is a
ConfigurationError at startup, never skipped.
"""

from __future__ import annotations

from functools import cache
from importlib.metadata import EntryPoint, entry_points

from oran_adapt.core.errors import ConfigurationError
from oran_adapt.ports import PORTS, AdapterSpec, Capability

GROUP_PREFIX = "oran_adapt."


def _check_port(port: str) -> None:
    if port not in PORTS:
        raise ConfigurationError(f"unknown port {port!r}", ports=sorted(PORTS))


def _entry_points(port: str) -> dict[str, EntryPoint]:
    found: dict[str, EntryPoint] = {}
    for ep in entry_points(group=GROUP_PREFIX + port):
        if ep.name in found and found[ep.name].value != ep.value:
            raise ConfigurationError(
                f"adapter {ep.name!r} for port {port!r} is registered twice",
                first=found[ep.name].value,
                second=ep.value,
            )
        found[ep.name] = ep
    return found


def _load(port: str, ep: EntryPoint) -> AdapterSpec[object]:
    try:
        spec = ep.load()
    except Exception as exc:
        raise ConfigurationError(
            f"adapter {ep.name!r} for port {port!r} failed to load",
            entry_point=ep.value,
            cause=str(exc),
        ) from exc
    if not isinstance(spec, AdapterSpec):
        raise ConfigurationError(
            f"entry point {ep.value!r} is not an AdapterSpec", port=port, adapter=ep.name
        )
    if spec.capability.port != port or spec.capability.adapter != ep.name:
        raise ConfigurationError(
            f"entry point {ep.value!r} describes {spec.capability.port}/"
            f"{spec.capability.adapter}, but is registered as {port}/{ep.name}",
        )
    return spec


@cache
def adapters(port: str) -> dict[str, AdapterSpec[object]]:
    """Every adapter installed for ``port``, by name."""
    _check_port(port)
    return {name: _load(port, ep) for name, ep in sorted(_entry_points(port).items())}


def resolve(port: str, name: str, *, config_key: str) -> AdapterSpec[object]:
    """The adapter ``name`` for ``port``; ``config_key`` is the setting that chose it, named in
    the error when no such adapter is installed."""
    available = adapters(port)
    try:
        return available[name]
    except KeyError:
        raise ConfigurationError(
            f"{config_key.upper()}={name!r}: no {port} adapter of that name is installed",
            key=config_key.upper(),
            available=sorted(available),
        ) from None


def capabilities() -> dict[str, list[Capability]]:
    """Port name -> the capability descriptors of its installed adapters."""
    return {port: [spec.capability for spec in adapters(port).values()] for port in PORTS}
