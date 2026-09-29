"""Operator CLI for the O-RAN model adaptation framework.

Database migration, data versioning, model onboarding and submitting a drift event, all against
the same database / MLflow registry the API uses (configured through the usual settings / .env).

    python -m oran_adapt.cli db upgrade
    python -m oran_adapt.cli data ingest --dataset kpi --version v1 --csv train.csv
    python -m oran_adapt.cli data lineage --dataset kpi --version v1
    python -m oran_adapt.cli data register --dataset kpi --version v3         --uri s3://kpi-bucket/2026/09/cells.parquet --timestamp-column ts
    python -m oran_adapt.cli data verify --dataset kpi --version v3
    python -m oran_adapt.cli cdc trigger-sql --table cell_kpi --dialect sqlite         --columns id,cell,ts,prb_util
    python -m oran_adapt.cli model onboard --model-id m1 --model-file m1.joblib \\
        --framework sklearn --task-type classifier --target label \\
        --dataset kpi --training-csv train.csv
    python -m oran_adapt.cli model show --model-id m1
    python -m oran_adapt.cli event submit --model-id m1 --dataset kpi --drifted-version v2
    python -m oran_adapt.cli auth new-key --role OPERATOR --name noc-dashboard
    python -m oran_adapt.cli cdc run --mode polling --once
    python -m oran_adapt.cli cdc materialize --dataset kpi
    python -m oran_adapt.cli data current --model-id m1
    python -m oran_adapt.cli notifications dispatch
    python -m oran_adapt.cli notifications list --status DEAD
    python -m oran_adapt.cli notifications redrive --sink webhook
    python -m oran_adapt.cli worker run --classes default,gpu
    python -m oran_adapt.cli worker run-job --job-id 3f2a...
    python -m oran_adapt.cli jobs list --status QUEUED
    python -m oran_adapt.cli jobs cancel --job-id 3f2a...
    python -m oran_adapt.cli jobs reap

``--model-file`` is loaded with joblib (i.e. unpickled): only pass files you trust.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from typing import Any

import pandas as pd

from oran_adapt.core.config import Settings, get_settings, load_settings
from oran_adapt.core.errors import AdaptationError, ConfigurationError
from oran_adapt.core.frameworks import ADAPTABLE_FRAMEWORKS


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _session_factory(settings: Settings):
    from oran_adapt.db.base import create_db_engine, make_session_factory

    return make_session_factory(create_db_engine(settings.database_url))


def _registry(settings: Settings):
    from oran_adapt.bootstrap import build_registry

    return build_registry(settings)


def _model_handler(settings: Settings):
    from oran_adapt.bootstrap import build_model_handler

    return build_model_handler(settings)


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


def _data_access(settings: Settings):
    from oran_adapt.bootstrap import build_data_access

    return build_data_access(settings)


def _cmd_data_register(args, settings: Settings) -> Any:
    from oran_adapt.db.base import session_scope

    access = _data_access(settings)
    with session_scope(_session_factory(settings)) as session:
        info = access.register(
            session,
            args.dataset,
            args.version,
            args.uri,
            kind=args.kind,
            fmt=args.format,
            timestamp_column=args.timestamp_column,
            parent_version=args.parent,
            model_id=args.model_id,
            model_version=args.model_version,
            role=args.role,
            actor="cli",
        )
        return info.as_dict()


def _cmd_data_verify(args, settings: Settings) -> Any:
    from oran_adapt.datastore.versioning import version_row
    from oran_adapt.db.base import session_scope

    access = _data_access(settings)
    with session_scope(_session_factory(settings)) as session:
        result = access.verify(session, version_row(session, args.dataset, args.version))
    if not result["matches"]:
        from oran_adapt.core.errors import DataSourceChangedError

        raise DataSourceChangedError(
            f"data version '{args.version}' no longer matches its content hash", **result
        )
    return result


def _cmd_data_rows(args, settings: Settings) -> Any:
    from oran_adapt.datastore.versioning import version_row
    from oran_adapt.db.base import session_scope

    access = _data_access(settings)
    with session_scope(_session_factory(settings)) as session:
        dv = version_row(session, args.dataset, args.version)
        rows = access.page(session, dv, offset=args.offset, limit=args.limit)
        return [{"observed_at": r.observed_at, "record_key": r.record_key, "payload": r.payload}
                for r in rows]


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


def _cmd_data_current(args, settings: Settings) -> Any:
    from oran_adapt.datastore.current_data import get_current_data, list_current_data
    from oran_adapt.db.base import session_scope

    with session_scope(_session_factory(settings)) as session:
        if args.id:
            found = get_current_data(session, args.id)
            if found is None:
                raise AdaptationError(f"current data '{args.id}' not found")
            return found
        return list_current_data(
            session, limit=settings.api_pagination_default_limit, model_id=args.model_id
        )


def _cmd_cdc_run(args, settings: Settings) -> Any:
    from oran_adapt.cdc import run_cdc, run_cdc_once

    if args.mode:
        # Re-validated, so the adapter's required keys are checked for the new mode too.
        settings = load_settings(**{**settings.model_dump(), "cdc_mode": args.mode})
    factory = _session_factory(settings)
    if args.once:
        return run_cdc_once(factory, settings)
    return run_cdc(factory, settings, max_batches=args.max_batches)


def _cmd_cdc_materialize(args, settings: Settings) -> Any:
    from oran_adapt.cdc import materialize_cdc
    from oran_adapt.db.base import session_scope

    with session_scope(_session_factory(settings)) as session:
        info = materialize_cdc(
            session, args.dataset, max_tx_ids=settings.cdc_max_tx_ids_per_version
        )
        if info is None:
            return {"dataset_id": args.dataset, "materialized": False, "reason": "no pending events"}
        return {"materialized": True, **info.as_dict()}


def _cmd_cdc_trigger_sql(args, settings: Settings) -> Any:
    from oran_adapt.cdc.triggers import trigger_sql

    statements = trigger_sql(
        args.table or settings.cdc_polling_table,
        dialect=args.dialect,
        key_column=args.key_column or settings.cdc_key_column,
        columns=[c for c in (args.columns or "").split(",") if c],
        json_columns=[c for c in (args.json_columns or "").split(",") if c],
    )
    print(";\n\n".join(statements) + ";")
    return None


def _cmd_notifications_dispatch(args, settings: Settings) -> Any:
    """Deliver the outbox from this process (beside, or instead of, the API's dispatcher)."""
    import threading

    from oran_adapt.bootstrap import build_notifiers
    from oran_adapt.notifications.dispatcher import Dispatcher

    sinks = build_notifiers(settings)
    if not sinks:
        raise ConfigurationError(
            "NOTIFICATION_BACKEND=none: there is no sink to deliver to",
            key="NOTIFICATION_BACKEND",
        )
    dispatcher = Dispatcher(_session_factory(settings), sinks, settings)
    if args.once:
        return {"worker_id": dispatcher.worker_id,
                "processed": dispatcher.drain(max_rounds=args.max_rounds)}
    stop = threading.Event()
    try:
        dispatcher.run_forever(stop)
    except KeyboardInterrupt:
        stop.set()
    return {"worker_id": dispatcher.worker_id, "stopped": True}


def _cmd_notifications_list(args, settings: Settings) -> Any:
    from oran_adapt.db.base import session_scope
    from oran_adapt.notifications.service import get_delivery, list_deliveries

    with session_scope(_session_factory(settings)) as session:
        if args.id is not None:
            return get_delivery(session, args.id)
        return list_deliveries(
            session, limit=args.limit or settings.api_pagination_default_limit,
            status=args.status, sink=args.sink, event_type=args.event_type,
            subject=args.subject,
        )


def _cmd_notifications_redrive(args, settings: Settings) -> Any:
    import getpass

    from oran_adapt.db.base import session_scope
    from oran_adapt.notifications.service import redrive, redrive_dead

    actor = f"cli:{getpass.getuser()}"
    with session_scope(_session_factory(settings)) as session:
        if args.id is not None:
            return redrive(session, args.id, actor=actor)
        return redrive_dead(
            session, actor=actor, limit=args.limit or settings.api_pagination_default_limit,
            sink=args.sink, event_type=args.event_type, subject=args.subject,
        )


def _cmd_model_onboard(args, settings: Settings) -> Any:
    import joblib

    from oran_adapt.core.integrity import check_size
    from oran_adapt.db.base import session_scope
    from oran_adapt.registry.onboarding import onboard_model

    # A trusted, operator-supplied local file (joblib can run code on load: never point this at
    # a file from an untrusted source). The size check runs before anything is deserialized.
    check_size(args.model_file, settings.artifact_max_bytes)
    model = joblib.load(args.model_file)
    with session_scope(_session_factory(settings)) as session:
        result = onboard_model(
            session,
            _registry(settings),
            _model_handler(settings),
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
    from oran_adapt.registry.publishing import describe_versions

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
        "versions": describe_versions(_registry(settings), name),
        "data_links": links,
    }


def _cmd_event_submit(args, settings: Settings) -> Any:
    from oran_adapt.bootstrap import build_llm
    from oran_adapt.core.schemas import DriftEvent
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
        llm_client=build_llm(settings),
        workdir=settings.artifact_workdir,
    ).model_dump(mode="json")


