"""The backend-neutral model URI scheme.

``model://<name>/<version>`` names one version; ``model://<name>@<alias>`` names whatever
version the alias points at when it is resolved. The same URI means the same thing whichever
registry adapter is configured, so it can be stored in the database, sent over the API or
written in a config file without naming a vendor. Each adapter keeps its own native address
(an MLflow ``models:/`` URI, a SageMaker ARN, a Vertex resource name) to itself.

A model name is 1-128 characters of letters, digits, ``_``, ``-`` and ``.``, starting with a
letter or digit; a version is a positive integer; an alias is a name that is not all digits.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from oran_adapt.core.errors import InvalidReferenceError

if TYPE_CHECKING:
    from oran_adapt.ports import ModelRegistryPort, ModelVersion

SCHEME = "model://"
_NAME = r"[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}"
_NAME_RE = re.compile(rf"^{_NAME}$")
_VERSION_RE = re.compile(r"^[1-9][0-9]*$")
_URI_RE = re.compile(
    rf"^{re.escape(SCHEME)}(?P<name>{_NAME})(?:/(?P<version>[1-9][0-9]*)|@(?P<alias>{_NAME}))$"
)


def check_model_name(name: str) -> str:
    """``name`` if it is a valid model name; InvalidReferenceError naming it otherwise."""
    if not _NAME_RE.match(name):
        raise InvalidReferenceError(f"invalid model name {name!r}", name=name, pattern=_NAME)
    return name


@dataclass(frozen=True)
class ModelUri:
    name: str
    version: str | None = None
    alias: str | None = None

    def __post_init__(self) -> None:
        check_model_name(self.name)
        if (self.version is None) == (self.alias is None):
            raise InvalidReferenceError("a model URI names exactly one of a version or an alias")
        if self.version is not None and not _VERSION_RE.match(self.version):
            raise InvalidReferenceError(
                f"invalid model version {self.version!r}", version=self.version
            )
        if self.alias is not None and (self.alias.isdigit() or not _NAME_RE.match(self.alias)):
            raise InvalidReferenceError(f"invalid model alias {self.alias!r}", alias=self.alias)

    def __str__(self) -> str:
        if self.version is not None:
            return f"{SCHEME}{self.name}/{self.version}"
        return f"{SCHEME}{self.name}@{self.alias}"

    @classmethod
    def parse(cls, uri: str) -> ModelUri:
        match = _URI_RE.match(uri)
        if match is None:
            raise InvalidReferenceError(
                f"not a model URI: {uri!r}",
                uri=uri,
                expected=f"{SCHEME}<name>/<version> or {SCHEME}<name>@<alias>",
            )
        return cls(match["name"], match["version"], match["alias"])

    @classmethod
    def for_version(cls, name: str, version: str) -> ModelUri:
        return cls(name, version=str(version))

    def resolve(self, registry: ModelRegistryPort) -> ModelVersion:
        """The version this URI names now. ModelNotFoundError if it does not exist."""
        version = self.version or registry.get_version_by_alias(self.name, str(self.alias))
        return registry.get_version(self.name, version)
