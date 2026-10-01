"""Hardening Phase 11: packaging and deployment.

Static checks of the images, the compose stack, the Helm chart and the kustomize tree (Helm,
kustomize, docker and a cluster are not on the development machine; CI renders, lints and
installs them: .github/workflows/ci.yml `packaging`, `build-images`, `phase-0-baseline`, and
.github/workflows/k8s-e2e.yml), plus the runtime pieces the manifests rely on: the worker's
liveness file, `oran-adapt db status` / `db wait`, and the expand-only migrations.
"""

from __future__ import annotations

import ast
import json
import os
import re
import sqlite3
import time
from pathlib import Path

import jsonschema
import pytest
import yaml

from oran_adapt import cli
from oran_adapt.core.config import Settings, load_settings
from oran_adapt.core.errors import (
    ConfigurationError,
    SchemaNotReadyError,
    WorkerUnhealthyError,
)
from oran_adapt.core.liveness import beat, check
from oran_adapt.db.migrate import schema_status, upgrade_to_head, wait_for_schema

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "deploy/helm/oran-adapt"
KUSTOMIZE = ROOT / "deploy/kustomize"
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
TARGETS = ("migrator", "worker", "api")


# ---- images -----------------------------------------------------------------------------------
def _stages(dockerfile: str) -> dict[str, str]:
    """Each build stage's instructions, keyed by stage name."""
    parts = re.split(r"^FROM\s+\S+\s+AS\s+(\S+)\s*$", dockerfile, flags=re.MULTILINE)
    return dict(zip(parts[1::2], parts[2::2], strict=True))


@pytest.mark.smoke
def test_the_dockerfile_builds_api_worker_and_migrator_from_a_pinned_base() -> None:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    base = re.search(r"^ARG PYTHON_IMAGE=(\S+)$", text, re.MULTILINE)
    assert base and DIGEST.search(base.group(1)), "the base image must be pinned by digest"
    assert re.findall(r"^FROM\s+(\S+)", text, re.MULTILINE) == ["${PYTHON_IMAGE}"] * 2 + [
        "runtime"] * 3
    stages = _stages(text)
    assert list(stages)[-1] == "api", "api is the default (last) target"
    assert set(TARGETS) <= set(stages)
    users = re.findall(r"^USER\s+(\S+)", stages["runtime"], re.MULTILINE)
    assert users == ["10001:10001"]
    assert "HEALTHCHECK NONE" in stages["migrator"]
    assert '"db", "upgrade"' in stages["migrator"]
    assert '"worker", "health"' in stages["worker"] and "WORKER_HEALTH_FILE=" in stages["worker"]
    assert "HEALTHCHECK" in stages["api"] and "UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN" in stages["api"]
    for stage in ("worker", "api"):
        assert "STOPSIGNAL SIGTERM" in stages[stage]
    # The API never migrates: the schema comes from the migrator.
    assert "upgrade" not in stages["api"]


@pytest.mark.smoke
def test_every_other_image_base_is_pinned_by_digest() -> None:
    for rel in ("docker/mlflow/Dockerfile", "docker/sandbox/Dockerfile"):
        froms = re.findall(r"^FROM\s+(\S+)", (ROOT / rel).read_text(encoding="utf-8"),
                           re.MULTILINE)
        assert froms and all(DIGEST.search(f) for f in froms), rel


# ---- compose ----------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


@pytest.mark.smoke
def test_compose_pins_pulled_images_and_builds_one_target_per_service(compose) -> None:
    for name, svc in compose["services"].items():
        if "build" in svc:
            build = svc["build"]
            if isinstance(build, dict) and build.get("context") == ".":
                assert build["target"] in TARGETS, name
        else:
            assert DIGEST.search(svc["image"]), f"{name}: pin {svc['image']} by digest"
    targets = {n: s["build"]["target"] for n, s in compose["services"].items()
               if isinstance(s.get("build"), dict) and "target" in s["build"]}
    assert targets == {"migrate": "migrator", "api": "api", "worker": "worker",
                       "cdc-consumer": "worker"}


