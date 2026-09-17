"""The MCP adapter preserves SDK outcomes and forwards lookup arguments."""
from __future__ import annotations

from unittest.mock import Mock

import pytest
from sdk.run_control.delivery_reconcile import DeliveryReconcileResult

from orcho_mcp.run_control.delivery_reconcile import reconcile_delivery_record


@pytest.mark.parametrize("blocker", [
    None, "no_delivery_commit_found", "already_recorded",
    "commit_mismatch", "commit_unreadable",
])
def test_sdk_result_and_arguments_are_preserved(monkeypatch, blocker):
    sdk_result = DeliveryReconcileResult(
        run_id="run-1", accepted=blocker is None, state="legacy_commit",
        commit_sha="abc123", blocker=blocker, reason="SDK reason",
        artifact_path="/runs/run-1/commit_decisions/run-1.json",
        terminal_outcome="done", release_verdict="REJECTED",
        notes=("SDK note",),
    )
    delegate = Mock(return_value=sdk_result)
    monkeypatch.setattr(
        "orcho_mcp.run_control.delivery_reconcile._sdk_reconcile_delivery_record",
        delegate,
    )

    result = reconcile_delivery_record(
        "run-1", operator=" Operator ", commit="ABC", note="verified",
        workspace="/workspace", runs_dir="/runs",
    )

    delegate.assert_called_once_with(
        "run-1", operator=" Operator ", commit="ABC", note="verified",
        workspace="/workspace", runs_dir="/runs", cwd=None,
    )
    assert result.model_dump() == sdk_result.to_dict()
