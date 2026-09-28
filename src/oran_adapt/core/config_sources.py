"""Configuration layers, provenance, redaction and lint.

Precedence, highest first:

1. ``init``    - keyword arguments (tests, embedding code)
2. ``env``     - process environment (``DATABASE_URL`` ...)
3. ``dotenv``  - the ``.env`` file in the working directory
4. ``secrets`` - secret-typed keys from the selected SecretsPort backend (SECRETS_BACKEND)
5. ``file``    - the TOML file named by ``ORAN_CONFIG_FILE``
6. ``default`` - the schema defaults in ``Settings``

The TOML file uses sections that join with ``_`` onto the flat key, so ``[llm]
max_output_tokens = 1024`` sets ``llm_max_output_tokens`` (env ``LLM_MAX_OUTPUT_TOKENS``). A table
whose flat name is itself a dict-typed key (``api_keys``, ``policy_roles``) is that key's value.
Unknown keys in the file are an error, never ignored, and so are secret-typed keys: secrets come
from the environment or the secrets backend only.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any, get_args, get_origin

from pydantic import SecretStr
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from oran_adapt.core.errors import ConfigurationError

if TYPE_CHECKING:
    from oran_adapt.core.config import Settings

CONFIG_FILE_ENV = "ORAN_CONFIG_FILE"
REDACTED = "**********"


def _is_dict_field(field: FieldInfo) -> bool:
    annotation = field.annotation
    if get_origin(annotation) is dict:
        return True
    return any(get_origin(arg) is dict for arg in get_args(annotation))


def is_secret_field(field: FieldInfo) -> bool:
    annotation = field.annotation
    return annotation is SecretStr or SecretStr in get_args(annotation)


def flatten(table: dict[str, Any], dict_fields: set[str], prefix: str = "") -> dict[str, Any]:
    """Nested TOML tables -> flat ``section_key`` names (lower-case, ``-`` read as ``_``)."""
    out: dict[str, Any] = {}
    for raw_key, value in table.items():
        key = raw_key.lower().replace("-", "_")
        flat = f"{prefix}_{key}" if prefix else key
        if isinstance(value, dict) and flat not in dict_fields:
            out.update(flatten(value, dict_fields, flat))
        else:
            out[flat] = value
    return out


def read_config_file(settings_cls: type[BaseSettings], path: str | Path) -> dict[str, Any]:
    """The file's settings as flat keys. ConfigurationError for an unreadable file, bad TOML or
    any key the schema does not define."""
    path = Path(path)
    try:
        with path.open("rb") as fh:
            table = tomllib.load(fh)
    except OSError as exc:
        raise ConfigurationError(
            f"config file {path} cannot be read", key=CONFIG_FILE_ENV, cause=str(exc)
        ) from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigurationError(
            f"config file {path} is not valid TOML", key=CONFIG_FILE_ENV, cause=str(exc)
        ) from exc
    fields = settings_cls.model_fields
    dict_fields = {name for name, field in fields.items() if _is_dict_field(field)}
    values = flatten(table, dict_fields)
    unknown = sorted(set(values) - set(fields))
    if unknown:
        raise ConfigurationError(
            f"config file {path} has unknown key(s): {', '.join(unknown)}",
            key=CONFIG_FILE_ENV,
            unknown=unknown,
        )
    secrets = sorted(k for k in values if is_secret_field(fields[k]))
    if secrets:
        raise ConfigurationError(
            f"config file {path} holds secret key(s): {', '.join(secrets)}",
            key=secrets[0].upper(),
            hint="set secrets in the environment or through SECRETS_BACKEND, never in a file",
        )
    return values


class TomlFileSource(PydanticBaseSettingsSource):
    """Layer 5: the TOML file named by ``ORAN_CONFIG_FILE`` (or an explicit path)."""

    def __init__(self, settings_cls: type[BaseSettings], path: str | None = None) -> None:
        super().__init__(settings_cls)
        self.path = path if path is not None else os.environ.get(CONFIG_FILE_ENV)
        self._values = read_config_file(settings_cls, self.path) if self.path else {}

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return self._values.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        return dict(self._values)


class SecretsSource(PydanticBaseSettingsSource):
    """Layer 4: every secret-typed key not already set by a higher layer, looked up in the
    SecretsPort adapter named by ``secrets_backend`` (the ``env`` backend adds nothing the env
    layer has not already read)."""

    def __init__(self, settings_cls: type[BaseSettings], file_source: TomlFileSource) -> None:
        super().__init__(settings_cls)
        self._file = file_source

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def _setting(self, key: str) -> Any:
        if key in self.current_state:
            return self.current_state[key]
        return self._file().get(key)

    def __call__(self) -> dict[str, Any]:
        from oran_adapt import plugins

        backend = self._setting("secrets_backend") or "env"
        if backend == "env":
            return {}
        spec = plugins.resolve("secrets", backend, config_key="secrets_backend")
        # Only the keys the secrets adapter reads; the rest of the settings are not known yet.
        known: dict[str, Any] = {"secrets_backend": backend, "secrets_dir": self._setting("secrets_dir")}
        partial = self.settings_cls.model_construct(**known)
        store = spec.factory(partial)  # type: ignore[arg-type]
        found: dict[str, Any] = {}
        for name, field in self.settings_cls.model_fields.items():
            if is_secret_field(field) and name not in self.current_state:
                value = store.get(name)  # type: ignore[attr-defined]
                if value is not None:
                    found[name] = value
        return found


# ---- provenance and redaction ------------------------------------------------------------------
def _redact_url(value: str) -> str:
    """Hide the password in ``scheme://user:password@host`` and any query string."""
    scheme, sep, rest = value.partition("://")
    if not sep:
        return value
    userinfo, at, hostpart = rest.rpartition("@")
    if at and ":" in userinfo:
        rest = f"{userinfo.split(':', 1)[0]}:{REDACTED}@{hostpart}"
    base, q, _ = rest.partition("?")
    return f"{scheme}://{base}{'?' + REDACTED if q else ''}"


def redact(name: str, field: FieldInfo, value: Any) -> Any:
    if value is None:
        return None
    if is_secret_field(field):
        return REDACTED
    if name == "api_keys":
        # Digests are not keys, but they still identify callers: show only name and role.
        return {f"{digest[:8]}...": spec for digest, spec in value.items()}
    if isinstance(value, str) and ("_url" in name or "_uri" in name or name.endswith("url")):
        return _redact_url(value)
    return value


def layer_values(settings_cls: type[Settings]) -> list[tuple[str, dict[str, Any]]]:
    """Each non-init layer's own values, highest priority first (used for provenance)."""
    from pydantic_settings import DotEnvSettingsSource, EnvSettingsSource

    file_source = TomlFileSource(settings_cls)
    env = EnvSettingsSource(settings_cls)()
    dotenv = DotEnvSettingsSource(settings_cls)()
    secrets = SecretsSource(settings_cls, file_source)
    secrets._set_current_state({**dotenv, **env})
    return [("env", env), ("dotenv", dotenv), ("secrets", secrets()), ("file", file_source())]


def effective_config(settings: Settings) -> dict[str, dict[str, Any]]:
    """Every key's effective (redacted) value and the layer it came from."""
    layers = layer_values(type(settings))
    out: dict[str, dict[str, Any]] = {}
    for name, field in type(settings).model_fields.items():
        source = "default"
        for layer, values in layers:
            if name in values:
                source = layer
                break
        else:
            if name in settings.model_fields_set:
                source = "init"
        out[name] = {"value": redact(name, field, getattr(settings, name)), "source": source}
    return out
