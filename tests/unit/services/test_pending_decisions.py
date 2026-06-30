"""Unit tests for the workspace pending-decisions projector (T4).

``project_pending_decisions`` recovers every run paused on
``status=awaiting_phase_handoff`` from the runs' durable artifacts
(``meta.json`` + ``phase_handoff_decisions/``) — never from the advisory
``mcp/state.json`` cache. These tests pin: only paused runs surface, the
per-row ``next_actions`` branch decide→resume on
``decision_artifact_exists`` (coherent with ``orcho_run_diagnose`` and the
resume pre-flight guard), the rows stay bounded (no raw findings / reviewer
output), the row limit truncates with a flag, and a corrupt run is skipped
instead of collapsing the scan.

They also pin the default *operator inbox* contract: only ``actionable``
rows (project exists under the resolved workspace root) are visible by
default, non-actionable rows (missing / temp / out-of-workspace) are hidden
but tallied in ``hidden_count`` + breakdown, and ``include_stale=True`` is a
forensic escape that returns the hidden rows with their real
``classification`` while leaving the counters unchanged. Crucially,
*workspace-valid beats temp*: a project under the workspace root is
actionable even though ``fake_workspace`` itself lives under a pytest temp
directory (the F1 regression).
"""
from __future__ import annotations

import pytest
from sdk import phase_handoff_decide

from orcho_mcp.services.pending_decisions import project_pending_decisions
from tests.fixtures.mcp_workspace import in_workspace_project, meta, write_run


def _paused_meta(
    project, *, handoff_id="validate_plan:plan_round:1", **handoff_extra,
):
    """Build an ``awaiting_phase_handoff`` meta for ``project``.

    ``project`` is required so each test states the classification it wants
    (an actionable in-workspace path, ``None`` / a missing path, a temp path,
    or an out-of-workspace path). The handoff body deliberately carries a huge
    reviewer ``last_output`` and a ``findings`` list — neither must leak onto
    the bounded row.
    """
    handoff = {
        "id": handoff_id,
        "phase": "validate_plan",
        "trigger": "rejected",
        "verdict": "REJECTED",
        "round": 1,
        "loop_max_rounds": 1,
        "available_actions": ["continue", "retry_feedback", "halt"],
        # A deliberately huge reviewer body — must NOT leak onto the row.
        "last_output": "X" * 5000,
        "findings": [{"severity": "P1", "title": "secret finding body"}],
    }
    handoff.update(handoff_extra)
    return meta(
        status="awaiting_phase_handoff", project=project, task="t",
        phase_handoff=handoff,
    )


@pytest.mark.asyncio
async def test_tool_is_registered_l2():
    """L2: the tool is registered and visible via in-process list_tools."""
    import orcho_mcp.tools  # noqa: F401 — ensures @mcp.tool registration ran
    from orcho_mcp.instance import mcp

    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    assert "orcho_workspace_pending_decisions" in names


def test_returns_only_paused_runs(fake_workspace):
    """A paused run surfaces; running / done runs are excluded."""
    proj = in_workspace_project(fake_workspace)
    write_run(fake_workspace, "20260101_000001", meta=_paused_meta(proj))
    write_run(
        fake_workspace, "20260101_000002",
        meta=meta(status="running", project=proj, task="t"),
    )
    write_run(
        fake_workspace, "20260101_000003",
        meta=meta(status="done", project=proj, task="t"),
    )

    result = project_pending_decisions()

    assert [r.run_id for r in result.runs] == ["20260101_000001"]
    assert result.returned_count == 1
    assert result.scanned_count == 3
    assert result.truncated is False
    row = result.runs[0]
    assert row.project == proj
    assert row.classification == "actionable"
    assert row.handoff_id == "validate_plan:plan_round:1"
    assert row.phase == "validate_plan"
    assert row.available_actions == ["continue", "retry_feedback", "halt"]