def _classes(value: str | None) -> list[str] | None:
    return [c.strip() for c in value.split(",") if c.strip()] if value else None


def _cmd_worker_run(args, settings: Settings) -> Any:
    """Claim and run queued jobs until stopped (SIGTERM, Ctrl+C, Ctrl+Break drain it)."""
    from oran_adapt.orchestrator.worker import install_drain_handlers, worker_from_settings

    worker = worker_from_settings(settings, classes=_classes(args.classes))
    install_drain_handlers(worker)
    ran = worker.run(once=args.once, max_jobs=args.max_jobs)
    return {"worker": worker.owner, "classes": worker.classes, "jobs_run": ran}


def _cmd_worker_run_job(args, settings: Settings) -> Any:
    """One attempt of one job: what a broker message (celery, rq, a Kubernetes Job) runs."""
    from oran_adapt.orchestrator.worker import install_drain_handlers, worker_from_settings

    worker = worker_from_settings(settings)
    install_drain_handlers(worker)
    return {"worker": worker.owner, "job_id": args.job_id, "ran": worker.run_job(args.job_id)}


def _cmd_jobs_list(args, settings: Settings) -> Any:
    from oran_adapt.core.enums import JobStatus
    from oran_adapt.db.base import session_scope
    from oran_adapt.orchestrator.jobs import list_jobs

    with session_scope(_session_factory(settings)) as session:
        return list_jobs(
            session, limit=args.limit or settings.api_pagination_default_limit,
            status=JobStatus(args.status) if args.status else None, model_id=args.model_id,
            tenant=args.tenant, quarantined=True if args.quarantined else None,
        )


