"""Delegate delivery reconciliation and project the SDK's result unchanged."""
from __future__ import annotations

from sdk.run_control.delivery_reconcile import (
    reconcile_delivery_record as _sdk_reconcile_delivery_record,
)

from orcho_mcp.schemas.delivery_reconcile import DeliveryReconcileResult
from orcho_mcp.services.errors import map_sdk_errors


def reconcile_delivery_record(
    run_id: str,
    operator: str,
    commit: str | None = None,
    note: str | None = None,
    workspace: str | None = None,
    runs_dir: str | None = None,
) -> DeliveryReconcileResult:
    """Core owns commit discovery, refusal decisions, and durable writes."""
    with map_sdk_errors(run_id):
        result = _sdk_reconcile_delivery_record(
            run_id, operator=operator, commit=commit, note=note,
            workspace=workspace, runs_dir=runs_dir, cwd=None,
        )
    return DeliveryReconcileResult.model_validate(result.to_dict())
