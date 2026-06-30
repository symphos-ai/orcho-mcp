"""SILENT vs TERMINAL parity for MCP consumer-visible state.

Mission: prove that MCP-level read tools return equivalent consumer-
visible state regardless of whether the underlying orcho-core pipeline
ran with ``presentation=TERMINAL`` or ``PresentationPolicy.SILENT``.

This is the executable form of the SILENT-migration win condition
spelled out in
``../../orcho-core/docs/plans/2026-05-27-stdout-event-gap-register.md``:

    "Запустить mock/fixture run в normal terminal mode.
     Запустить эквивалентный run в SILENT.
     Сравнивать не точный stdout, а consumer-visible state."

The four MCP read tools we pin here cover the complete consumer
surface for a live progress UI:

  * ``orcho_run_status`` — status summary + metrics snapshot.
  * ``orcho_run_metrics`` — phase/cost breakdown.
  * ``orcho_run_events_tail`` — full event stream.
  * ``orcho_run_events_summary`` — bounded per-phase rollup.

If a future change leaks an event or artifact only when TERMINAL is in
effect, this test trips. If the SILENT path drops a kind or attribute
that the TERMINAL path emits, the equivalence assertion fails fast.

Inventory finding that motivates this test:
MCP source has zero stdout-parsing dependencies (every read goes
through ``events.jsonl`` and persisted artifacts via the SDK). So the
SILENT migration is structurally a no-op for MCP code itself — the
test exists to *prove* that, and to catch regressions if either side
of the boundary drifts.

Out of scope (deliberately not asserted):
  * Byte-equality of timestamps / cost numbers between two distinct
    runs.
  * Exact ``seq`` ordering inside event payloads — two real runs
    against the same task produce two independent timelines; we
    compare structural shape (kinds present, phases covered, status
    final), not bit-for-bit equality.
  * Stdout / stderr text content (the whole point is presentation
    decoupling).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from orcho_mcp.observe.summary import build_run_events_summary
from orcho_mcp.services.run_reads import (
    get_run_events_tail,
    get_run_metrics,
    get_run_status,
)

# ── helpers ──────────────────────────────────────────────────────────


@pytest.fixture
def tmp_project(tmp_path: Path) -> Path:
    """Minimal initialised git checkout — the pipeline reads git state
    on some phases; ``task`` profile is the cheapest path."""
    from tests.conftest import init_git_repo
    project = tmp_path / "proj"
    init_git_repo(project)
    return project


def _drive_run(
    *,
    workspace: Path,
    project_dir: Path,
    run_id: str,
    presentation: object,
    no_interactive: bool,
) -> Path:
    """Run a single mock pipeline directly against ``run_project_pipeline``.

    Pre-mints ``run_id`` via ``ORCHO_RUN_ID`` env (the same mechanism
    the async pilot uses) so the resulting run lands at
    ``<workspace>/runspace/runs/<run_id>/`` and is resolvable by the
    standard read tools.
    """
    import os

    from agents.runtimes import MockAgentProvider
    from pipeline.plugins import PluginConfig
    from pipeline.project.app import run_project_pipeline
    from pipeline.project.types import ProjectRunRequest

    output_dir = workspace / "runspace" / "runs" / run_id

    # Patch load_plugin to skip filesystem walks (pipeline path-resolver
    # interacts with workspace config; the test workspace has none).
    from unittest.mock import patch

    previous = os.environ.get("ORCHO_RUN_ID")
    os.environ["ORCHO_RUN_ID"] = run_id
    try:
        with patch(
            "pipeline.project.session_run.load_plugin",
            return_value=PluginConfig(),
        ):
            request = ProjectRunRequest(
                task="silent vs terminal parity smoke",
                project_dir=str(project_dir),
                output_dir=output_dir,
                max_rounds=1,
                profile_name="task",
                provider=MockAgentProvider(latency=0.0),
                presentation=presentation,
                no_interactive=no_interactive,
            )
            run_project_pipeline(request)
    finally:
        if previous is None:
            os.environ.pop("ORCHO_RUN_ID", None)
        else:
            os.environ["ORCHO_RUN_ID"] = previous

    return output_dir


# ── 1. Both modes produce a discoverable, completed run ──────────────


def test_silent_and_terminal_runs_both_resolve_via_mcp_read_path(
    fake_workspace: Path,
    tmp_project: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """Smoke: same mock task, two policies, both runs land under
    ``<ws>/runspace/runs/`` and the MCP read tools resolve them by
    ``run_id``. Pre-flight for the per-tool parity tests below — if
    this fails, every other parity assertion is meaningless."""
    from pipeline.project.types import PresentationPolicy

    terminal_id = "20260527_terminal"
    silent_id = "20260527_silent_"  # underscore avoids same-second collision

    terminal_dir = _drive_run(
        workspace=fake_workspace,
        project_dir=tmp_project,
        run_id=terminal_id,
        presentation=PresentationPolicy.TERMINAL,
        no_interactive=True,
    )
    out = capsys.readouterr()
    # TERMINAL run prints the legacy transcript to stdout.
    assert out.out != "", (
        "TERMINAL run must produce stdout transcript "
        "(presentation policy regression?)"
    )

    silent_dir = _drive_run(
        workspace=fake_workspace,
        project_dir=tmp_project,
        run_id=silent_id,
        presentation=PresentationPolicy.SILENT,
        no_interactive=True,
    )
    out = capsys.readouterr()
    # SILENT run produces nothing on stdout/stderr.
    assert out.out == "", (
        f"SILENT run leaked stdout ({len(out.out)} chars): "
        f"{out.out[:200]!r}"
    )
    assert out.err == "", (
        f"SILENT run leaked stderr: {out.err[:200]!r}"
    )

    # Both run dirs exist on disk with the persisted contracts.
    assert (terminal_dir / "meta.json").is_file()
    assert (silent_dir / "meta.json").is_file()
    assert (terminal_dir / "events.jsonl").is_file()
    assert (silent_dir / "events.jsonl").is_file()


# ── 2. orcho_run_status parity ──────────────────────────────────────


def test_status_consumer_state_equivalent_across_presentations(
    fake_workspace: Path,
    tmp_project: Path,
) -> None:
    """``orcho_run_status`` exposes status summary + metrics; the consumer-
    visible fields (final status, halt_reason shape, presence of
    failure block) must be equivalent regardless of presentation."""
    from pipeline.project.types import PresentationPolicy

    _drive_run(
        workspace=fake_workspace, project_dir=tmp_project,
        run_id="20260527_term_status",
        presentation=PresentationPolicy.TERMINAL, no_interactive=True,
    )
    _drive_run(
        workspace=fake_workspace, project_dir=tmp_project,
        run_id="20260527_sil_status_",
        presentation=PresentationPolicy.SILENT, no_interactive=True,
    )

    terminal = get_run_status("20260527_term_status")
    silent = get_run_status("20260527_sil_status_")

    # Final status — the load-bearing consumer field.
    assert terminal.meta["status"] == "done"
    assert silent.meta["status"] == "done"

    # halt_reason — None / absent on the done path for both.
    assert terminal.meta.get("halt_reason") in (None, "")
    assert silent.meta.get("halt_reason") in (None, "")

    # Failure block — absent on the done path for both.
    assert terminal.meta.get("failure") in (None, {})
    assert silent.meta.get("failure") in (None, {})

    # Top-level meta key surface should be structurally the same.
    # Two distinct runs WILL differ on timestamp / session_ts; what
    # we pin is which fields a consumer can read.
    terminal_keys = set(terminal.meta.keys())
    silent_keys = set(silent.meta.keys())
    # Both must expose at least the consumer-load-bearing set.
    consumer_keys = {"status", "task", "project", "profile"}
    assert consumer_keys <= terminal_keys, (
        f"TERMINAL meta missing consumer keys "
        f"{consumer_keys - terminal_keys}"
    )
    assert consumer_keys <= silent_keys, (
        f"SILENT meta missing consumer keys "
        f"{consumer_keys - silent_keys}"
    )


# ── 3. orcho_run_metrics parity ─────────────────────────────────────


def test_metrics_consumer_state_equivalent_across_presentations(
    fake_workspace: Path,
    tmp_project: Path,
) -> None:
    """``orcho_run_metrics`` reads metrics.json. The structural shape
    (which fields are present) must be equivalent across modes — exact
    cost numbers differ between runs and are deliberately not pinned."""
    from pipeline.project.types import PresentationPolicy

    _drive_run(
        workspace=fake_workspace, project_dir=tmp_project,
        run_id="20260527_term_metrics",
        presentation=PresentationPolicy.TERMINAL, no_interactive=True,
    )
    _drive_run(
        workspace=fake_workspace, project_dir=tmp_project,
        run_id="20260527_sil_metrics_",
        presentation=PresentationPolicy.SILENT, no_interactive=True,
    )

    terminal = get_run_metrics("20260527_term_metrics")
    silent = get_run_metrics("20260527_sil_metrics_")

    # Both metrics payloads should be populated dicts.
    assert isinstance(terminal.metrics, dict)
    assert isinstance(silent.metrics, dict)
    # Whichever keys metrics.json carries, the set should match — a
    # missing key under one mode would be a real consumer regression.
    assert set(terminal.metrics.keys()) == set(silent.metrics.keys()), (
        f"metrics shape drifts across presentations:\n"
        f"  TERMINAL keys: {sorted(terminal.metrics.keys())}\n"
        f"  SILENT keys:   {sorted(silent.metrics.keys())}"
    )


# ── 4. orcho_run_events_tail parity ─────────────────────────────────


def test_events_tail_kinds_equivalent_across_presentations(
    fake_workspace: Path,
    tmp_project: Path,
) -> None:
    """events.jsonl is the structural event store — never gated by
    presentation per the file-sink invariant. The set of event KINDS
    emitted by the same mock task must match exactly across modes;
    if a kind appears under TERMINAL but not SILENT, the SILENT
    boundary leaked an event-store write."""
    from pipeline.project.types import PresentationPolicy

    _drive_run(
        workspace=fake_workspace, project_dir=tmp_project,
        run_id="20260527_term_events",
        presentation=PresentationPolicy.TERMINAL, no_interactive=True,
    )
    _drive_run(
        workspace=fake_workspace, project_dir=tmp_project,
        run_id="20260527_sil_events_",
        presentation=PresentationPolicy.SILENT, no_interactive=True,
    )

    # Read the full event stream via the MCP-level path (which goes
    # through ``sdk.list_events`` — the same path live consumers use).
    terminal = get_run_events_tail(
        "20260527_term_events", since_seq=0, limit=10_000,
    )
    silent = get_run_events_tail(
        "20260527_sil_events_", since_seq=0, limit=10_000,
    )

    terminal_kinds = {e.kind for e in terminal.events}
    silent_kinds = {e.kind for e in silent.events}

    # The canonical spine MUST appear in both — file + event sinks
    # are never gated by presentation policy.
    spine = {"run.start", "phase.start", "phase.end", "run.end"}
    assert spine <= terminal_kinds, (
        f"TERMINAL events missing spine {spine - terminal_kinds}"
    )
    assert spine <= silent_kinds, (
        f"SILENT events missing spine {spine - silent_kinds} — this "
        f"is the load-bearing observability regression that blocks "
        f"the whole MCP migration"
    )

    # The full kinds set must match — a kind appearing only under
    # TERMINAL would prove a stdout-side emit that doesn't reach the
    # file sink under SILENT.
    assert terminal_kinds == silent_kinds, (
        f"event kinds diverge across presentations:\n"
        f"  TERMINAL-only: {terminal_kinds - silent_kinds}\n"
        f"  SILENT-only:   {silent_kinds - terminal_kinds}\n"
        f"This is the SILENT regression the gap register exists to "
        f"catch — every event-emitting branch must be reachable "
        f"under both policies."
    )

    # ``next_seq`` is roughly comparable — two independent runs can
    # differ slightly if mock timing nudges retry counts, but the
    # order of magnitude must agree (no SILENT run should drop >25%
    # of TERMINAL's events).
    assert abs(terminal.next_seq - silent.next_seq) <= max(
        5, terminal.next_seq // 4,
    ), (
        f"event count diverges substantially: "
        f"TERMINAL next_seq={terminal.next_seq}, "
        f"SILENT next_seq={silent.next_seq}"
    )


# ── 5. orcho_run_events_summary parity ──────────────────────────────


def test_events_summary_phase_shape_equivalent_across_presentations(
    fake_workspace: Path,
    tmp_project: Path,
) -> None:
    """``orcho_run_events_summary`` is what a live progress UI reads:
    current_phase, by_kind aggregation, per-phase buckets. The set of
    phases covered + each phase's kind set must match across modes."""
    from pipeline.project.types import PresentationPolicy

    _drive_run(
        workspace=fake_workspace, project_dir=tmp_project,
        run_id="20260527_term_summary",
        presentation=PresentationPolicy.TERMINAL, no_interactive=True,
    )
    _drive_run(
        workspace=fake_workspace, project_dir=tmp_project,
        run_id="20260527_sil_summary_",
        presentation=PresentationPolicy.SILENT, no_interactive=True,
    )

    # ``build_run_events_summary`` enforces a 1000-event ceiling on
    # ``limit``; a mock task run emits well under that, so the
    # ceiling-limit query effectively returns the whole stream.
    terminal = build_run_events_summary(
        "20260527_term_summary", since_seq=0, limit=1000, last_n=0,
    )
    silent = build_run_events_summary(
        "20260527_sil_summary_", since_seq=0, limit=1000, last_n=0,
    )

    # Final consumer status — both done.
    assert terminal.status == "done"
    assert silent.status == "done"

    # The per-phase coverage set must match. Two independent runs
    # walk the same phase DAG for ``profile=task``, so the set of
    # phase keys must be identical.
    terminal_phases = {p.phase for p in terminal.by_phase}
    silent_phases = {p.phase for p in silent.by_phase}
    assert terminal_phases == silent_phases, (
        f"per-phase coverage diverges:\n"
        f"  TERMINAL-only phases: {terminal_phases - silent_phases}\n"
        f"  SILENT-only phases:   {silent_phases - terminal_phases}"
    )

    # For each phase, the set of event kinds observed must match —
    # the kind set is the live progress UI's contract for "what
    # happened in this phase".
    def _phase_kinds(summary, phase: str) -> set[str]:
        for entry in summary.by_phase:
            if entry.phase == phase:
                return set(entry.kinds)
        return set()

    for phase in terminal_phases:
        t_kinds = _phase_kinds(terminal, phase)
        s_kinds = _phase_kinds(silent, phase)
        assert t_kinds == s_kinds, (
            f"phase {phase!r} kind set diverges across presentations:\n"
            f"  TERMINAL-only kinds: {t_kinds - s_kinds}\n"
            f"  SILENT-only kinds:   {s_kinds - t_kinds}"
        )