def _cmd_jobs_cancel(args, settings: Settings) -> Any:
    import getpass

    from oran_adapt.orchestrator.jobs import request_cancel

    response, immediate = request_cancel(
        _session_factory(settings), settings, args.job_id, actor=f"cli:{getpass.getuser()}"
    )
    return {**response.model_dump(mode="json"), "cancelled_now": immediate}


def _cmd_jobs_reap(args, settings: Settings) -> Any:
    """One reaper pass: requeue expired leases, time out overdue queued jobs, republish."""
    from oran_adapt.bootstrap import build_job_queue
    from oran_adapt.orchestrator.worker import reap

    return reap(_session_factory(settings), settings, build_job_queue(settings))


def _delivery(settings: Settings):
    from oran_adapt.delivery.controller import Delivery

    return Delivery.from_settings(settings, _registry(settings))


def _cmd_rollout_tick(args, settings: Settings) -> Any:
    """One controller pass over every active rollout (what the worker does every
    ROLLOUT_TICK_S); with --rollout-id, just that one."""
    from oran_adapt.db.base import session_scope
    from oran_adapt.delivery import controller

    with session_scope(_session_factory(settings)) as session:
        if args.rollout_id:
            return {args.rollout_id: controller.tick(session, _delivery(settings),
                                                     args.rollout_id)}
        return controller.tick_all(session, _delivery(settings))


