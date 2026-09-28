"""Zero hardcoding: the layered, schema-validated configuration.

Startup fails fast naming the missing key; the TOML file sits under the environment; a file can
neither invent keys nor hold secrets; config-lint passes every shipped example; the effective
configuration and the adapter capabilities are visible over the API, secrets redacted."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from oran_adapt import cli
from oran_adapt.api.app import create_app
from oran_adapt.core.config import PRODUCTION_REQUIRED, Settings, lint_config_file, load_settings
from oran_adapt.core.config_sources import CONFIG_FILE_ENV, REDACTED, effective_config
from oran_adapt.core.errors import ConfigurationError

pytestmark = pytest.mark.smoke

EXAMPLES = sorted((Path(__file__).resolve().parents[2] / "config" / "examples").glob("*.toml"))
SECRET = "sk-test-do-not-leak-0123456789"


@pytest.fixture
def isolated(monkeypatch, tmp_path) -> Path:
    """No config file, no ``.env`` and none of the keys these tests set, from the shell."""
    monkeypatch.chdir(tmp_path)
    for key in (CONFIG_FILE_ENV, "ENVIRONMENT", "LOG_LEVEL", "JOB_TIMEOUT_S", "CDC_MODE",
                "KAFKA_BOOTSTRAP_SERVERS", *(k.upper() for k in PRODUCTION_REQUIRED)):
        monkeypatch.delenv(key, raising=False)
    return tmp_path


def _toml(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_a_selected_adapter_without_its_required_key_fails_naming_it(isolated) -> None:
    with pytest.raises(ConfigurationError) as info:
        load_settings(_env_file=None, cdc_mode="kafka")
    assert info.value.context["key"] == "KAFKA_BOOTSTRAP_SERVERS"
    assert info.value.context["selected_by"] == "CDC_MODE"
    assert "KAFKA_BOOTSTRAP_SERVERS" in info.value.message


def test_production_refuses_defaulted_storage_locations(isolated) -> None:
    with pytest.raises(ConfigurationError) as info:
        load_settings(_env_file=None, environment="production")
    assert info.value.context["missing"] == [k.upper() for k in PRODUCTION_REQUIRED]


def test_a_bad_value_fails_naming_the_key(isolated) -> None:
    with pytest.raises(ConfigurationError) as info:
        load_settings(_env_file=None, job_timeout_s=0)
    assert info.value.context["key"] == "JOB_TIMEOUT_S"


def test_the_file_layer_sits_under_the_environment(isolated, monkeypatch) -> None:
    path = _toml(isolated / "c.toml", '[log]\nlevel = "WARNING"\n[job]\ntimeout_s = 42.0\n')
    monkeypatch.setenv(CONFIG_FILE_ENV, str(path))
    monkeypatch.setenv("LOG_LEVEL", "ERROR")
    settings = Settings(_env_file=None)
    assert settings.job_timeout_s == 42.0  # from the file
    assert settings.log_level == "ERROR"  # the environment wins over the file
    effective = effective_config(settings)
    assert effective["job_timeout_s"]["source"] == "file"
    assert effective["log_level"]["source"] == "env"
    assert effective["llm_max_output_tokens"]["source"] == "default"


def test_a_file_with_an_unknown_key_is_refused(isolated, monkeypatch) -> None:
    path = _toml(isolated / "c.toml", '[job]\ntimeout_seconds = 1.0\n')
    monkeypatch.setenv(CONFIG_FILE_ENV, str(path))
    with pytest.raises(ConfigurationError) as info:
        Settings(_env_file=None)
    assert info.value.context["unknown"] == ["job_timeout_seconds"]


def test_a_file_holding_a_secret_is_refused(isolated, monkeypatch) -> None:
    path = _toml(isolated / "c.toml", f'[anthropic]\napi_key = "{SECRET}"\n')
    monkeypatch.setenv(CONFIG_FILE_ENV, str(path))
    with pytest.raises(ConfigurationError) as info:
        Settings(_env_file=None)
    assert info.value.context["key"] == "ANTHROPIC_API_KEY"
    assert SECRET not in json.dumps(info.value.to_dict())


def test_effective_config_redacts_secrets_and_url_passwords(isolated) -> None:
    settings = Settings(
        _env_file=None,
        anthropic_api_key=SecretStr(SECRET),
        database_url="postgresql://app:hunter2@db:5432/oran?sslkey=x",
    )
    effective = effective_config(settings)
    assert effective["anthropic_api_key"] == {"value": REDACTED, "source": "init"}
    url = effective["database_url"]["value"]
    assert "hunter2" not in url and "sslkey" not in url
    assert url.startswith("postgresql://app:")
    assert SECRET not in json.dumps(effective, default=str)


def test_every_example_config_passes_lint() -> None:
    assert EXAMPLES, "config/examples/*.toml is empty"
    for path in EXAMPLES:
        assert lint_config_file(str(path)) == [], path
    assert cli.main(["config", "lint", *map(str, EXAMPLES)]) == 0


def test_lint_reports_each_bad_file_by_name(tmp_path, capsys) -> None:
    unknown = _toml(tmp_path / "unknown.toml", "colour = 1\n")
    kafka = _toml(tmp_path / "kafka.toml", 'cdc_mode = "kafka"\n')
    bad_value = _toml(tmp_path / "bad.toml", "[job]\ntimeout_s = -1\n")
    assert "colour" in lint_config_file(str(unknown))[0]
    (problem,) = lint_config_file(str(kafka))
    assert str(kafka) in problem and "KAFKA_BOOTSTRAP_SERVERS" in problem
    (problem,) = lint_config_file(str(bad_value))
    assert str(bad_value) in problem and "JOB_TIMEOUT_S" in problem

    capsys.readouterr()
    assert cli.main(["config", "lint", str(unknown), str(kafka), *map(str, EXAMPLES)]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["code"] == "CONFIGURATION_ERROR"
    assert len(out["context"]["problems"]) == 2


@pytest.fixture
def app_client(migrated_settings) -> TestClient:
    settings = migrated_settings.model_copy(update={"anthropic_api_key": SecretStr(SECRET)})
    with TestClient(create_app(settings)) as client:
        yield client


def test_capabilities_lists_every_registered_adapter(app_client) -> None:
    r = app_client.get("/api/v1/capabilities")
    assert r.status_code == 200
    ports = r.json()["ports"]
    names = {port: {a["adapter"] for a in info["adapters"]} for port, info in ports.items()}
    assert {"mlflow"} <= names["registry"]
    assert {"process", "thread"} <= names["job_executor"]
    assert {"kafka", "polling"} <= names["cdc_source"]
    assert {"anthropic", "gemini"} <= names["llm"]
    assert ports["registry"]["selected"] == "mlflow"
    for info in ports.values():
        for adapter in info["adapters"]:
            assert {"port", "adapter", "description", "features", "config_keys"} <= set(adapter)


def test_the_effective_config_endpoint_is_redacted(app_client) -> None:
    r = app_client.get("/api/v1/config/effective")
    assert r.status_code == 200
    keys = r.json()["keys"]
    assert keys["anthropic_api_key"]["value"] == REDACTED
    assert keys["job_timeout_s"]["value"] == app_client.app.state.settings.job_timeout_s
    assert SECRET not in r.text
