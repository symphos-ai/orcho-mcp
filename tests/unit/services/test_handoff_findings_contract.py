"""Writer→reader contract for handoff findings (``meta.phase_handoff``).

The engine builds the paused-run payload with
``pipeline.run_state.handoff.build_handoff_payload``, which nests findings
under ``artifacts``. This reader used to look for them at the top level
only, so every verification-gate pause reached the operator as
``verdict: REJECTED`` with ``findings_summary: null`` — the severity, the
evidence body and the required fix the engine had already written were all
dropped on the floor.

These tests drive the reader from the *writer's own* payload builder rather
than a hand-written fixture, so a future change to either side has to break
here before it can silently break the operator's payload again.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from pipeline.run_state.handoff import build_handoff_payload

from orcho_mcp.observe.handoff_hints import build_handoff_hint
from orcho_mcp.schemas.observe import RunEventsSummary
from orcho_mcp.services.run_projection import project_handoff_read_model
from tests.fixtures.mcp_workspace import write_run

_GATE_FINDING = {
    "id": "verification_gate_unverifiable",
    "severity": "P3",
    "title": "Verification gate unverifiable",
    "body": "quant_tests exited 0 but its subject could not be compared",
    "required_fix": (
        "Fix the verification environment outside the agent or choose an "
        "explicit waiver."
    ),
    "failure_kind": "unverifiable",
}


def _gate_payload(**artifact_overrides: object) -> dict:
    """The payload an engine verification-gate pause actually persists."""
    artifacts: dict[str, object] = {
        "gate_command": "quant_tests",
        "gate_set": "quant",
        "findings": [_GATE_FINDING],
        "short_summary": "quant_tests: unverifiable",
    }
    artifacts.update(artifact_overrides)
    return build_handoff_payload(
        handoff_id="gate:quant_tests:1",
        phase="implement",
        handoff_type="human_feedback_on_reject",
        trigger="verification_gate_failed",
        verdict="REJECTED",
        approved=False,
        round_extras_key="repair_round",
        round_n=1,
        loop_max_rounds=1,
        available_actions=["continue_with_waiver", "halt"],
        artifacts=artifacts,
        last_output="quant_tests: unverifiable",
    )


def _write_paused_gate_run(workspace: Path, run_id: str, payload: dict) -> None:
    write_run(
        workspace, run_id,
        meta={
            "project": "/p/x",
            "status": "awaiting_phase_handoff",
            "task": "hedge",
            "phase_handoff": payload,
        },
        events=[{
            "seq": 1, "ts": "2026-08-26T14:57:18", "kind": "phase.start",
            "phase": "implement", "payload": {},
        }],
    )


def test_engine_written_gate_findings_reach_the_read_model(
    fake_workspace,
) -> None:
    """The builder nests findings under ``artifacts``; the reader finds them."""
    _write_paused_gate_run(fake_workspace, "20260826_gate", _gate_payload())

    read_model = project_handoff_read_model(
        "20260826_gate", current_phase="implement",
    )

    assert read_model.raw_findings == [_GATE_FINDING]


def test_gate_handoff_hint_summarises_what_was_rejected(fake_workspace) -> None:
    """``findings_summary`` must not be null for a payload that has findings."""
    _write_paused_gate_run(fake_workspace, "20260826_hint", _gate_payload())

    snap = RunEventsSummary(
        run_id="20260826_hint",
        total_count=1,
        next_seq=2,
        eof=True,
        status="awaiting_phase_handoff",
        current_phase="implement",
    )

    hint = build_handoff_hint("20260826_hint", snap)

    assert hint is not None
    assert hint.findings_summary == "P3: Verification gate unverifiable"
    assert [f.title for f in hint.findings] == ["Verification gate unverifiable"]


def test_top_level_findings_still_win(fake_workspace) -> None:
    """A payload that carries findings at the top level keeps that source."""
    payload = _gate_payload()
    payload["findings"] = [{"title": "top-level", "severity": "P1"}]

    _write_paused_gate_run(fake_workspace, "20260826_toplevel", payload)

    read_model = project_handoff_read_model(
        "20260826_toplevel", current_phase="implement",
    )

    assert read_model.raw_findings == [{"title": "top-level", "severity": "P1"}]


def test_empty_nested_findings_fall_through_to_evidence(
    fake_workspace, monkeypatch,
) -> None:
    """An empty nested list means "not embedded here", not "none exist"."""
    from orcho_mcp.services import run_projection

    monkeypatch.setattr(
        run_projection, "_sdk_list_findings",
        lambda *a, **k: [{"title": "from evidence", "severity": "P2"}],
    )
    _write_paused_gate_run(
        fake_workspace, "20260826_empty", _gate_payload(findings=[]),
    )

    read_model = project_handoff_read_model(
        "20260826_empty", current_phase="implement",
    )

    assert read_model.raw_findings == [{"title": "from evidence", "severity": "P2"}]


@pytest.mark.parametrize("artifacts", ["not-a-dict", None, 42])
def test_malformed_artifacts_never_raise(
    fake_workspace, monkeypatch, artifacts: object,
) -> None:
    """A corrupt payload degrades to the evidence fallback, never an error."""
    from orcho_mcp.services import run_projection

    monkeypatch.setattr(run_projection, "_sdk_list_findings", lambda *a, **k: [])
    payload = _gate_payload()
    payload["artifacts"] = artifacts

    _write_paused_gate_run(fake_workspace, "20260826_corrupt", payload)

    read_model = project_handoff_read_model(
        "20260826_corrupt", current_phase="implement",
    )

    assert read_model.raw_findings == []