def _cmd_rollout_list(args, settings: Settings) -> Any:
    from oran_adapt.db.base import session_scope
    from oran_adapt.delivery import controller

    with session_scope(_session_factory(settings)) as session:
        rows = controller.list_rollouts(
            session, model_id=args.model_id, state=args.state,
            active=True if args.active else None,
            limit=args.limit or settings.api_pagination_default_limit,
        )
        return [controller.to_dict(r) for r in rows]


def _cmd_rollout_decide(args, settings: Settings) -> Any:
    import getpass

    from oran_adapt.db.base import session_scope
    from oran_adapt.delivery import controller

    act = controller.approve if args.cmd == "approve" else controller.reject
    with session_scope(_session_factory(settings)) as session:
        row = act(session, _delivery(settings), args.rollout_id,
                  actor=f"cli:{getpass.getuser()}", reason=args.reason)
        return controller.to_dict(row)


def _cmd_auth_new_key(args, settings: Settings) -> Any:
    """A new random API key (or, with --stdin, the key read from standard input) and the
    API_KEYS entry that grants it a role. Only the digest goes into configuration; the key itself
    is shown once, here, and stored nowhere."""
    import secrets

    from oran_adapt.adapters.access import hash_api_key
    from oran_adapt.core.enums import Role

    key = sys.stdin.readline().strip() if args.stdin else secrets.token_urlsafe(32)
    if len(key) < 16:
        raise AdaptationError("an API key must be at least 16 characters")
    role = Role(args.role).value
    spec = f"{role}:{args.name}" if args.name else role
    body: dict[str, object] = {"api_keys_entry": {hash_api_key(key): spec}}
    if not args.stdin:
        body["api_key"] = key
    return body


def _cmd_config_lint(args, settings: Settings | None) -> Any:
    from oran_adapt.core.config import lint_config_file

    problems = [p for path in args.files for p in lint_config_file(path)]
    if problems:
        raise ConfigurationError(
            f"{len(problems)} problem(s) in config file(s)", problems=problems
        )
    return {"ok": True, "files": args.files}


