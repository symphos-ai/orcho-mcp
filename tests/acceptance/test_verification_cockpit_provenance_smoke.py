"""Acceptance smoke for the durable scheduled-gate ledger MCP projection."""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.fixtures.mcp_workspace import write_run

pytestmark = pytest.mark.acceptance


def test_cockpit_and_timeline_read_the_same_durable_ledger(
    fake_workspace: Path,
) -> None:
    """The SDK ledger remains readable after its declaring plugin is absent."""
    from pipeline.verification_ledger import GateLedgerRow, GateTrailEvent
    from pipeline.verification_ledger_store import ScheduledGateLedger, write_ledger

    from orcho_mcp.inspection.evidence import inspect_run_evidence

    run_id = "20260716_ledger_smoke"
    run_dir = write_run(
        fake_workspace,
        run_id,
        meta={"task": "ledger smoke", "status": "done", "project": "/project"},
    )
    rows = (
        GateLedgerRow(
            gate="verify",
            hook="after_phase",
            phase="implement",
            timing="after_implement",
            run_mode="auto",
            gate_sets=(),
            condition="always",
            declared=True,
            selectable=True,
            selected=True,
            execution_policy="require",
            consequence="required_action",
            executor="engine",
            trigger="after_phase",
        ),
        GateLedgerRow(
            gate="verify",
            hook="before_delivery",
            phase="",
            timing="delivery",
            run_mode="auto",
            gate_sets=(),
            condition="always",
            declared=True,
            selectable=True,
            selected=True,
            execution_policy="warn",
            consequence="warning",
            executor="engine",
            trigger="pre_final",
        ),
    )
    ledger = ScheduledGateLedger(
        rows,
        trail=(
            GateTrailEvent(
                "verify",
                "after_phase",
                "implement",
                "execution",
                "pass",
                "completed",
                "verification_receipts/verify.json",
            ),
        ),
    ).finalize()
    write_ledger(run_dir, ledger)

    timeline = inspect_run_evidence(run_id, slice="verification_timeline")
    cockpit = inspect_run_evidence(run_id, slice="verification_cockpit")

    assert timeline.verification_timeline is not None
    assert cockpit.verification_cockpit is not None
    assert cockpit.verification_cockpit.model_dump() == timeline.verification_timeline.model_dump()
    assert [
        (row.command, row.hook, row.phase, row.disposition)
        for row in timeline.verification_timeline.rows
    ] == [
        ("verify", "after_phase", "implement", "executed_pass"),
        ("verify", "before_delivery", "", "residual_missing"),
    ]
    assert timeline.verification_timeline.rows[0].receipt_evidence is not None
    assert timeline.verification_timeline.rows[0].receipt_evidence.path == "verification_receipts/verify.json"