def test_no_decision_row_routes_to_decide(fake_workspace):
    """Without a recorded decision the row points at decide (operator input),
    carrying the available verbs in choices — never a ready resume."""
    proj = in_workspace_project(fake_workspace)
    write_run(fake_workspace, "20260101_000001", meta=_paused_meta(proj))

    [row] = project_pending_decisions().runs

    assert row.decision_artifact_exists is False
    assert "orcho_phase_handoff_decide" in row.suggested_next_action
    assert len(row.next_actions) == 1
    na = row.next_actions[0]
    assert na.tool == "orcho_phase_handoff_decide"
    assert na.kind == "operator_input_required"
    assert na.requires_operator_input is True
    assert na.choices == ["continue", "retry_feedback", "halt"]
    assert na.args == {
        "run_id": "20260101_000001",
        "handoff_id": "validate_plan:plan_round:1",
    }
    # No run_resume offered while no decision exists.
    assert all(a.tool != "orcho_run_resume" for a in row.next_actions)


def test_recorded_decision_row_routes_to_resume(fake_workspace):
    """Once a decision artifact exists the run stays paused but the row
    routes at a ready resume — coherent with T3 / the resume guard."""
    proj = in_workspace_project(fake_workspace)
    write_run(fake_workspace, "20260101_000001", meta=_paused_meta(proj))
    # ``continue`` records the decision without flipping status off paused.
    phase_handoff_decide(
        "20260101_000001", "validate_plan:plan_round:1", "continue", cwd=None,
    )

    [row] = project_pending_decisions().runs

    assert row.decision_artifact_exists is True
    assert "orcho_run_resume" in row.suggested_next_action
    assert len(row.next_actions) == 1
    na = row.next_actions[0]
    assert na.tool == "orcho_run_resume"
    assert na.kind == "ready_call"
    assert na.requires_operator_input is False
    assert na.optional is False
    assert na.args == {"run_id": "20260101_000001"}
    # No decide record once the decision is recorded.
    assert all(a.tool != "orcho_phase_handoff_decide" for a in row.next_actions)


def test_rows_carry_no_raw_bodies(fake_workspace):
    """The row is the bounded projection only — no raw reviewer output or
    findings bodies are reachable on the wire model."""
    proj = in_workspace_project(fake_workspace)
    write_run(fake_workspace, "20260101_000001", meta=_paused_meta(proj))

    [row] = project_pending_decisions().runs

    fields = set(row.model_dump().keys())
    assert "last_output" not in fields
    assert "findings" not in fields
    assert "raw_findings" not in fields
    # The huge reviewer body never appears anywhere on the serialised row.
    blob = row.model_dump_json()
    assert "X" * 100 not in blob
    assert "secret finding body" not in blob


def test_row_limit_truncates_with_flag(fake_workspace):
    """The returned rows are capped by ``limit`` and ``truncated`` is set
    when more paused runs exist than the cap allows."""
    proj = in_workspace_project(fake_workspace)
    for i in range(1, 6):
        write_run(
            fake_workspace, f"20260101_00000{i}",
            meta=_paused_meta(proj, handoff_id=f"validate_plan:plan_round:{i}"),
        )

    result = project_pending_decisions(limit=2)

    assert result.returned_count == 2
    assert len(result.runs) == 2
    assert result.truncated is True
    # Newest run ids first (descending by name).
    assert [r.run_id for r in result.runs] == [
        "20260101_000005", "20260101_000004",
    ]


def test_corrupt_run_is_skipped(fake_workspace):
    """A run with a corrupt meta.json is skipped, not fatal — the healthy
    paused run still surfaces and the scan counts both."""
    proj = in_workspace_project(fake_workspace)
    write_run(fake_workspace, "20260101_000001", meta=_paused_meta(proj))
    write_run(
        fake_workspace, "20260101_000002",
        meta_text="{not valid json",
    )

    result = project_pending_decisions()

    assert [r.run_id for r in result.runs] == ["20260101_000001"]
    # Both directories were examined even though one was unreadable.
    assert result.scanned_count == 2


