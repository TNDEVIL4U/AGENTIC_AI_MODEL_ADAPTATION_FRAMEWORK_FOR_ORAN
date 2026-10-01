"""Hardening Phase 15: the last settings that needed a decision (C4-C6, C10, C11), the MLflow
image's pins (D7), and the audit documents that scripts/audit.py scans."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from oran_adapt.core.config import Settings
from oran_adapt.core.errors import ConfigurationError, SandboxExecutionError
from oran_adapt.sandbox.runner import run_sandboxed

ROOT = Path(__file__).resolve().parents[2]
DIGEST = "registry.example/oran-adapt-sandbox@sha256:" + "a" * 64
REFUSED = (ConfigurationError, ValidationError)


@pytest.mark.smoke
@pytest.mark.parametrize("image", [None, "", "oran-adapt-sandbox:latest", "sandbox:1.2.3"])
def test_the_docker_sandbox_needs_an_image_pinned_by_digest(image) -> None:
    with pytest.raises(REFUSED, match="SANDBOX_DOCKER_IMAGE"):
        Settings(_env_file=None, sandbox_backend="docker", sandbox_docker_image=image)


@pytest.mark.smoke
def test_a_digest_pinned_image_is_accepted_and_subprocess_needs_none() -> None:
    assert Settings(_env_file=None, sandbox_backend="docker",
                    sandbox_docker_image=DIGEST).sandbox_docker_image == DIGEST
    assert Settings(_env_file=None, sandbox_backend="subprocess").sandbox_docker_image is None


def test_the_runner_refuses_the_docker_backend_without_an_image(tmp_path) -> None:
    import pandas as pd

    with pytest.raises(SandboxExecutionError, match="SANDBOX_DOCKER_IMAGE"):
        run_sandboxed("print(1)", current_model=None, X=pd.DataFrame({"a": [1]}),
                      y=pd.Series([1]), timeout_s=5, memory_mb=256, workdir=str(tmp_path),
                      backend="docker", docker_image=None)


@pytest.mark.smoke
@pytest.mark.parametrize("missing", ["cdc_kafka_topic", "cdc_consumer_group"])
def test_kafka_cdc_requires_its_topic_and_consumer_group(missing) -> None:
    keys = {"cdc_mode": "kafka", "kafka_bootstrap_servers": "kafka:9092",
            "cdc_kafka_topic": "site.public.kpi", "cdc_consumer_group": "site-cdc"}
    del keys[missing]
    with pytest.raises(REFUSED, match=missing.upper()):
        Settings(_env_file=None, **keys)
    keys[missing] = "set"
    Settings(_env_file=None, **keys)


@pytest.mark.smoke
@pytest.mark.parametrize("provider", ["anthropic", "gemini"])
def test_an_llm_provider_requires_its_model_id(provider) -> None:
    keys = {"llm_enabled": True, "llm_provider": provider, f"{provider}_api_key": "k"}
    with pytest.raises(REFUSED, match=f"{provider.upper()}_MODEL"):
        Settings(_env_file=None, **keys)
    assert getattr(Settings(_env_file=None, **keys, **{f"{provider}_model": "m"}),
                   f"{provider}_model") == "m"


@pytest.mark.smoke
def test_no_model_id_topic_group_or_image_has_a_default() -> None:
    fields = Settings.model_fields
    for key in ("anthropic_model", "gemini_model", "sandbox_docker_image", "cdc_kafka_topic",
                "cdc_consumer_group"):
        assert fields[key].default is None, key


def _pins(text: str) -> dict[str, str]:
    pins = {}
    for line in text.splitlines():
        match = re.match(r"([A-Za-z0-9_.-]+)(?:\[[^\]]*\])?==(\S+)", line.strip())
        if match:
            pins[match.group(1).lower()] = match.group(2)
    return pins


@pytest.mark.smoke
def test_the_mlflow_image_requirements_match_the_lock() -> None:
    image = _pins((ROOT / "docker/mlflow/requirements.txt").read_text(encoding="utf-8"))
    lock = _pins((ROOT / "requirements.lock").read_text(encoding="utf-8"))
    assert set(image) == {"mlflow", "psycopg"}, image
    for name, version in image.items():
        assert lock.get(name) == version, f"{name}=={version}, lock has {lock.get(name)}"
    dockerfile = (ROOT / "docker/mlflow/Dockerfile").read_text(encoding="utf-8")
    assert "-r /tmp/mlflow-requirements.txt" in dockerfile
    assert "==" not in dockerfile


@pytest.mark.smoke
def test_the_hardcoding_audit_passes() -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import audit
    finally:
        sys.path.remove(str(ROOT / "scripts"))
    assert audit.hardcoding() == []