def _cmd_config_effective(args, settings: Settings) -> Any:
    from oran_adapt.core.config_sources import effective_config

    return effective_config(settings)


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
    c = data.add_parser(
        "register", help="register a file / URL / object-store URI as a data version, uncopied"
    )
    c.add_argument("--dataset", required=True)
    c.add_argument("--version", required=True)
    c.add_argument("--uri", required=True, help="read by a DATASET_BACKENDS adapter")
    c.add_argument("--format", choices=["csv", "jsonl", "parquet"],
                   help="default: from the URI's extension")
    c.add_argument("--kind", choices=["HISTORICAL", "DRIFTED"], default="HISTORICAL")
    c.add_argument("--timestamp-column")
    c.add_argument("--parent")
    c.add_argument("--model-id")
    c.add_argument("--model-version")
    c.add_argument("--role", choices=["TRAINING", "VALIDATION", "DRIFT_OBSERVED"])
    c.set_defaults(fn=_cmd_data_register)
    c = data.add_parser("verify", help="re-read a version and recompute its content hash")
    c.add_argument("--dataset", required=True)
    c.add_argument("--version", required=True)
    c.set_defaults(fn=_cmd_data_verify)
    c = data.add_parser("rows", help="print a page of a version's rows")
    c.add_argument("--dataset", required=True)
    c.add_argument("--version", required=True)
    c.add_argument("--offset", type=int, default=0)
    c.add_argument("--limit", type=int, default=20)
    c.set_defaults(fn=_cmd_data_rows)
    c = data.add_parser("list")
    c.add_argument("--dataset")
    c.set_defaults(fn=_cmd_data_list)
    c = data.add_parser("lineage")
    c.add_argument("--dataset", required=True)
    c.add_argument("--version", required=True)
    c.set_defaults(fn=_cmd_data_lineage)
    c = data.add_parser("current", help="show the CurrentData adaptation jobs decided on")
    c.add_argument("--id", help="one current_data_id (default: the latest, newest first)")
    c.add_argument("--model-id")
    c.set_defaults(fn=_cmd_data_current)

    cdc = top.add_parser("cdc").add_subparsers(dest="cmd", required=True)
    c = cdc.add_parser("run", help="consume CDC events from the configured source (CDC_MODE)")
    c.add_argument("--mode", help="override CDC_MODE (an installed cdc_source adapter)")
    c.add_argument("--once", action="store_true", help="process one batch and exit")
    c.add_argument("--max-batches", type=int, help="stop after this many batches")
    c.set_defaults(fn=_cmd_cdc_run)
    c = cdc.add_parser("materialize", help="fold pending CDC events into a new data version")
    c.add_argument("--dataset", required=True)
    c.set_defaults(fn=_cmd_cdc_materialize)
    c = cdc.add_parser(
        "trigger-sql", help="print changelog trigger DDL for a source table (to review, apply)"
    )
    c.add_argument("--table", help="default: CDC_POLLING_TABLE")
    c.add_argument("--dialect", choices=["sqlite", "postgresql"], required=True)
    c.add_argument("--key-column", help="default: CDC_KEY_COLUMN")
    c.add_argument("--columns", help="comma-separated column list (required for sqlite)")
    c.add_argument("--json-columns", help="columns holding JSON text (sqlite)")
    c.set_defaults(fn=_cmd_cdc_trigger_sql)

    notes = top.add_parser("notifications").add_subparsers(dest="cmd", required=True)
    c = notes.add_parser("dispatch", help="deliver the notification outbox to the sinks")
    c.add_argument("--once", action="store_true", help="drain what is due now and exit")
    c.add_argument("--max-rounds", type=int, default=100,
                   help="with --once: stop after this many polls")
    c.set_defaults(fn=_cmd_notifications_dispatch)
    for name, fn, text in (
        ("list", _cmd_notifications_list, "show deliveries (newest first) or one by --id"),
        ("redrive", _cmd_notifications_redrive, "re-queue DEAD deliveries (one by --id)"),
    ):
        c = notes.add_parser(name, help=text)
        c.add_argument("--id", type=int)
        c.add_argument("--sink")
        c.add_argument("--event-type")
        c.add_argument("--subject", help="the job id")
        c.add_argument("--limit", type=int)
        if name == "list":
            c.add_argument("--status", choices=["PENDING", "DELIVERED", "DEAD"])
        c.set_defaults(fn=fn)

    model = top.add_parser("model").add_subparsers(dest="cmd", required=True)
    c = model.add_parser("onboard", help="register a trusted local joblib model + training data")
    c.add_argument("--model-id", required=True)
    c.add_argument("--model-file", required=True)
    c.add_argument("--framework", required=True, choices=sorted(ADAPTABLE_FRAMEWORKS))
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
    c = event.add_parser("submit", help="queue an adaptation job for a drift event")
    c.add_argument("--model-id", required=True)
    c.add_argument("--dataset")
    c.add_argument("--drifted-version")
    c.add_argument("--event-id")
    c.set_defaults(fn=_cmd_event_submit)

    worker = top.add_parser("worker").add_subparsers(dest="cmd", required=True)
    c = worker.add_parser("run", help="claim and run queued adaptation jobs")
    c.add_argument("--classes", help="comma-separated worker classes (JOB_WORKER_CLASSES)")
    c.add_argument("--once", action="store_true", help="stop when nothing is claimable")
    c.add_argument("--max-jobs", type=int)
    c.set_defaults(fn=_cmd_worker_run)
    c = worker.add_parser("run-job", help="run one attempt of one queued job")
    c.add_argument("--job-id", required=True)
    c.set_defaults(fn=_cmd_worker_run_job)

    jobs_ = top.add_parser("jobs").add_subparsers(dest="cmd", required=True)
    c = jobs_.add_parser("list", help="adaptation jobs, newest first")
    c.add_argument("--status")
    c.add_argument("--model-id")
    c.add_argument("--tenant")
    c.add_argument("--quarantined", action="store_true")
    c.add_argument("--limit", type=int)
    c.set_defaults(fn=_cmd_jobs_list)
    c = jobs_.add_parser("cancel", help="cancel a queued or running job")
    c.add_argument("--job-id", required=True)
    c.set_defaults(fn=_cmd_jobs_cancel)
    c = jobs_.add_parser("reap", help="one reaper pass over the job queue")
    c.set_defaults(fn=_cmd_jobs_reap)

    rollout = top.add_parser("rollout").add_subparsers(dest="cmd", required=True)
    c = rollout.add_parser("tick", help="advance active rollouts once (the worker does this "
                           "every ROLLOUT_TICK_S)")
    c.add_argument("--rollout-id")
    c.set_defaults(fn=_cmd_rollout_tick)
    c = rollout.add_parser("list", help="progressive rollouts, newest first")
    c.add_argument("--model-id")
    c.add_argument("--state")
    c.add_argument("--active", action="store_true")
    c.add_argument("--limit", type=int)
    c.set_defaults(fn=_cmd_rollout_list)
    for name, text in (("approve", "approve a rollout awaiting approval"),
                       ("reject", "stop an active rollout; traffic returns to stable")):
        c = rollout.add_parser(name, help=text)
        c.add_argument("--rollout-id", required=True)
        c.add_argument("--reason", default="")
        c.set_defaults(fn=_cmd_rollout_decide)

    auth = top.add_parser("auth").add_subparsers(dest="cmd", required=True)
    c = auth.add_parser("new-key", help="make an API key and its API_KEYS settings entry")
    c.add_argument(
        "--role", required=True, choices=["ADMIN", "OPERATOR", "ML_ENGINEER", "READ_ONLY"]
    )
    c.add_argument("--name", help="who the key is for (shown as the audit actor)")
    c.add_argument("--stdin", action="store_true", help="hash a key read from stdin instead")
    c.set_defaults(fn=_cmd_auth_new_key)

    config = top.add_parser("config").add_subparsers(dest="cmd", required=True)
    c = config.add_parser("lint", help="validate config file(s) against the settings schema")
    c.add_argument("files", nargs="+", metavar="FILE")
    # Checks the files on their own, so a broken environment cannot stop the lint.
    c.set_defaults(fn=_cmd_config_lint, needs_settings=False)
    c = config.add_parser("effective", help="every key's effective value (redacted) and source")
    c.set_defaults(fn=_cmd_config_effective)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # MLflow prints emoji run links when talking to an HTTP server; on Windows a redirected
    # stdout defaults to cp1252 and that print would crash the run. Never fail on a log line.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        settings = get_settings() if getattr(args, "needs_settings", True) else None
        result = args.fn(args, settings)
        if result is not None:
            _print(result)
    except AdaptationError as exc:
        _print(exc.to_dict())
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