def test_empty_workspace_returns_empty_bounded_result(fake_workspace):
    """No runs → an empty, well-shaped result (never an error)."""
    result = project_pending_decisions()

    assert result.runs == []
    assert result.returned_count == 0
    assert result.scanned_count == 0
    assert result.truncated is False
    assert result.hidden_count == 0
    assert result.hidden_missing_project_count == 0
    assert result.hidden_temp_project_count == 0
    assert result.hidden_out_of_workspace_count == 0


# ── default-inbox filtering, counters, forensic escape ───────────────────────


def _seed_mixed(fake_workspace, tmp_path):
    """Seed one actionable paused run plus one of each hidden class.

    Returns ``(actionable_run_id, temp_demo_path)``. The temp demo project is
    a *real existing* directory created as a sibling of ``fake_workspace``
    under the pytest temp tree (``/private/var/folders/.../pytest-*``) but
    outside the workspace root, so it classifies ``temp_project`` rather than
    ``out_of_workspace``. ``/usr`` is a real directory that exists, is not a
    temp root, and is not under the workspace → ``out_of_workspace``.
    """
    actionable = in_workspace_project(fake_workspace, "proj")
    temp_demo = tmp_path / "demo"  # under /private/var/folders + pytest-* path
    temp_demo.mkdir()

    # Newest id first → 000005 actionable leads the default view.
    write_run(
        fake_workspace, "20260101_000005",
        meta=_paused_meta(actionable, handoff_id="validate_plan:plan_round:5"),
    )
    write_run(
        fake_workspace, "20260101_000004",
        meta=_paused_meta(None, handoff_id="validate_plan:plan_round:4"),
    )
    write_run(
        fake_workspace, "20260101_000003",
        meta=_paused_meta(
            "/p/does-not-exist", handoff_id="validate_plan:plan_round:3",
        ),
    )
    write_run(
        fake_workspace, "20260101_000002",
        meta=_paused_meta(
            str(temp_demo), handoff_id="validate_plan:plan_round:2",
        ),
    )
    write_run(
        fake_workspace, "20260101_000001",
        meta=_paused_meta("/usr", handoff_id="validate_plan:plan_round:1"),
    )
    return "20260101_000005", str(temp_demo)


def test_workspace_valid_beats_temp_priority(fake_workspace, tmp_path):
    """F1 regression: a project under the (temp-rooted) workspace is
    actionable and default-visible, while a demo project under the same
    pytest temp tree but *outside* the workspace is hidden as temp."""
    actionable = in_workspace_project(fake_workspace, "proj")
    temp_demo = tmp_path / "demo"
    temp_demo.mkdir()
    write_run(
        fake_workspace, "20260101_000002",
        meta=_paused_meta(actionable, handoff_id="validate_plan:plan_round:2"),
    )
    write_run(
        fake_workspace, "20260101_000001",
        meta=_paused_meta(
            str(temp_demo), handoff_id="validate_plan:plan_round:1",
        ),
    )

    default = project_pending_decisions()

    # Only the in-workspace project is visible, classified actionable — even
    # though the workspace root itself lives under the pytest temp tree.
    assert [r.run_id for r in default.runs] == ["20260101_000002"]
    assert default.runs[0].classification == "actionable"
    assert default.runs[0].project == actionable
    assert default.hidden_count == 1
    assert default.hidden_temp_project_count == 1

    # The demo project is only the temp one — surfaced as temp under forensic.
    forensic = project_pending_decisions(include_stale=True)
    by_id = {r.run_id: r for r in forensic.runs}
    assert by_id["20260101_000001"].classification == "temp_project"
    assert by_id["20260101_000002"].classification == "actionable"


