"""Audit trail: one append-only ``audit_log`` row per significant action (drift received, data
versioned, versions scored, decisions, training, sandbox runs, validation, registration,
promotion, rollback).

Rows are written on the caller's session, so an audit row commits or rolls back together with
the change it describes. Rows cannot be changed afterwards: the ORM refuses updates and deletes
(db.models) and migration 0004 adds database triggers that do the same."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from oran_adapt.core.correlation import get_correlation_id
from oran_adapt.core.enums import AuditAction
from oran_adapt.db.models import AuditLog


def record_audit(
    session: Session,
    action: AuditAction,
    *,
    component: str,
    actor: str = "system",
    job_id: str | None = None,
    model_id: str | None = None,
    model_version: str | None = None,
    decision: str | None = None,
    reason: str | None = None,
    metadata: dict[str, Any] | None = None,
    status: str = "OK",
) -> AuditLog:
    row = AuditLog(
        action=AuditAction(action).value,
        component=component,
        actor=actor,
        job_id=job_id,
        model_id=model_id,
        model_version=model_version,
        decision=decision,
        reason=reason,
        detail=metadata,
        status=status,
        correlation_id=get_correlation_id(),
    )
    session.add(row)
    return row
