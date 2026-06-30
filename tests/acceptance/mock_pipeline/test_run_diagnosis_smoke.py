"""Acceptance smoke for the core-delegated run diagnosis path.

Sibling to ``test_orcho_run_via_tool.py``: that test proves the MCP
tool-handler ``orcho_run_start`` → read round-trip works against a real
``--mock`` subprocess. This one closes the remaining L4 gap for the
*diagnosis* surface — it drives a full ``--mock`` run through the live
MCP tool functions (``orcho_run_start`` → poll ``orcho_run_status`` →
``orcho_run_diagnose``) and asserts that ``orcho_run_diagnose`` returns
a valid, typed ``RunDiagnosis`` (NOT an error) whose continuation fields
are projected from the *same* ``services.run_lineage`` resolver that
backs ``orcho_run_status`` — i.e. the diagnosis classification is
delegated to core's read-model and never drifts from the lineage
projection.

Marked ``mcp_integration`` so the default suite stays fast; it spawns a
real subprocess pipeline.
"""
from __future__ import annotations

import asyncio
import time
import typing
from pathlib import Path

import pytest

pytestmark = pytest.mark.mcp_integration


@pytest.mark.asyncio
async def test_run_diagnose_delegates_to_core_lineage(
    mock_project: Path,
) -> None:
    """start(mock) → status(done) → diagnose, all via the live tool surface.

    Asserts the delegated, non-error path: a typed ``RunDiagnosis`` with a
    ``condition`` in the model's ``Literal`` set (``resume_inert_terminal``
    for a done run), typed-or-``None`` continuation fields, a well-formed
    ``recovery_lineage`` when present, and continuation_subject /
    recommended_next_action that match the recovery-lineage projection of
    the same run (single resolver).
    """
    from orcho_mcp.schemas.run_control import RunDiagnosis
    from orcho_mcp.schemas.shared import (
        ContinuationSubjectLiteral,
        RecommendedNextActionLiteral,
        RecoveryLineage,
    )
    from orcho_mcp.services.run_lineage import project_recovery_lineage
    from orcho_mcp.tools import (
        orcho_run_diagnose,
        orcho_run_start,
        orcho_run_status,
    )

    started = await orcho_run_start(
        task="diagnosis smoke — say hello",
        project_dir=str(mock_project),
        mock=True,
        max_rounds=1,
    )
    run_id = started.run_id
    assert started.pid > 0

    # Poll status until the pipeline reaches a terminal state.
    deadline = time.monotonic() + 90
    final_status: str | None = None
    while time.monotonic() < deadline:
        snap = orcho_run_status(run_id)
        cur = (snap.meta or {}).get("status")
        if cur in ("done", "failed", "interrupted", "halted"):
            final_status = cur
            break
        await asyncio.sleep(0.3)

    if final_status is None:
        pytest.fail("run did not reach a terminal status within 90s")
    # The normal mock-pipeline outcome is a clean ``done``.
    assert final_status == "done", f"unexpected final status: {final_status}"

    # --- The delegated, non-error diagnosis path. ------------------------
    diag = orcho_run_diagnose(run_id)
    assert isinstance(diag, RunDiagnosis), (
        f"diagnose returned {type(diag)!r}, expected a typed RunDiagnosis"
    )
    assert diag.run_id == run_id

    # ``condition`` must be one of the model's typed Literal values; for a
    # terminal-success (done) run the deterministic classification is
    # ``resume_inert_terminal``.
    condition_values = set(
        typing.get_args(RunDiagnosis.model_fields["condition"].annotation)
    )
    assert diag.condition in condition_values, (
        f"condition {diag.condition!r} is not in the typed set {condition_values}"
    )
    assert diag.condition == "resume_inert_terminal", (
        f"done run expected resume_inert_terminal, got {diag.condition!r}"
    )

    # Continuation fields: either ``None`` or a member of the typed vocab.
    subject_values = set(typing.get_args(ContinuationSubjectLiteral))
    action_values = set(typing.get_args(RecommendedNextActionLiteral))
    assert diag.continuation_subject is None or (
        diag.continuation_subject in subject_values
    ), f"continuation_subject {diag.continuation_subject!r} not typed"
    assert diag.recommended_next_action is None or (
        diag.recommended_next_action in action_values
    ), f"recommended_next_action {diag.recommended_next_action!r} not typed"

    # When recovery lineage is projected, its fields must be well-formed.
    # ``None`` is acceptable for the resume_inert_terminal branch (a clean
    # done run carries no source/child lineage to continue).
    if diag.recovery_lineage is not None:
        lineage = diag.recovery_lineage
        assert isinstance(lineage, RecoveryLineage)
        assert isinstance(lineage.source_resumable, bool)
        assert isinstance(lineage.plan_subject_available, bool)
        assert isinstance(lineage.missing_facts, list)
        assert lineage.source_run_id is None or isinstance(
            lineage.source_run_id, str
        )

    # --- Single-resolver consistency. -----------------------------------
    # ``orcho_run_diagnose`` and the recovery-lineage projection read the
    # same ``services.run_lineage`` resolver, so the typed continuation
    # subject / next action must agree for the same run.
    projection = project_recovery_lineage(run_id)
    assert diag.continuation_subject == projection.continuation_subject, (
        "continuation_subject drifted between diagnose and lineage projection"
    )
    assert diag.recommended_next_action == projection.recommended_next_action, (
        "recommended_next_action drifted between diagnose and lineage projection"
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