@pytest.mark.smoke
def test_compose_runs_the_migration_once_before_anything_reads_the_schema(compose) -> None:
    services = compose["services"]
    assert services["migrate"]["restart"] == "no"
    assert services["migrate"]["depends_on"]["postgres"]["condition"] == "service_healthy"
    for name in ("api", "worker", "cdc-consumer", "debezium-init"):
        assert services[name]["depends_on"]["migrate"] == {
            "condition": "service_completed_successfully"}, name
    # A fresh clone needs no .env: the file is optional, only the password must be given.
    assert services["api"]["env_file"] == [{"path": ".env", "required": False}]
    assert ":?" in services["postgres"]["environment"]["POSTGRES_PASSWORD"]
    # The worker keeps the image's liveness check; the CDC consumer runs no job loop.
    assert "healthcheck" not in services["worker"]
    assert services["cdc-consumer"]["healthcheck"] == {"disable": True}
    smoke = (ROOT / "scripts/ci/compose_smoke.sh").read_text(encoding="utf-8")
    assert 'ONE_SHOT_SERVICES="migrate debezium-init"' in smoke
    assert "oran-adapt db status" in smoke and "oran-adapt worker health" in smoke


# ---- Helm -------------------------------------------------------------------------------------
def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        both = isinstance(value, dict) and isinstance(base.get(key), dict)
        out[key] = _merge(base[key], value) if both else value
    return out


def _values_files() -> list[Path]:
    return sorted((CHART / "examples").glob("values-*.yaml")) + [CHART / "ci/kind-values.yaml"]


@pytest.fixture(scope="module")
def values() -> dict:
    return yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))


@pytest.mark.smoke
def test_every_example_values_file_validates_against_the_chart_schema(values) -> None:
    schema = json.loads((CHART / "values.schema.json").read_text(encoding="utf-8"))
    jsonschema.Draft7Validator.check_schema(schema)
    jsonschema.validate(values, schema)
    files = _values_files()
    assert {f.stem for f in files} >= {"values-dev", "values-production", "values-gpu",
                                       "values-airgapped"}
    for path in files:
        jsonschema.validate(_merge(values, yaml.safe_load(path.read_text(encoding="utf-8"))),
                            schema)
    bad = _merge(values, {"workers": [{"name": "Not_DNS", "classes": [], "replicas": 1}]})
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)


def _templates() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted((CHART / "templates").iterdir())}


def _exists(tree: object, path: list[str]) -> bool:
    for i, part in enumerate(path):
        if not isinstance(tree, dict):
            return False
        if part not in tree:
            # An open map ({} by default: annotations, adapters, env) takes any key.
            return tree == {} and i > 0
        tree = tree[part]
    return True


@pytest.mark.smoke
def test_every_values_path_a_template_reads_is_declared_in_values_yaml(values) -> None:
    missing = []
    for name, text in _templates().items():
        refs = [m.split(".") for m in re.findall(r"\$?\.Values\.([A-Za-z0-9_.]+)", text)]
        refs += [["api", *m.split(".")] for m in re.findall(r"\$api\.([A-Za-z0-9_.]+)", text)]
        missing += [f"{name}: {'.'.join(r)}" for r in refs if not _exists(values, r)]
    assert not missing, missing


@pytest.mark.smoke
def test_the_chart_has_every_required_object() -> None:
    text = "\n".join(_templates().values())
    kinds = set(re.findall(r"^kind:\s*(\w+)", text, re.MULTILINE))
    assert kinds >= {"Deployment", "Service", "HorizontalPodAutoscaler", "PodDisruptionBudget",
                     "Job", "ConfigMap", "ExternalSecret", "Ingress", "NetworkPolicy",
                     "ServiceMonitor", "PrometheusRule", "ServiceAccount"}
    job = _templates()["migration-job.yaml"]
    assert "helm.sh/hook: pre-install,pre-upgrade" in job
    assert '"oran-adapt", "db", "upgrade"' in job
    helpers = _templates()["_helpers.tpl"]
    assert '"oran-adapt", "db", "wait"' in helpers
    workers = _templates()["workers.yaml"]
    assert "range $i, $w := .Values.workers" in workers and "JOB_WORKER_CLASSES" in workers
    assert '"oran-adapt", "worker", "health"' in workers
    gpu = yaml.safe_load((CHART / "examples/values-gpu.yaml").read_text(encoding="utf-8"))
    pool = next(w for w in gpu["workers"] if w["classes"] == ["gpu"])
    assert pool["resources"]["limits"]["nvidia.com/gpu"] == 1 and pool["tolerations"]


