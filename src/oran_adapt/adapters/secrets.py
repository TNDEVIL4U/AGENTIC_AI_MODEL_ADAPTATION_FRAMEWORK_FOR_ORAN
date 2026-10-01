"""Secrets adapters ``env`` (process environment) and ``file`` (one file per secret in a
directory, the Docker/Kubernetes secrets layout: ``<dir>/<name>``, name lower-case).

The config loader asks the selected backend for every secret-typed setting (e.g.
``anthropic_api_key``) before validation, so a secret never has to sit in a config file."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from oran_adapt.core.errors import ConfigurationError
from oran_adapt.ports import AdapterSpec, Capability

if TYPE_CHECKING:
    from oran_adapt.core.config import Settings


class EnvSecrets:
    def get(self, name: str) -> str | None:
        return os.environ.get(name.upper())


class FileSecrets:
    def __init__(self, directory: str) -> None:
        self.directory = Path(directory)
        if not self.directory.is_dir():
            raise ConfigurationError(
                "SECRETS_DIR does not exist or is not a directory",
                key="SECRETS_DIR",
                path=str(self.directory),
            )

    def get(self, name: str) -> str | None:
        path = self.directory / name.lower()
        if not path.is_file():
            return None
        # Secret files conventionally end with a newline that is not part of the value.
        return path.read_text(encoding="utf-8").rstrip("\r\n")


def _env(settings: Settings) -> EnvSecrets:
    return EnvSecrets()


def _file(settings: Settings) -> FileSecrets:
    directory = settings.secrets_dir
    if not directory:
        raise ConfigurationError(
            "SECRETS_BACKEND=file requires SECRETS_DIR", key="SECRETS_DIR"
        )
    return FileSecrets(directory)


ENV = AdapterSpec(
    capability=Capability(
        port="secrets",
        adapter="env",
        description="secrets from environment variables (upper-case name)",
        features=frozenset({"read"}),
    ),
    factory=_env,
)

FILE = AdapterSpec(
    capability=Capability(
        port="secrets",
        adapter="file",
        description="one file per secret in SECRETS_DIR (Docker/Kubernetes secret mounts)",
        features=frozenset({"read", "rotation_on_restart"}),
        config_keys=("secrets_dir",),
        required_keys=("secrets_dir",),
    ),
    factory=_file,
)
