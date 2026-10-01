"""A secrets adapter template: secret values from one JSON document.

The document maps setting names to values (``{"dataset_http_token": "...", ...}``); names are
matched case-insensitively. Replace ``_load()`` with your secret manager (AWS Secrets Manager,
Azure Key Vault, GCP Secret Manager, CyberArk, ...; import its SDK inside the adapter) and keep
``get``'s rules (docs/adapters/auth.md): the stored value exactly, None for a name the backend
does not hold, and never a secret value in an error message or a log line.

Register it in your package's ``pyproject.toml``::

    [project.entry-points."oran_adapt.secrets"]
    json-file = "my_package.secrets_adapter:SPEC"

then set ``SECRETS_BACKEND=json-file`` and ``SECRETS_JSON_FILE``.
"""

from __future__ import annotations

import json
import os
from typing import Any

from oran_adapt.core.errors import ConfigurationError
from oran_adapt.ports import AdapterSpec, Capability


class JsonFileSecrets:
    def __init__(self, path: str) -> None:
        self.path = path
        self._values = self._load()

    def _load(self) -> dict[str, str]:
        try:
            with open(self.path, encoding="utf-8") as handle:
                document = json.load(handle)
        except (OSError, ValueError) as exc:
            # The exception type only: a parse error can quote the document's contents.
            raise ConfigurationError("SECRETS_JSON_FILE could not be read",
                                     key="SECRETS_JSON_FILE", cause=type(exc).__name__) from exc
        return {str(k).lower(): str(v) for k, v in dict(document).items()}

    def get(self, name: str) -> str | None:
        return self._values.get(name.lower())


def _factory(settings: Any) -> JsonFileSecrets:
    # Settings ignores unknown keys; a plugin reads its own from the environment.
    return JsonFileSecrets(os.environ.get("SECRETS_JSON_FILE", "secrets.json"))


SPEC = AdapterSpec(
    capability=Capability(
        port="secrets",
        adapter="json-file",
        description="secret values from one JSON document",
    ),
    factory=_factory,
)