# Words that may look like hosts in a manifest: API groups, label and annotation prefixes.
ALLOWED_DOMAINS = ("k8s.io", "kubernetes.io", "helm.sh", "external-secrets.io",
                   "coreos.com")
HOST_LIKE = re.compile(r"\b(?:[a-z0-9-]+\.)+(?:com|net|org|io|local|internal|lan|corp|cloud|"
                       r"dev|example|svc|sh|ai|co|cluster)\b")
IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")


def _manifest_sources() -> dict[str, str]:
    files = sorted((CHART / "templates").iterdir()) + sorted((KUSTOMIZE / "base").iterdir())
    files += [CHART / "values.yaml"]
    return {str(p.relative_to(ROOT)): p.read_text(encoding="utf-8") for p in files}


@pytest.mark.smoke
def test_no_manifest_holds_an_environment_specific_literal() -> None:
    problems = []
    for rel, text in _manifest_sources().items():
        plain = re.sub(r"\{\{.*?\}\}", "", text, flags=re.DOTALL)
        problems += [f"{rel}: IP {ip}" for ip in IPV4.findall(plain)]
        problems += [f"{rel}: host {h.group(0)}" for h in HOST_LIKE.finditer(plain)
                     if not h.group(0).endswith(ALLOWED_DOMAINS)]
        problems += [f"{rel}: namespace" for _ in re.finditer(r"^\s*namespace:", plain,
                                                               re.MULTILINE)]
        for image in re.findall(r"^\s*image:\s*(\S+)", plain, re.MULTILINE):
            if rel.startswith("deploy/kustomize") and re.search(r"[/:@]", image):
                problems.append(f"{rel}: image {image} (set it in an overlay)")
    assert not problems, problems
    # The scan does find what it looks for.
    assert HOST_LIKE.search("registry.example.com") and IPV4.search("10.0.0.1")


_EXTERNAL_KEYS = {"MLFLOW_TRACKING_TOKEN", "UVICORN_PORT", "UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN"}


def _settings_keys() -> set[str]:
    return {name.upper() for name in Settings.model_fields} | _EXTERNAL_KEYS


@pytest.mark.smoke
def test_every_setting_the_manifests_pass_is_a_key_the_application_reads() -> None:
    known, unknown = _settings_keys(), []
    for path in _values_files() + [CHART / "values.yaml"]:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        envs = [doc.get("config", {}).get("env", {})]
        envs += [spec.get("env", {}) for spec in doc.get("adapters", {}).values()]
        envs += [w.get("env", {}) for w in doc.get("workers", [])]
        envs += [{k: None for k in doc.get("secrets", {}).get("externalSecret", {})
                  .get("data", {})}]
        unknown += [f"{path.name}: {k}" for env in envs for k in env if k not in known]
    for kust in KUSTOMIZE.rglob("kustomization.yaml"):
        doc = yaml.safe_load(kust.read_text(encoding="utf-8"))
        for gen in doc.get("configMapGenerator", []):
            unknown += [f"{kust.parent.name}: {lit.split('=', 1)[0]}"
                        for lit in gen.get("literals", []) if lit.split("=", 1)[0] not in known]
    assert not unknown, unknown


@pytest.mark.smoke
def test_every_alert_names_a_runbook_and_every_runbook_an_alert() -> None:
    rules = yaml.safe_load((CHART / "files/prometheus-rules.yaml").read_text(encoding="utf-8"))
    alerts = [r for g in rules["groups"] for r in g["rules"] if "alert" in r]
    assert alerts
    for alert in alerts:
        runbook = alert["annotations"]["runbook"]
        assert runbook == f"docs/runbooks/{alert['alert']}.md", alert["alert"]
        assert (ROOT / runbook).is_file(), runbook
        assert alert["labels"]["severity"] in {"critical", "warning", "info"}
    books = {p.stem for p in (ROOT / "docs/runbooks").glob("*.md")}
    assert books == {a["alert"] for a in alerts}