def test_workspace_orchestrator_sibling_project_is_actionable(
    tmp_path, monkeypatch,
):
    """Real workspace layout: project repos live beside the orchestrator.

    ``ORCHO_WORKSPACE`` points at ``<group>/workspace-orchestrator``, while a
    normal run's ``meta.project`` points at ``<group>/<repo>``. That sibling
    project is part of the same operator workspace and must remain visible in
    the default inbox, not be demoted to ``out_of_workspace``.
    """
    group = tmp_path / "project-group"
    workspace = group / "workspace-orchestrator"
    (workspace / "runspace" / "runs").mkdir(parents=True)
    project = group / "orcho-core"
    project.mkdir()
    monkeypatch.setenv("ORCHO_WORKSPACE", str(workspace))
    monkeypatch.delenv("ORCHO_RUNSPACE", raising=False)

    write_run(
        workspace, "20260101_000001",
        meta=_paused_meta(
            str(project), handoff_id="validate_plan:plan_round:1",
        ),
    )

    result = project_pending_decisions()

    assert [r.run_id for r in result.runs] == ["20260101_000001"]
    assert result.runs[0].classification == "actionable"
    assert result.hidden_count == 0
    assert result.hidden_out_of_workspace_count == 0


def test_default_hides_non_actionable_with_counters(fake_workspace, tmp_path):
    """The default view hides missing / temp / out-of-workspace rows and
    tallies them in ``hidden_count`` + a breakdown whose sum matches."""
    actionable_id, _ = _seed_mixed(fake_workspace, tmp_path)

    result = project_pending_decisions()

    # Only the actionable run is visible.
    assert [r.run_id for r in result.runs] == [actionable_id]
    assert result.runs[0].classification == "actionable"
    assert result.returned_count == 1
    assert result.scanned_count == 5

    # Breakdown: 2 missing (None + nonexistent), 1 temp, 1 out-of-workspace.
    assert result.hidden_missing_project_count == 2
    assert result.hidden_temp_project_count == 1
    assert result.hidden_out_of_workspace_count == 1
    assert result.hidden_count == 4
    assert result.hidden_count == (
        result.hidden_missing_project_count
        + result.hidden_temp_project_count
        + result.hidden_out_of_workspace_count
    )


def test_include_stale_returns_hidden_with_stable_counters(
    fake_workspace, tmp_path,
):
    """``include_stale=True`` returns the hidden rows with their real
    per-row classification, and the counters are identical to the default
    run over the same set of paused runs."""
    _seed_mixed(fake_workspace, tmp_path)

    default = project_pending_decisions()
    forensic = project_pending_decisions(include_stale=True)

    # Forensic returns every paused run, newest id first.
    assert [r.run_id for r in forensic.runs] == [
        "20260101_000005", "20260101_000004", "20260101_000003",
        "20260101_000002", "20260101_000001",
    ]
    by_id = {r.run_id: r.classification for r in forensic.runs}
    assert by_id == {
        "20260101_000005": "actionable",
        "20260101_000004": "missing_project",
        "20260101_000003": "missing_project",
        "20260101_000002": "temp_project",
        "20260101_000001": "out_of_workspace",
    }

    # Counters are computed by classification over the scan window and are
    # IDENTICAL whether or not the hidden rows are returned.
    assert forensic.hidden_count == default.hidden_count == 4
    assert (
        forensic.hidden_missing_project_count
        == default.hidden_missing_project_count
        == 2
    )
    assert (
        forensic.hidden_temp_project_count
        == default.hidden_temp_project_count
        == 1
    )
    assert (
        forensic.hidden_out_of_workspace_count
        == default.hidden_out_of_workspace_count
        == 1
    )


def test_actionable_rows_visible_newest_first_and_bounded(fake_workspace):
    """Valid workspace-project runs are visible by default, ordered
    newest-id-first among the visible rows, with no raw bodies on the wire."""
    proj = in_workspace_project(fake_workspace)
    for i in range(1, 4):
        write_run(
            fake_workspace, f"20260101_00000{i}",
            meta=_paused_meta(proj, handoff_id=f"validate_plan:plan_round:{i}"),
        )

    result = project_pending_decisions()

    assert [r.run_id for r in result.runs] == [
        "20260101_000003", "20260101_000002", "20260101_000001",
    ]
    assert all(r.classification == "actionable" for r in result.runs)
    assert result.hidden_count == 0
    # Bounded: no raw reviewer body / findings leak onto any visible row.
    blob = "".join(r.model_dump_json() for r in result.runs)
    assert "X" * 100 not in blob
    assert "secret finding body" not in blob
