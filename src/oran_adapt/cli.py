"""Operator CLI for the O-RAN model adaptation framework.

Database migration, data versioning, model onboarding and submitting a drift event, all against
the same database / MLflow registry the API uses (configured through the usual settings / .env).

    python -m oran_adapt.cli db upgrade
    python -m oran_adapt.cli data ingest --dataset kpi --version v1 --csv train.csv
    python -m oran_adapt.cli data lineage --dataset kpi --version v1
    python -m oran_adapt.cli model onboard --model-id m1 --model-file m1.joblib \\
        --framework sklearn --task-type classifier --target label \\
        --dataset kpi --training-csv train.csv
    python -m oran_adapt.cli model show --model-id m1
    python -m oran_adapt.cli event submit --model-id m1 --dataset kpi --drifted-version v2

``--model-file`` is loaded with joblib (i.e. unpickled): only pass files you trust.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from typing import Any

import pandas as pd

from oran_adapt.core.config import Settings, get_settings
from oran_adapt.core.errors import AdaptationError


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _session_factory(settings: Settings):
    from oran_adapt.db.base import create_db_engine, make_session_factory

    return make_session_factory(create_db_engine(settings.database_url))


def _registry(settings: Settings):
    from oran_adapt.registry.client import MlflowRegistry

    return MlflowRegistry(
        settings.mlflow_tracking_uri,
        settings.mlflow_registry_uri,
        skops_trusted_types=settings.mlflow_skops_trusted_types,
    )


def _cmd_db_upgrade(args, settings: Settings) -> Any:
    from oran_adapt.db.migrate import upgrade_to_head

    upgrade_to_head(settings.database_url)
    return {"database": "upgraded to head"}


def _cmd_data_create(args, settings: Settings) -> Any:
    from oran_adapt.datastore import get_or_create_dataset
    from oran_adapt.db.base import session_scope

    with session_scope(_session_factory(settings)) as session:
        ds = get_or_create_dataset(
            session, args.dataset, name=args.name, description=args.description
        )
        return {"dataset_id": ds.dataset_id, "name": ds.name}


def _cmd_data_ingest(args, settings: Settings) -> Any:
    from oran_adapt.datastore import ingest_version
    from oran_adapt.db.base import session_scope

    frame = pd.read_csv(args.csv)
    with session_scope(_session_factory(settings)) as session:
        info = ingest_version(
            session,
            args.dataset,
            args.version,
            frame,
            kind=args.kind,
            timestamp_column=args.timestamp_column,
            start=args.start,
            parent_version=args.parent,
            model_id=args.model_id,
            model_version=args.model_version,
            role=args.role,
            source=f"cli:{args.csv}",
        )
        return info.as_dict()


def _cmd_data_list(args, settings: Settings) -> Any:
    from oran_adapt.datastore import list_datasets, list_versions
    from oran_adapt.db.base import session_scope

    with session_scope(_session_factory(settings)) as session:
        if args.dataset:
            return [v.as_dict() for v in list_versions(session, args.dataset)]
        return list_datasets(session)


def _cmd_data_lineage(args, settings: Settings) -> Any:
    from oran_adapt.datastore import lineage
    from oran_adapt.db.base import session_scope

    with session_scope(_session_factory(settings)) as session:
        return lineage(session, args.dataset, args.version)


def _cmd_model_onboard(args, settings: Settings) -> Any:
    import joblib

    from oran_adapt.db.base import session_scope
    from oran_adapt.registry.onboarding import onboard_model

    model = joblib.load(args.model_file)  # trusted, operator-supplied local file
    with session_scope(_session_factory(settings)) as session:
        result = onboard_model(
            session,
            _registry(settings),
            model_id=args.model_id,
            model=model,
            framework=args.framework,
            task_type=args.task_type,
            target_column=args.target,
            dataset_id=args.dataset,
            training_frame=pd.read_csv(args.training_csv),
            training_version=args.training_version,
            timestamp_column=args.timestamp_column,
            mlflow_model_name=args.mlflow_name,
            live_alias=settings.live_alias,
        )
        return result.as_dict()


def _cmd_model_attach(args, settings: Settings) -> Any:
    from oran_adapt.db.base import session_scope
    from oran_adapt.registry.onboarding import attach_existing_model

    with session_scope(_session_factory(settings)) as session:
        return attach_existing_model(
            session,
            _registry(settings),
            model_id=args.model_id,
            mlflow_model_name=args.mlflow_name,
            framework=args.framework,
            task_type=args.task_type,
            target_column=args.target,
            live_alias=settings.live_alias,
            version=args.version,
            dataset_id=args.dataset,
            training_version=args.training_version,
        ).as_dict()


def _cmd_model_show(args, settings: Settings) -> Any:
    from sqlalchemy import select

    from oran_adapt.core.errors import ModelNotFoundError
    from oran_adapt.datastore import model_data_links
    from oran_adapt.db.base import session_scope
    from oran_adapt.db.models import ModelMetadata

    with session_scope(_session_factory(settings)) as session:
        meta = session.execute(
            select(ModelMetadata).where(ModelMetadata.model_id == args.model_id)
        ).scalar_one_or_none()
        if meta is None:
            raise ModelNotFoundError(f"model '{args.model_id}' is not onboarded")
        name = meta.mlflow_model_name
        links = model_data_links(session, args.model_id)
    return {
        "model_id": args.model_id,
        "mlflow_model_name": name,
        "versions": _registry(settings).describe_versions(name),
        "data_links": links,
    }


def _cmd_event_submit(args, settings: Settings) -> Any:
    from oran_adapt.core.schemas import DriftEvent
    from oran_adapt.llm.client import build_llm_client
    from oran_adapt.orchestrator.jobs import submit_adaptation_job

    event = DriftEvent(
        model_id=args.model_id,
        drift_detected=True,
        event_id=args.event_id,
        dataset_id=args.dataset,
        drifted_data_version=args.drifted_version,
    )
    return submit_adaptation_job(
        _session_factory(settings),
        event,
        settings,
        registry=_registry(settings),
        llm_client=build_llm_client(settings),
        workdir=settings.artifact_workdir,
    ).model_dump(mode="json")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="oran-adapt", description=__doc__.splitlines()[0])
    top = p.add_subparsers(dest="group", required=True)

    db = top.add_parser("db").add_subparsers(dest="cmd", required=True)
    db.add_parser("upgrade", help="apply all database migrations").set_defaults(fn=_cmd_db_upgrade)

    data = top.add_parser("data").add_subparsers(dest="cmd", required=True)
    c = data.add_parser("create-dataset")
    c.add_argument("--dataset", required=True)
    c.add_argument("--name")
    c.add_argument("--description")
    c.set_defaults(fn=_cmd_data_create)
    c = data.add_parser("ingest", help="store a CSV as an immutable data version")
    c.add_argument("--dataset", required=True)
    c.add_argument("--version", required=True)
    c.add_argument("--csv", required=True)
    c.add_argument("--kind", choices=["HISTORICAL", "DRIFTED"], default="HISTORICAL")
    c.add_argument("--timestamp-column")
    c.add_argument(
        "--start",
        type=datetime.fromisoformat,
        help="ISO time of the first row when there is no --timestamp-column (default: now; "
        "pass it to make re-ingesting the same CSV idempotent)",
    )
    c.add_argument("--parent")
    c.add_argument("--model-id")
    c.add_argument("--model-version")
    c.add_argument("--role", choices=["TRAINING", "VALIDATION", "DRIFT_OBSERVED"])
    c.set_defaults(fn=_cmd_data_ingest)
    c = data.add_parser("list")
    c.add_argument("--dataset")
    c.set_defaults(fn=_cmd_data_list)
    c = data.add_parser("lineage")
    c.add_argument("--dataset", required=True)
    c.add_argument("--version", required=True)
    c.set_defaults(fn=_cmd_data_lineage)

    model = top.add_parser("model").add_subparsers(dest="cmd", required=True)
    c = model.add_parser("onboard", help="register a trusted local joblib model + training data")
    c.add_argument("--model-id", required=True)
    c.add_argument("--model-file", required=True)
    c.add_argument("--framework", required=True, choices=["sklearn", "xgboost", "torch"])
    c.add_argument("--task-type", required=True, choices=["classifier", "regressor"])
    c.add_argument("--target", required=True)
    c.add_argument("--dataset", required=True)
    c.add_argument("--training-csv", required=True)
    c.add_argument("--training-version", default="v1")
    c.add_argument("--timestamp-column")
    c.add_argument("--mlflow-name")
    c.set_defaults(fn=_cmd_model_onboard)
    c = model.add_parser("attach", help="adopt a model already registered in MLflow")
    c.add_argument("--model-id", required=True)
    c.add_argument("--mlflow-name", required=True)
    c.add_argument("--framework", required=True)
    c.add_argument("--task-type", required=True, choices=["classifier", "regressor"])
    c.add_argument("--target", required=True)
    c.add_argument("--version")
    c.add_argument("--dataset")
    c.add_argument("--training-version")
    c.set_defaults(fn=_cmd_model_attach)
    c = model.add_parser("show")
    c.add_argument("--model-id", required=True)
    c.set_defaults(fn=_cmd_model_show)

    event = top.add_parser("event").add_subparsers(dest="cmd", required=True)
    c = event.add_parser("submit", help="run the adaptation pipeline for a drift event")
    c.add_argument("--model-id", required=True)
    c.add_argument("--dataset")
    c.add_argument("--drifted-version")
    c.add_argument("--event-id")
    c.set_defaults(fn=_cmd_event_submit)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # MLflow prints emoji run links when talking to an HTTP server; on Windows a redirected
    # stdout defaults to cp1252 and that print would crash the run. Never fail on a log line.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        _print(args.fn(args, get_settings()))
    except AdaptationError as exc:
        _print(exc.to_dict())
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