@pytest.mark.smoke
def test_the_kustomize_tree_is_complete() -> None:
    base = yaml.safe_load((KUSTOMIZE / "base/kustomization.yaml").read_text(encoding="utf-8"))
    for resource in base["resources"]:
        assert (KUSTOMIZE / "base" / resource).is_file(), resource
    for overlay in ("dev", "production"):
        doc = yaml.safe_load((KUSTOMIZE / "overlays" / overlay / "kustomization.yaml")
                             .read_text(encoding="utf-8"))
        assert "../../base" in doc["resources"]
        assert {i["name"] for i in doc["images"]} == {f"oran-adapt-{t}" for t in TARGETS}


# ---- expand/contract migrations -------------------------------------------------------------
_CONTRACTING = ("drop_", "alter_column", "rename_")
_CONTRACTING_SQL = re.compile(r"\b(DROP|RENAME|TRUNCATE)\b|\bALTER\s+COLUMN\b", re.IGNORECASE)


def _contracting_calls(fn: ast.FunctionDef) -> list[str]:
    found = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        attr = node.func.attr
        if attr.startswith(_CONTRACTING):
            found.append(attr)
        if attr == "add_column":
            # op.add_column("t", Column(...)) or batch_op.add_column(Column(...))
            column = next((a for a in node.args if isinstance(a, ast.Call)), None)
            kw = {k.arg: k.value for k in getattr(column, "keywords", [])}
            nullable = kw.get("nullable")
            not_null = isinstance(nullable, ast.Constant) and nullable.value is False
            if not_null and "server_default" not in kw:
                found.append("add_column NOT NULL without server_default")
        if attr == "execute":
            for const in ast.walk(node):
                if isinstance(const, ast.Constant) and isinstance(const.value, str) \
                        and _CONTRACTING_SQL.search(const.value):
                    found.append(f"execute: {const.value[:40]}")
    return found


@pytest.mark.smoke
def test_every_migration_upgrade_only_expands_the_schema() -> None:
    versions = sorted((ROOT / "migrations/versions").glob("*.py"))
    assert versions
    problems = []
    for path in versions:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module_sql = [n.value for n in ast.walk(tree) if isinstance(n, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id.startswith("_") for t in n.targets)]
        upgrade = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                       and n.name == "upgrade")
        problems += [f"{path.name}: {c}" for c in _contracting_calls(upgrade)]
        # DDL kept in module constants and executed by upgrade(): the trigger definitions.
        for value in module_sql:
            for const in ast.walk(value):
                if isinstance(const, ast.Constant) and isinstance(const.value, str) \
                        and re.search(r"\bDROP\s+(TABLE|COLUMN)\b", const.value, re.IGNORECASE):
                    problems.append(f"{path.name}: {const.value[:40]}")
    assert not problems, problems
    # The check does catch a contracting step.
    bad = ast.parse("def upgrade():\n    op.drop_column('t', 'c')\n    op.execute('DROP TABLE x')\n"
                    "    op.add_column('t', sa.Column('n', sa.Integer(), nullable=False))\n"
                    "    op.add_column('t', sa.Column('d', sa.Integer(), nullable=False,"
                    " server_default='0'))\n")
    assert len(_contracting_calls(bad.body[0])) == 3


# ---- liveness ---------------------------------------------------------------------------------
@pytest.mark.smoke
def test_the_liveness_file_is_fresh_after_a_beat_and_stale_later(tmp_path) -> None:
    path = str(tmp_path / "deep" / "worker.alive")
    with pytest.raises(WorkerUnhealthyError):
        check(path, 60)
    beat(path)
    report = check(path, 60)
    assert report["healthy"] is True and report["max_age_s"] == 60
    with pytest.raises(WorkerUnhealthyError) as stale:
        check(path, 60, now=time.time() + 120)
    assert stale.value.code == "WORKER_UNHEALTHY"
    with pytest.raises(ConfigurationError):
        check(None, 60)
    beat(None)  # no file configured: nothing to do


