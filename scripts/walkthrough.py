"""Clone-to-canary walkthrough: from a fresh checkout to a candidate model serving a canary slice,
driven only by what a monitoring system would send.

    1 a model is onboarded and LIVE    5 the job ran the pipeline to DELIVERING
    2 the API is ready                 6 the rollout is in CANARY at the policy's first step
    3 a drift alert arrives as an      7 the serving side reports that same split
      Alertmanager webhook             8 the same alert again is a duplicate, not a second job
    4 the mapper turns it into a job

The alert is ``config/mappers/samples/alertmanager.json`` posted unchanged to
``POST /api/v1/adaptation/events/from/alertmanager``; the model it names (cell-a-throughput, with
the drifted data version drift-1 of cell-a-kpis) is onboarded first from synthetic KPI data with
fixed seeds. Every step checks what the system actually did and exits 1 if it is wrong.

Two modes:

- local (the default, and the Phase 14 gate): the real FastAPI app in-process against a temporary
  SQLite database, the filesystem registry, the registry-alias deployment and
  ``config/policies/delivery.toml`` with DELIVERY_STRATEGY=canary. Nothing is left behind.
- ``--base-url URL``: a running stack (``docker compose up``; docs/integration-guide.md). The
  model is onboarded with the settings of this environment (ORAN_CONFIG_FILE / env), which must
  point at the stack's database and registry, and every other step goes over HTTP; the API key,
  if the stack needs one, is read from ORAN_API_KEY. The stack must run with
  DELIVERY_STRATEGY=canary, DELIVERY_POLICY_FILE and DRIFT_MAPPERS naming the alertmanager
  mapper. ``--skip-onboard`` reuses a model onboarded by an earlier run.

Usage:
    python scripts/walkthrough.py
    python scripts/walkthrough.py --base-url http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from oran_adapt.api.app import create_app
from oran_adapt.core.config import Settings, get_settings
from oran_adapt.core.enums import TERMINAL_STATUSES
from oran_adapt.core.policies import DeliveryPolicy, load_policy_file

SAMPLE = ROOT / "config" / "mappers" / "samples" / "alertmanager.json"
MAPPER = ROOT / "config" / "mappers" / "alertmanager.toml"
POLICY = ROOT / "config" / "policies" / "delivery.toml"
FEATURES = ["prb_util", "cqi", "rsrp"]
TARGET = "throughput"
STEPS = 8


class WalkthroughFailure(RuntimeError):
    pass


def check(condition: object, message: str) -> None:
    if not condition:
        raise WalkthroughFailure(message)


def step(n: int, title: str) -> None:
    print(f"\n[{n}/{STEPS}] {title}")


def kpi_frame(n: int, *, shift: float, slope: float, seed: int) -> pd.DataFrame:
    """Synthetic cell KPIs; ``shift`` moves the PRB-utilisation distribution and ``slope`` is
    how throughput depends on it (a changed slope is the concept drift a retrain fixes)."""
    rng = np.random.default_rng(seed)
    df = pd.DataFrame({f: rng.normal(shift if f == "prb_util" else 0.0, 1.0, n) for f in FEATURES})
    df[TARGET] = slope * df["prb_util"] + 0.5 * df["cqi"] + rng.normal(0, 0.1, n)
    return df


def alert() -> tuple[dict[str, Any], dict[str, str]]:
    """The sample webhook and the labels of its one firing alert."""
    payload = json.loads(SAMPLE.read_text(encoding="utf-8"))
    firing = [a for a in payload["alerts"] if a["status"] == "firing"]
    check(len(firing) == 1, f"{SAMPLE.name} should hold exactly one firing alert")
    return payload, firing[0]["labels"]


def first_canary_percent() -> int:
    policy: DeliveryPolicy = load_policy_file(str(POLICY), DeliveryPolicy, "delivery_policy_file")
    return policy.canary_steps[0]


def onboard(settings: Settings, labels: dict[str, str]) -> tuple[str, str]:
    """Register the model the alert names as version 1, LIVE, with its training and drifted
    data versions; returns (registry name, version). Uses the components ``create_app`` builds
    from ``settings``."""
    from fastapi.testclient import TestClient

    from oran_adapt.db.base import session_scope
    from oran_adapt.registry.onboarding import onboard_model

    historical = kpi_frame(300, shift=0.0, slope=2.0, seed=1)
    drifted = kpi_frame(300, shift=3.0, slope=1.2, seed=2)
    with TestClient(create_app(settings)) as client:
        state = client.app.state  # type: ignore[attr-defined]
        with session_scope(state.session_factory) as session:
            result = onboard_model(
                session, state.registry, state.model_handler,
                model_id=labels["model_id"],
                model=Ridge().fit(historical[FEATURES], historical[TARGET]),
                framework="sklearn", task_type="regressor", target_column=TARGET,
                dataset_id=labels["dataset_id"], training_frame=historical,
                drifted_frame=drifted, drifted_version=labels["data_version"],
                live_alias=settings.live_alias,
            )
    return result.mlflow_model_name, str(result.model_version)


def walk(http: Any, labels: dict[str, str], payload: dict[str, Any], *,
         first_percent: int, traffic: Callable[[], Any] | None, timeout_s: float) -> None:
    """Steps 2-8 over the API (``http`` is a TestClient or an httpx.Client)."""
    model_id = labels["model_id"]

    step(2, "The API is ready")
    ready = http.get("/api/v1/readiness")
    check(ready.status_code == 200, f"readiness {ready.status_code}: {ready.text}")
    view = http.get(f"/api/v1/models/{model_id}")
    check(view.status_code == 200 and view.json().get("live_version"),
          f"{model_id} is not onboarded: {view.text}")
    live = view.json()["live_version"]
    print(f"ready; {model_id} LIVE at version {live}")

    step(3, "A drift alert arrives as an Alertmanager webhook")
    response = http.post("/api/v1/adaptation/events/from/alertmanager", json=payload)
    check(response.status_code == 201, f"expected 201, got {response.status_code}: "
          f"{response.text}")
    body = response.json()
    print(f"{len(payload['alerts'])} alerts posted, {body['events']} firing")

    step(4, "The mapper turned it into a job")
    check(body["mapper"] == "alertmanager" and body["events"] == 1 and len(body["jobs"]) == 1,
          f"mapped: {body}")
    job = body["jobs"][0]
    print(f"job {job['job_id']} for {job['model_id']} ({job['status']})")

    step(5, "The job ran the pipeline to DELIVERING")
    deadline = time.monotonic() + timeout_s
    while job["status"] not in TERMINAL_STATUSES:
        check(time.monotonic() < deadline, f"job still {job['status']} after {timeout_s}s")
        time.sleep(1.0)
        job = http.get(f"/api/v1/adaptation/jobs/{job['job_id']}").json()
    result = job.get("result") or {}
    check(job["status"] == "COMPLETED" and result.get("outcome") == "DELIVERING",
          f"job {job['status']}, outcome {result.get('outcome')}: "
          f"{result.get('reason') or job.get('error')}")
    candidate = result["registered_version"]
    print(f"strategy {job['strategy']}; candidate version {candidate} registered; "
          f"LIVE still {live}")

    step(6, "The rollout is in CANARY at the policy's first step")
    rollout_id = result["rollout"]["rollout_id"]
    rollout = http.get(f"/api/v1/rollouts/{rollout_id}").json()
    check(rollout["state"] == "CANARY" and rollout["percent"] == first_percent
          and rollout["stable_version"] == live and rollout["candidate_version"] == candidate,
          f"rollout: {rollout}")
    after = http.get(f"/api/v1/models/{model_id}").json()["live_version"]
    check(after == live, f"LIVE moved to {after} before the canary finished")
    print(f"rollout {rollout_id}: {rollout['percent']}% to version {candidate}, "
          f"{100 - rollout['percent']}% to version {live}")

    step(7, "The serving side reports that same split")
    if traffic is None:
        print("skipped: the serving side is only reachable in-process (local mode)")
    else:
        split = traffic()
        check(split is not None and split.percent == first_percent
              and split.candidate == candidate and split.stable == live,
              f"deployment reports {split}")
        print(f"deployment: {split.percent}% to version {split.candidate}, rest to {split.stable}")

    step(8, "The same alert again is a duplicate, not a second job")
    again = http.post("/api/v1/adaptation/events/from/alertmanager", json=payload)
    check(again.status_code == 200, f"expected 200, got {again.status_code}: {again.text}")
    duplicate = again.json()["jobs"][0]
    check(duplicate["job_id"] == job["job_id"] and duplicate["duplicate"],
          f"duplicate: {duplicate}")
    print(f"200, job {duplicate['job_id']} (duplicate)")


def run_local() -> None:
    from fastapi.testclient import TestClient

    from oran_adapt.db.migrate import upgrade_to_head

    payload, labels = alert()
    first = first_canary_percent()
    # ignore_cleanup_errors: on Windows SQLite files can stay locked until the process exits.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        settings = Settings(
            _env_file=None,
            database_url=f"sqlite:///{Path(tmp, 'app.db').as_posix()}",
            artifact_workdir=str(Path(tmp, "work")),
            registry_backend="filesystem",
            registry_fs_root=str(Path(tmp, "registry")),
            artifact_store_root=str(Path(tmp, "artifacts")),
            deployment_backend="registry-alias",
            delivery_strategy="canary",
            delivery_policy_file=str(POLICY),
            drift_mappers={"alertmanager": str(MAPPER)},
            job_queue_backend="inline",  # one process: a job runs inside its request
            auth_enabled=False,  # a local walkthrough; docs/security covers API keys
            notification_dispatch_enabled=False,
            log_json=False,
            # The model is scikit-learn; naming its requirement spares MLflow inferring it (a
            # subprocess per saved model).
            mlflow_pip_requirements=["scikit-learn"],
        )
        upgrade_to_head(settings.database_url)
        step(1, "A model is onboarded and LIVE")
        name, version = onboard(settings, labels)
        print(f"{labels['model_id']} version {version} LIVE (filesystem registry)")
        app = create_app(settings)
        with TestClient(app) as client:
            walk(client, labels, payload, first_percent=first,
                 traffic=lambda: app.state.deployer.traffic(name), timeout_s=120)


def run_remote(base_url: str, skip_onboard: bool, timeout_s: float) -> None:
    import httpx

    payload, labels = alert()
    first = first_canary_percent()
    if skip_onboard:
        step(1, "A model is onboarded and LIVE (skipped: --skip-onboard)")
    else:
        step(1, "A model is onboarded and LIVE")
        _, version = onboard(get_settings(), labels)
        print(f"{labels['model_id']} version {version} LIVE")
    headers = {}
    if os.environ.get("ORAN_API_KEY"):
        headers[get_settings().auth_api_key_header] = os.environ["ORAN_API_KEY"]
    with httpx.Client(base_url=base_url, headers=headers, timeout=30.0) as http:
        walk(http, labels, payload, first_percent=first, traffic=None, timeout_s=timeout_s)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", help="a running stack instead of the in-process app")
    parser.add_argument("--skip-onboard", action="store_true",
                        help="with --base-url: the model is already onboarded")
    parser.add_argument("--timeout-s", type=float, default=600.0,
                        help="with --base-url: how long to wait for the job")
    args = parser.parse_args(argv)
    try:
        if args.base_url:
            run_remote(args.base_url, args.skip_onboard, args.timeout_s)
        else:
            run_local()
    except WalkthroughFailure as exc:
        print(f"\nWALKTHROUGH FAILED: {exc}")
        return 1
    print("\nWalkthrough complete: the drift alert produced a candidate serving a canary slice.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