# ── 6. Gap-register witness ─────────────────────────────────────────


def test_gap_register_candidate_events_not_required_by_current_consumers(
    fake_workspace: Path,
    tmp_project: Path,
) -> None:
    """Witness for the gap register's candidate events — confirms that
    none of them are emitted by the current mock pipeline and the
    MCP read tools still produce complete consumer-visible state
    without them. If a future migration introduces any of these
    events, this test trips and the gap-register row should flip from
    "candidate" to "shipped".

    Candidates per
    ``../../orcho-core/docs/plans/2026-05-27-stdout-event-gap-register.md``:
      * agent.notice, run.notice, phase.notice
      * phase.parse_failed, phase.output_ready, usage.snapshot

    This test is intentionally a *witness*, not an enforcer — if a
    real MCP consumer use case appears that needs one of these
    events, the right move is to add it, ship it, and flip the row.
    """
    from pipeline.project.types import PresentationPolicy

    _drive_run(
        workspace=fake_workspace, project_dir=tmp_project,
        run_id="20260527_gap_witness",
        presentation=PresentationPolicy.SILENT, no_interactive=True,
    )

    tail = get_run_events_tail(
        "20260527_gap_witness", since_seq=0, limit=10_000,
    )
    observed = {e.kind for e in tail.events}

    candidates = {
        "agent.notice",
        "run.notice",
        "phase.notice",
        "phase.parse_failed",
        "phase.output_ready",
        "usage.snapshot",
    }

    # If any candidate is emitted by the current pipeline, the
    # gap-register row should be flipped from "proposed" to
    # "shipped" in the same diff.
    leaked_into_pipeline = observed & candidates
    assert not leaked_into_pipeline, (
        f"gap-register candidate events now reach events.jsonl: "
        f"{sorted(leaked_into_pipeline)}. Flip the matching rows "
        f"in 2026-05-27-stdout-event-gap-register.md and update "
        f"core/observability/event_kinds.py to make them required."
    )

    # And the consumer-visible state still completes — meta.status,
    # canonical spine, run.end status all populated without any of
    # the candidates needing to fire.
    status = get_run_status("20260527_gap_witness")
    assert status.meta["status"] == "done"
    assert {"run.start", "phase.start", "phase.end", "run.end"} <= observed