@pytest.mark.smoke
def test_a_liveness_file_that_cannot_be_written_never_fails_the_worker(tmp_path, caplog) -> None:
    blocker = tmp_path / "a-file"
    blocker.write_text("", encoding="utf-8")
    beat(str(blocker / "worker.alive"))  # its parent is a file
    assert "could not touch the liveness file" in caplog.text


@pytest.mark.smoke
def test_the_worker_health_window_must_exceed_two_beats() -> None:
    with pytest.raises(ConfigurationError) as exc:
        load_settings(job_heartbeat_s=5.0, job_poll_interval_s=1.0, worker_health_max_age_s=10.0)
    assert "WORKER_HEALTH_MAX_AGE_S" in str(exc.value.to_dict())
    assert load_settings(worker_health_max_age_s=11.0).worker_health_max_age_s == 11.0


def test_the_worker_loop_touches_the_liveness_file(tmp_path, monkeypatch) -> None:
    from oran_adapt.orchestrator import worker as worker_module

    beats: list[str | None] = []
    monkeypatch.setattr(worker_module.liveness, "beat", beats.append)
    health = str(tmp_path / "worker.alive")
    settings = load_settings(database_url=f"sqlite:///{(tmp_path / 'w.db').as_posix()}",
                             worker_health_file=health)
    upgrade_to_head(settings.database_url)
    worker = worker_module.worker_from_settings(settings)
    worker.run(once=True)
    assert beats and set(beats) == {health}


# ---- db status / db wait ----------------------------------------------------------------------
def _sqlite(tmp_path: Path) -> tuple[str, Path]:
    path = tmp_path / "schema.db"
    return f"sqlite:///{path.as_posix()}", path


def test_schema_status_reports_behind_at_head_and_ahead(tmp_path) -> None:
    url, path = _sqlite(tmp_path)
    empty = schema_status(url)
    assert empty["state"] == "behind" and empty["current"] == []
    upgrade_to_head(url)
    at_head = schema_status(url)
    assert at_head["state"] == "at_head" and at_head["current"] == at_head["head"]
    with sqlite3.connect(path) as conn:  # a newer release migrated this database
        conn.execute("UPDATE alembic_version SET version_num = 'f00dfeed0099'")
    ahead = schema_status(url)
    assert ahead["state"] == "ahead" and ahead["current"] == ["f00dfeed0099"]
    # A newer schema is fine for this release: waiting returns at once.
    assert wait_for_schema(url, timeout_s=1, interval_s=0.01)["state"] == "ahead"


def test_db_wait_times_out_on_a_database_that_is_not_migrated(tmp_path) -> None:
    url, _ = _sqlite(tmp_path)
    with pytest.raises(SchemaNotReadyError) as exc:
        wait_for_schema(url, timeout_s=0.05, interval_s=0.01)
    assert exc.value.code == "SCHEMA_NOT_READY" and exc.value.context["state"] == "behind"
    unreachable = "postgresql+psycopg://nobody:none@127.0.0.1:1/none?connect_timeout=1"
    with pytest.raises(SchemaNotReadyError) as down:
        wait_for_schema(unreachable, timeout_s=0.05, interval_s=0.01)
    assert down.value.context["state"] == "unreachable"


def test_the_cli_reports_schema_state_and_worker_health(tmp_path, monkeypatch, capsys) -> None:
    url, _ = _sqlite(tmp_path)
    health = tmp_path / "worker.alive"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("WORKER_HEALTH_FILE", str(health))
    monkeypatch.setattr(cli, "get_settings", load_settings)

    assert cli.main(["db", "status"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "behind"
    assert cli.main(["db", "wait", "--timeout-s", "0.05"]) == 1
    assert json.loads(capsys.readouterr().out)["code"] == "SCHEMA_NOT_READY"
    assert cli.main(["db", "upgrade"]) == 0
    capsys.readouterr()
    assert cli.main(["db", "wait"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "at_head"

    assert cli.main(["worker", "health"]) == 1
    assert json.loads(capsys.readouterr().out)["code"] == "WORKER_UNHEALTHY"
    health.touch()
    assert cli.main(["worker", "health"]) == 0
    assert json.loads(capsys.readouterr().out)["healthy"] is True
    old = time.time() - 3600
    os.utime(health, (old, old))
    assert cli.main(["worker", "health"]) == 1
