"""E2E mock smoke — implement-incomplete handoff → continue_with_waiver.

Exercises the full operator path for an ADR-0073 implement-incomplete
delivery and proves the typed delivery/waiver projection on the
``orcho_run_evidence(slice="errors")`` read-path agrees with the summary
``orcho_run_status().meta`` audit fields end-to-end:

    start feature --mock (implement leaves a subtask INCOMPLETE)
      → run pauses at status=awaiting_phase_handoff, trigger="incomplete"
      → orcho_phase_handoff_decide(action="continue_with_waiver", feedback=…)
      → orcho_run_resume(run_id)
      → run advances
      → orcho_run_evidence(slice="errors").implement_delivery shows
        delivery_status="waived", decided_by="operator",
        action="continue_with_waiver",
        waiver_id="implement:implement_handoff:1"
      → the same values are present in
        orcho_run_status().meta.phases.implement /
        meta.phase_handoff_waiver (summary status projection — no drift).

──────────────────────────────────────────────────────────────────────
PREFLIGHT (core-under-test provides the deterministic trigger)
──────────────────────────────────────────────────────────────────────
Core-under-test: the canonical workspace ``orcho-core`` checkout, i.e.
the ``$ORCHO_CORE_DEV`` path (``../orcho-core`` relative to the canonical
``orcho-mcp`` repo root). It is the resolved ``import sdk`` / ``pipeline``
/ ``agents`` source here, made so via ``pip install -e "$ORCHO_CORE_DEV"``
(NOT the read-only stable install under ``$HOME/.local/share/orcho-*``).

Reproducibility note: this suite runs from an isolated git worktree, so a
literal ``../orcho-core`` is NOT a sibling of this checkout — install the
core by its resolved path. Confirm the path under test with
``python -c "import agents.runtimes._strategy as m; print(m.__file__)"``;
it must resolve under ``$ORCHO_CORE_DEV``, not the stable install.

The ADR-0073 machinery is present in that core
(``pipeline/phases/builtin/subtask_dag_handoff.py``:
``IMPLEMENT_HANDOFF_ID = "implement:implement_handoff:1"``, trigger
``"incomplete"``, ``delivery_status`` resolution, and the
operator/auto ``phase_handoff_waiver`` breadcrumb).

The deterministic mock path that yields an *incomplete* delivery is now
also in that core: ``agents/runtimes/_strategy.py::
_mock_subtask_attestation`` is env-gated on
``ORCHO_MOCK_IMPLEMENT_INCOMPLETE``. When that env var is truthy the mock
emits a ``subtask_attestation`` with one criterion left ``met: false``,
so subtask_dag marks the subtask INCOMPLETE; with the feature implement
policy (``repair_attempts=1``) the repair re-emits the same unmet
attestation, the delivery stays incomplete, and — because
``auto_waiver_allowed`` is left at its default ``False`` — the run pauses
on the implement handoff instead of auto-waiving. With the env unset the
mock's default all-met behaviour is unchanged (that regression is covered
by a separate core unit test, not this E2E).

This smoke therefore runs unconditionally in the ``mcp_integration``
group: the test arms the trigger for the spawned subprocess via
``monkeypatch.setenv`` (the subprocess inherits it — ``spawn.py`` does
``os.environ.copy()``) and asserts the projection↔meta invariant.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

pytestmark = [pytest.mark.mcp_integration]

_HANDOFF_ID = "implement:implement_handoff:1"


async def _wait_status(run_id: str, wanted: set[str], timeout_s: float = 90.0) -> str:
    """Poll ``orcho_run_status`` until ``meta.status`` lands in ``wanted``."""
    from orcho_mcp.tools import orcho_run_status

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        snap = orcho_run_status(run_id)
        cur = (snap.meta or {}).get("status")
        if cur in wanted:
            return cur
        await asyncio.sleep(0.3)
    raise AssertionError(
        f"run {run_id} did not reach {sorted(wanted)} within {timeout_s}s"
    )


@pytest.mark.asyncio
async def test_implement_incomplete_waiver_projection_matches_meta(
    mock_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pause on implement-incomplete, waive via the real MCP decide+resume
    path, then assert the typed ``implement_delivery`` projection equals the
    summary ``meta`` audit fields (no drift)."""
    from orcho_mcp.tools import (
        orcho_phase_handoff_decide,
        orcho_run_evidence,
        orcho_run_resume,
        orcho_run_start,
        orcho_run_status,
    )

    # Arm the core-under-test's deterministic incomplete-delivery trigger
    # BEFORE spawning: the pipeline subprocess inherits this env
    # (spawn.py does ``os.environ.copy()``), so the mock leaves a subtask
    # criterion unmet and the implement delivery resolves to incomplete.
    monkeypatch.setenv("ORCHO_MOCK_IMPLEMENT_INCOMPLETE", "1")

    # auto_waiver_allowed is left at its default (False) so an exhausted
    # incomplete delivery PAUSES for an operator decision instead of
    # auto-waiving in-process.
    started = await orcho_run_start(
        task="implement-incomplete handoff smoke",
        project_dir=str(mock_project),
        mock=True,
        profile="feature",
        max_rounds=1,
    )
    run_id = started.run_id

    # The run must pause on the implement-incomplete handoff.
    assert (await _wait_status(run_id, {"awaiting_phase_handoff"})) == (
        "awaiting_phase_handoff"
    )

    paused = orcho_run_status(run_id)
    meta = paused.meta or {}
    impl = (meta.get("phases") or {}).get("implement") or {}
    assert impl.get("delivery_status") == "incomplete"
    handoff = meta.get("phase_handoff") or {}
    assert handoff.get("trigger") == "incomplete"
    assert handoff.get("id") == _HANDOFF_ID

    # Real MCP operator path: decide continue_with_waiver, then resume.
    await orcho_phase_handoff_decide(
        run_id,
        handoff_id=_HANDOFF_ID,
        action="continue_with_waiver",
        feedback="accept: ship with stub",
    )
    await orcho_run_resume(run_id)

    # The run advances off the pause and finalizes.
    final = await _wait_status(run_id, {"done", "failed", "halted"})
    assert final == "done"

    # ── MCP read-path: typed projection from the errors-rollup (T2) ──
    ev = orcho_run_evidence(run_id, slice="errors")
    assert ev.errors is not None
    d = ev.errors.implement_delivery
    assert d is not None
    assert d.delivery_status == "waived"
    assert d.decided_by == "operator"
    assert d.action == "continue_with_waiver"
    assert d.waiver_id == _HANDOFF_ID

    # ── Status summary: orcho_run_status().meta carries the same audit fields ──
    post = orcho_run_status(run_id).meta or {}
    post_impl = (post.get("phases") or {}).get("implement") or {}
    post_waiver = post.get("phase_handoff_waiver") or {}
    assert post_impl.get("delivery_status") == "waived"
    assert post_impl.get("action") == "continue_with_waiver"
    assert post_impl.get("waiver_id") == _HANDOFF_ID
    assert post_waiver.get("decided_by") == "operator"

    # End-to-end no-drift: the typed projection equals the status summary audit.
    assert d.delivery_status == post_impl.get("delivery_status")
    assert d.action == post_impl.get("action")
    assert d.waiver_id == post_impl.get("waiver_id")
    assert d.decided_by == post_waiver.get("decided_by")

    # ── Blocking-id no-drift: WHICH subtasks were accepted under the waiver ──
    # Only this L4 path proves the real chain (core collector → evidence.json →
    # MCP projection) carries the ids; unit tests mock the errors rollup. The
    # armed mock leaves one criterion unmet, so the subtask is INCOMPLETE (it
    # produced a receipt; the attestation just did not close) — it lands in
    # ``incomplete_subtasks``, NOT ``missing_subtask_receipts``.
    assert d.incomplete_subtasks, (
        "the incomplete subtask id must survive core → evidence → MCP"
    )
    assert d.incomplete_subtasks == (post_impl.get("incomplete_subtasks") or [])
    assert d.missing_subtask_receipts == (
        post_impl.get("missing_subtask_receipts") or []
    )
