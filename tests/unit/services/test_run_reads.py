"""Unit tests for direct run-read MCP tools (status / metrics /
events_tail / workspace_state).

Backed by ``orcho_mcp.services.run_reads``; the @mcp.tool handlers are
one-line shims. Tests call the handlers as plain Python functions.
"""
from __future__ import annotations

import pytest

from orcho_mcp.errors import RunNotFoundError
from orcho_mcp.schemas.read import PhaseCost, RunEconomics
from orcho_mcp.services.run_reads import project_run_economics
from orcho_mcp.tools import (
    orcho_run_events_summary,
    orcho_run_events_tail,
    orcho_run_live_status,
    orcho_run_metrics,
    orcho_run_status,
    orcho_workspace_state,
)
from orcho_mcp.workspace_state import state_path
from tests.fixtures.mcp_workspace import event, meta, metrics, write_run


def _ev(seq: int, kind: str = "phase.start", phase: str = "plan", **payload):
    return {"seq": seq, "ts": f"2026-01-01T00:00:{seq:02d}", "kind": kind,
            "phase": phase, "payload": payload}


# ── orcho_run_status ─────────────────────────────────────────────────────────

def test_status_returns_meta_and_metrics(fake_workspace):
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(status="done", project="/p/x", task="t"),
        metrics=metrics(total_tokens=42),
    )

    s = orcho_run_status("20260101_000001")
    assert s.run_id == "20260101_000001"
    assert s.meta["status"] == "done"
    assert s.metrics is not None and s.metrics["total_tokens"] == 42
    assert s.sub_runs == []


def test_status_does_not_compose_scheduled_gate_evidence(fake_workspace):
    """Status remains available when a separate evidence artifact is unreadable.

    ``orcho_run_status`` owns the durable meta/metrics snapshot. Scheduled-gate
    ledger parsing belongs to the explicit evidence timeline slice, so a ledger
    failure must not take down the high-frequency status surface.
    """
    run_dir = write_run(
        fake_workspace, "20260101_000009",
        meta=meta(status="done", project="/p/x", task="t"),
        metrics=metrics(total_tokens=42),
    )
    (run_dir / "scheduled_gate_ledger.json").write_text(
        '{"not": "the current ledger contract"}\n',
        encoding="utf-8",
    )

    s = orcho_run_status("20260101_000009")

    assert s.meta["status"] == "done"
    assert s.metrics is not None and s.metrics["total_tokens"] == 42


def test_status_current_subtask_none_without_active_subtask(fake_workspace):
    """A terminal run with no in-flight subtask returns
    ``current_subtask is None`` — the absence is not an error."""
    write_run(
        fake_workspace, "20260101_000010",
        meta=meta(status="done", project="/p/x", task="t"),
        events=[
            event(1, "run.start"),
            event(2, "phase.start", phase="implement"),
            event(3, "run.end", payload={"status": "done"}),
        ],
    )

    s = orcho_run_status("20260101_000010")
    assert s.current_subtask is None


def test_status_current_subtask_matches_live_status(fake_workspace):
    """An in-flight ``subtask_dag`` subtask surfaces on run_status with
    index / total / goal, identical to what ``orcho_run_live_status``
    reports for the same run (same observe derivation, no divergence)."""
    write_run(
        fake_workspace, "20260101_000011",
        meta=meta(status="running", project="/p/x", task="t"),
        events=[
            event(1, "phase.start", phase="implement"),
            event(2, "subtask.start", phase="implement", payload={
                "subtask_id": "T3",
                "index": 3,
                "total": 12,
                "goal": "Patch the target module",
            }),
        ],
    )

    s = orcho_run_status("20260101_000011")
    sub = s.current_subtask
    assert sub is not None
    assert sub.subtask_id == "T3"
    assert sub.index == 3
    assert sub.total == 12
    assert sub.goal == "Patch the target module"
    assert sub.state == "running"

    # Same run, same observe derivation → identical coordinate on live_status.
    card = orcho_run_live_status("20260101_000011")
    assert card.current_subtask == sub


def test_status_summarises_phase_bodies_by_default(fake_workspace):
    """Polling payload stays cheap: heavy phase bodies become size
    markers, the long task text truncates, but status/verdicts survive."""
    write_run(
        fake_workspace, "20260101_000002",
        meta={
            "status": "done",
            "project": "/p/x",
            "task": "T" * 4000,
            "phases": {
                "plan": [{"attempt": 1, "output": "PLAN " * 1000}],
                "implement": {
                    "output": "OUT " * 1000,
                    "implementation_receipts": [
                        {"subtask_id": "T1", "state": "done",
                         "criteria_report": [{"c": "y" * 500}]},
                    ],
                },
            },
        },
    )
    s = orcho_run_status("20260101_000002")
    assert s.meta["status"] == "done"
    assert len(s.meta["task"]) == 280
    assert s.meta["task_chars"] == 4000
    plan0 = s.meta["phases"]["plan"][0]
    assert "output" not in plan0 and plan0["output_chars"] > 1000
    impl = s.meta["phases"]["implement"]
    assert "output" not in impl
    assert "criteria_report" not in impl["implementation_receipts"][0]
    assert impl["implementation_receipts"][0]["state"] == "done"


def test_status_include_all_returns_full_bodies(fake_workspace):
    """``include=["all"]`` is the back-compat escape hatch — full meta."""
    raw_phases = {
        "plan": [{"attempt": 1, "output": "PLAN " * 1000}],
        "implement": {"output": "OUT " * 1000},
    }
    write_run(
        fake_workspace, "20260101_000003",
        meta={"status": "done", "project": "/p/x", "task": "t",
              "phases": raw_phases},
    )
    s = orcho_run_status("20260101_000003", include=["all"])
    assert s.meta["phases"]["plan"][0]["output"].startswith("PLAN")
    assert s.meta["phases"]["implement"]["output"].startswith("OUT")


def test_status_include_output_targets_implement_body(fake_workspace):
    write_run(
        fake_workspace, "20260101_000004",
        meta={"status": "done", "project": "/p/x", "task": "t",
              "phases": {
                  "plan": [{"attempt": 1, "output": "PLAN " * 1000}],
                  "implement": {"output": "OUT " * 1000},
              }},
    )
    s = orcho_run_status("20260101_000004", include=["output"])
    assert "output" in s.meta["phases"]["implement"]
    # plan markdown is a different family — still elided
    assert "output" not in s.meta["phases"]["plan"][0]


def test_status_sub_runs_for_cross(fake_workspace):
    run_dir = write_run(fake_workspace, "20260101_000001",
                        meta={"projects": {"alpha": "/p/a", "beta": "/p/b"},
                              "status": "done", "task": "cross"})
    (run_dir / "alpha").mkdir()
    (run_dir / "beta").mkdir()
    (run_dir / ".hidden").mkdir()

    s = orcho_run_status("20260101_000001")
    assert s.sub_runs == ["alpha", "beta"]


def test_status_raises_on_missing_run(fake_workspace):
    with pytest.raises(RunNotFoundError):
        orcho_run_status("does_not_exist")


def test_status_metrics_optional(fake_workspace):
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(status="running", project="/p/x", task="t"),
    )
    s = orcho_run_status("20260101_000001")
    assert s.metrics is None


# ── orcho_run_status — artefact map pass-through ────────────────────────────
#
# Parametrize the four (parsed_plan, diff) presence combinations. The SDK
# computes ``artefacts`` (see the orcho-core artefact-map decision); the
# MCP service is pure pass-through with the wire-side narrowing of
# ``kind`` to ``Literal``. Asserting end-to-end here pins both the SDK
# call and the wire-model construction in one test.


def _write_run_with_artefacts(
    fake_workspace, run_id: str, *, parsed_plan: bool, diff: bool,
):
    """Write a minimal run with optional parsed_plan.json / diff.patch."""
    run_dir = write_run(
        fake_workspace, run_id,
        meta=meta(status="running", project="/p/x", task="t"),
    )
    if parsed_plan:
        (run_dir / "parsed_plan.json").write_text("{\"x\": 1}", encoding="utf-8")
    if diff:
        (run_dir / "diff.patch").write_text(
            "diff --git a/x b/x\n+ hi\n", encoding="utf-8",
        )
    return run_dir


@pytest.mark.parametrize(
    ("parsed_plan", "diff_patch", "want_kinds"),
    [
        (False, False, ["evidence"]),
        (True,  False, ["parsed_plan", "evidence"]),
        (False, True,  ["diff", "evidence"]),
        (True,  True,  ["parsed_plan", "diff", "evidence"]),
    ],
    ids=["no-physical", "plan-only", "diff-only", "all-three"],
)
def test_status_artefacts_presence_matrix(
    fake_workspace,
    parsed_plan: bool,
    diff_patch: bool,
    want_kinds: list[str],
) -> None:
    """``RunStatus.artefacts`` lists every readable artefact for a run.

    Evidence is composable (always emitted, ``size_bytes=None``); the
    two physical artefacts (``parsed_plan`` / ``diff``) appear only
    when their respective files exist on disk and carry
    ``size_bytes`` from ``os.stat``.
    """
    run_id = "20260101_000001"
    _write_run_with_artefacts(
        fake_workspace, run_id,
        parsed_plan=parsed_plan, diff=diff_patch,
    )

    s = orcho_run_status(run_id)

    assert [a.kind for a in s.artefacts] == want_kinds, (
        f"artefact kinds drift for parsed_plan={parsed_plan}, "
        f"diff={diff_patch}"
    )

    # Evidence entry is always present and carries None size_bytes
    # (composable resource, assembled at read time).
    ev = next(a for a in s.artefacts if a.kind == "evidence")
    assert ev.uri == f"orcho://runs/{run_id}/evidence"
    assert ev.mime == "application/json"
    assert ev.size_bytes is None

    # Physical artefacts (when present) carry their on-disk size.
    if parsed_plan:
        plan = next(a for a in s.artefacts if a.kind == "parsed_plan")
        assert plan.uri == f"orcho://runs/{run_id}/parsed_plan.json"
        assert plan.mime == "application/json"
        assert plan.size_bytes is not None and plan.size_bytes > 0
    if diff_patch:
        diff = next(a for a in s.artefacts if a.kind == "diff")
        assert diff.uri == f"orcho://runs/{run_id}/diff.patch"
        assert diff.mime == "text/x-patch"
        assert diff.size_bytes is not None and diff.size_bytes > 0


def test_status_surfaces_worktree_block(fake_workspace):
    """meta.worktree round-trips through ``orcho_run_status``.

    ``RunStatus.meta`` is typed as ``dict[str, Any]`` precisely so new
    blocks like ``worktree`` flow through the wire without a schema
    change. This pin guards the wire shape claim: any future tightening
    of meta typing must not silently drop the worktree projection.
    """
    write_run(fake_workspace, "20260522_worktree",
              meta={
                  "project": "/p/x", "status": "done", "task": "t",
                  "worktree": {
                      "isolation": "per_run",
                      "path": "/runs/20260522_worktree/checkout",
                      "base_ref": "abcdef1234",
                      "branch_ref": "orcho/run/20260522_worktree",
                      "retention_until": "2026-05-29T00:00:00+00:00",
                  },
              })
    s = orcho_run_status("20260522_worktree")
    assert s.meta["worktree"]["isolation"] == "per_run"
    assert s.meta["worktree"]["branch_ref"] == "orcho/run/20260522_worktree"
    assert s.meta["worktree"]["base_ref"] == "abcdef1234"


def test_status_surfaces_worktree_off_with_degraded_reason(fake_workspace):
    """Degraded shape (mode='off' + degraded_reason) also flows through.

    Used when GWT-1 fails to create an isolated worktree (project not a
    git repo, target_path exists but not a registered orcho worktree).
    The wire must preserve the reason for the operator UI.
    """
    write_run(fake_workspace, "20260522_degraded",
              meta={
                  "project": "/p/x", "status": "done", "task": "t",
                  "worktree": {
                      "isolation": "off",
                      "path": "/p/x",
                      "base_ref": "",
                      "branch_ref": None,
                      "degraded_reason": (
                          "project_dir is not a git repository (no HEAD); "
                          "cannot create isolated worktree"
                      ),
                  },
              })
    s = orcho_run_status("20260522_degraded")
    assert s.meta["worktree"]["isolation"] == "off"
    assert "not a git repository" in s.meta["worktree"]["degraded_reason"]


class TestRunStatusGateDecisionPassthrough:
    """When a cross-run pauses on a manual_confirm gate the runner
    writes ``status="awaiting_gate_decision"`` plus a ``pending_gate``
    block into ``meta.json`` and exits with code 4. The MCP status
    surface must let the new status pass through to clients — the
    pending payload is the operator UI's hook for surfacing the
    decision.
    """

    def test_status_surfaces_awaiting_gate_decision(
        self, fake_workspace,
    ) -> None:
        write_run(
            fake_workspace,
            "20260514_gd1",
            meta={
                "task": "T",
                "projects": {"api": "/p/api", "web": "/p/web"},
                "profile": "manual_demo",
                "status": "awaiting_gate_decision",
                "phases": {},
                "pending_gate": {
                    "name": "contract_check",
                    "run_policy": "manual_confirm",
                    "choices": ["run", "skip"],
                    "on_skip": "allow_with_gap",
                },
            },
        )
        snap = orcho_run_status("20260514_gd1")
        assert snap.meta["status"] == "awaiting_gate_decision"
        pg = snap.meta["pending_gate"]
        assert pg["name"] == "contract_check"
        assert pg["choices"] == ["run", "skip"]
        assert pg["on_skip"] == "allow_with_gap"

    def test_skipped_contract_check_entry_passes_through(
        self, fake_workspace,
    ) -> None:
        """SKIPPED is a new contract_check.verdict value (manual_confirm
        skipped by operator, or policy_never / policy_disabled). The
        status surface must surface the entry verbatim; nothing in the
        chain should assume ``APPROVED | REJECTED`` only."""
        write_run(
            fake_workspace,
            "20260514_skip1",
            meta={
                "task": "T",
                "projects": {"api": "/p/api"},
                "status": "done",
                "phases": {
                    "contract_check": {
                        "api": {
                            "approved": False,
                            "verdict": "SKIPPED",
                            "skipped": True,
                            "skip_reason": "operator_decision",
                            "on_skip": "allow_with_gap",
                            "source": "operator",
                            "short_summary": "skipped by operator",
                            "operator_feedback": "Tiny docs change.",
                            "findings": [],
                            "risks": [],
                            "checks": [],
                        },
                    },
                },
            },
        )
        snap = orcho_run_status("20260514_skip1")
        cc = snap.meta["phases"]["contract_check"]["api"]
        assert cc["verdict"] == "SKIPPED"
        assert cc["skipped"] is True
        assert cc["skip_reason"] == "operator_decision"
        assert cc["operator_feedback"] == "Tiny docs change."


# ── orcho_run_metrics ────────────────────────────────────────────────────────

def test_metrics_returns_full_payload(fake_workspace):
    write_run(fake_workspace, "20260101_000001",
              meta={"project": "/p/x", "status": "done", "task": "t"},
              metrics={"total_tokens": 1234, "phases": {"plan": {"tokens_in": 1000}}})
    m = orcho_run_metrics("20260101_000001")
    assert m.run_id == "20260101_000001"
    assert m.metrics["total_tokens"] == 1234
    assert m.metrics["phases"]["plan"]["tokens_in"] == 1000


def test_metrics_raises_when_missing(fake_workspace):
    write_run(fake_workspace, "20260101_000001",
              meta={"project": "/p/x", "status": "running", "task": "t"})
    with pytest.raises(RunNotFoundError):
        orcho_run_metrics("20260101_000001")


# ── run economics projection (project_run_economics) ────────────────────────
#
# Thin typed view over the raw metrics.json dict — pure projection, no IO and
# no wire surface (not yet wired into any tool). retry_rate is the per-phase
# retry surplus normalised by phase count.


def test_economics_clean_run_zero_retry_rate():
    metrics = {
        "total_tokens": 4100,
        "total_duration_s": 142.3,
        "total_rounds": 1,
        "phases": {
            "plan": {"total_tokens": 8300, "duration_s": 12.3, "attempts": 1},
            "implement": {"total_tokens": 23500, "duration_s": 28.1, "attempts": 1},
            "review": {"total_tokens": 1900, "duration_s": 5.4, "attempts": 1},
        },
    }

    econ = project_run_economics(metrics)

    assert isinstance(econ, RunEconomics)
    assert econ.total_tokens == 4100
    assert econ.total_duration_s == 142.3
    assert econ.total_rounds == 1
    assert econ.retry_rate == 0.0
    # Phases preserved in metrics order, typed as PhaseCost.
    assert [p.phase for p in econ.phases] == ["plan", "implement", "review"]
    assert all(isinstance(p, PhaseCost) for p in econ.phases)
    assert econ.phases[0].total_tokens == 8300
    assert econ.phases[1].duration_s == 28.1


def test_economics_retry_rate_from_attempts():
    # 3 phases, attempts [2, 1, 1] → sum 4, (4 - 3) / 3 = 0.333…
    metrics = {
        "total_tokens": 10,
        "phases": {
            "plan": {"attempts": 2, "total_tokens": 5, "duration_s": 1.0},
            "implement": {"attempts": 1, "total_tokens": 4, "duration_s": 2.0},
            "review": {"attempts": 1, "total_tokens": 1, "duration_s": 0.5},
        },
    }

    econ = project_run_economics(metrics)

    assert econ.phases[0].attempts == 2
    assert econ.retry_rate == pytest.approx((4 - 3) / 3)


def test_economics_missing_attempts_defaults_to_one():
    metrics = {
        "phases": {
            "plan": {"total_tokens": 5, "duration_s": 1.0},
            "implement": {"total_tokens": 4, "duration_s": 2.0},
        },
    }

    econ = project_run_economics(metrics)

    assert [p.attempts for p in econ.phases] == [1, 1]
    assert econ.retry_rate == 0.0


def test_economics_total_tokens_falls_back_to_split_counters():
    metrics = {
        "phases": {
            "plan": {"tokens_in": 1000, "tokens_out": 500, "attempts": 1},
        },
    }

    econ = project_run_economics(metrics)

    assert econ.phases[0].total_tokens == 1500


def test_economics_empty_and_malformed_phases_are_safe():
    # No phases at all → zero rate, empty list.
    assert project_run_economics({}).retry_rate == 0.0
    assert project_run_economics({}).phases == []

    # Non-dict phases block and non-dict entries contribute nothing.
    junk = {"phases": {"plan": "not-a-dict", "implement": {"attempts": 3}}}
    econ = project_run_economics(junk)
    assert [p.phase for p in econ.phases] == ["implement"]
    assert econ.phases[0].attempts == 3
    assert econ.retry_rate == pytest.approx((3 - 1) / 1)

    assert project_run_economics({"phases": []}).phases == []


def test_economics_projects_from_real_metrics_json(fake_workspace):
    # End-to-end over the metrics surface: read metrics.json via the tool,
    # then project the typed economics view from the returned dict.
    write_run(
        fake_workspace, "20260101_000099",
        meta=meta(status="done", project="/p/x", task="t"),
        metrics=metrics(
            total_tokens=999, total_duration_s=12.0, total_rounds=2,
            phases={
                "plan": {"total_tokens": 600, "duration_s": 8.0, "attempts": 2},
                "implement": {"total_tokens": 399, "duration_s": 4.0, "attempts": 1},
            },
        ),
    )

    m = orcho_run_metrics("20260101_000099")
    econ = project_run_economics(m.metrics)

    assert econ.total_tokens == 999
    assert econ.total_rounds == 2
    assert econ.retry_rate == pytest.approx((3 - 2) / 2)
    assert [p.phase for p in econ.phases] == ["plan", "implement"]


# ── orcho_run_events_tail ────────────────────────────────────────────────────

def test_events_tail_returns_all_events_with_eof(fake_workspace):
    write_run(fake_workspace, "20260101_000001",
              meta={"project": "/p/x", "status": "done", "task": "t"},
              events=[_ev(1), _ev(2, kind="phase.end"), _ev(3, kind="run.end")])

    r = orcho_run_events_tail("20260101_000001")
    assert [e.seq for e in r.events] == [1, 2, 3]
    assert r.next_seq == 3
    assert r.eof is True


def test_events_tail_since_seq(fake_workspace):
    write_run(fake_workspace, "20260101_000001",
              meta={"project": "/p/x", "status": "done", "task": "t"},
              events=[_ev(1), _ev(2), _ev(3)])

    r = orcho_run_events_tail("20260101_000001", since_seq=2)
    assert [e.seq for e in r.events] == [3]
    assert r.next_seq == 3
    assert r.eof is True


def test_events_tail_limit_signals_more(fake_workspace):
    write_run(fake_workspace, "20260101_000001",
              meta={"project": "/p/x", "status": "done", "task": "t"},
              events=[_ev(i) for i in range(1, 11)])

    r = orcho_run_events_tail("20260101_000001", limit=4)
    assert [e.seq for e in r.events] == [1, 2, 3, 4]
    assert r.next_seq == 4
    assert r.eof is False  # 6 more events remain past next_seq


def test_events_tail_default_limit_is_small(fake_workspace):
    write_run(fake_workspace, "20260101_000001",
              meta={"project": "/p/x", "status": "done", "task": "t"},
              events=[_ev(i) for i in range(1, 31)])

    r = orcho_run_events_tail("20260101_000001")
    assert [e.seq for e in r.events] == list(range(1, 26))
    assert r.next_seq == 25
    assert r.eof is False


def test_events_tail_no_new_events(fake_workspace):
    write_run(fake_workspace, "20260101_000001",
              meta={"project": "/p/x", "status": "done", "task": "t"},
              events=[_ev(1), _ev(2)])

    r = orcho_run_events_tail("20260101_000001", since_seq=2)
    assert r.events == []
    assert r.next_seq == 2  # advance not possible
    assert r.eof is True


# ── orcho_run_events_tail cursor boundaries ─────────────────────────────────
#
# Pin the (since_seq, limit, total) cursor algebra at the edges
# where off-by-one errors usually hide. Each row encodes:
#   total       — number of events on disk (seqs 1..total)
#   since_seq   — caller's cursor (only events with seq > since_seq returned)
#   limit       — max events to return in this call
#   want_seqs   — expected seqs in the response
#   want_next   — expected next_seq cursor for the follow-up call
#   want_eof    — expected eof flag
#
# Why each row matters (referenced from the test docstring below):
#   "empty stream"         — total=0 + cursor=0 → no events, eof=True
#   "exact-fit eof"        — limit == remaining → all returned, eof=True
#   "exact-fit at since"   — since at top + matching limit
#   "limit truncates"      — limit < remaining → eof=False
#   "limit == 1"           — single-event slice, boundary check
#   "since past last seq"  — cursor beyond highest seq → empty, eof=True
#   "next_seq == last seq" — next_seq matches the seq of the last
#                            returned event (not last+1)
@pytest.mark.parametrize(
    ("total", "since_seq", "limit", "want_seqs", "want_next", "want_eof"),
    [
        # name                          total since limit  seqs       next  eof
        pytest.param(0, 0,  100, [],          0, True,  id="empty stream"),
        pytest.param(3, 0,    3, [1, 2, 3],   3, True,  id="exact-fit eof"),
        pytest.param(5, 2,    3, [3, 4, 5],   5, True,  id="exact-fit at since"),
        pytest.param(10, 0,   4, [1, 2, 3, 4], 4, False, id="limit truncates"),
        pytest.param(5, 0,    1, [1],         1, False, id="limit == 1"),
        pytest.param(3, 10,   5, [],          10, True, id="since past last seq"),
        pytest.param(5, 1,    1, [2],         2, False, id="next_seq == last seq"),
    ],
)
def test_events_tail_cursor_boundaries(
    fake_workspace,
    total: int,
    since_seq: int,
    limit: int,
    want_seqs: list[int],
    want_next: int,
    want_eof: bool,
) -> None:
    """Table-parametrized cursor algebra for ``orcho_run_events_tail``.

    Each case asserts the (events, next_seq, eof) triple for a known
    (total events on disk, since_seq cursor, limit) input. Covers the
    boundaries where off-by-one bugs are easiest to introduce:
    empty stream, exact-fit eof, limit truncation, since-past-end,
    next_seq points at the last RETURNED seq (not last + 1).
    """
    events = [_ev(i) for i in range(1, total + 1)]
    write_run(
        fake_workspace, "20260101_000001",
        meta={"project": "/p/x", "status": "done", "task": "t"},
        events=events if events else None,
    )

    r = orcho_run_events_tail("20260101_000001", since_seq=since_seq, limit=limit)
    assert [e.seq for e in r.events] == want_seqs, (
        f"events mismatch for (total={total}, since={since_seq}, limit={limit})"
    )
    assert r.next_seq == want_next, (
        f"next_seq mismatch for (total={total}, since={since_seq}, limit={limit})"
    )
    assert r.eof is want_eof, (
        f"eof mismatch for (total={total}, since={since_seq}, limit={limit})"
    )


# ── services.run_events.read_run_events — run_control routing guard ─────────
#
# read_run_events is routed through sdk.run_control.read_run_events (the
# run-control read model). These guards pin the two invariants that must
# survive that routing: byte-identical RunEvent-tuple output, and the
# RunNotFound / NoWorkspace → MCP-error mapping.

def test_read_run_events_matches_run_control_output(fake_workspace):
    """The MCP service returns exactly what ``sdk.run_control.read_run_events``
    returns for the same run (same RunEvent tuple, in seq order)."""
    from sdk.run_control import read_run_events as rc_read

    from orcho_mcp.services.run_events import read_run_events

    write_run(
        fake_workspace, "20260101_000001",
        meta={"project": "/p/x", "status": "done", "task": "t"},
        events=[_ev(1), _ev(2, kind="phase.end"), _ev(3, kind="run.end")],
    )

    got = read_run_events("20260101_000001")
    expected = rc_read("20260101_000001", cwd=None)

    assert got == expected
    assert isinstance(got, tuple)
    assert [(e.seq, e.kind, e.phase) for e in got] == [
        (1, "phase.start", "plan"),
        (2, "phase.end", "plan"),
        (3, "run.end", "plan"),
    ]


def test_read_run_events_routes_through_run_control(fake_workspace, monkeypatch):
    """The service consumes the run-control reader (``_rc_read``), not a
    direct ``sdk.list_events`` call."""
    from orcho_mcp.services import run_events as run_events_mod

    sentinel: tuple = ()
    calls: list[tuple] = []

    def _fake_rc_read(run_id, *, cwd):
        calls.append((run_id, cwd))
        return sentinel

    monkeypatch.setattr(run_events_mod, "_rc_read", _fake_rc_read)
    out = run_events_mod.read_run_events("RID")
    assert out is sentinel
    # Routed through run-control with the no-walk-up cwd=None contract.
    assert calls == [("RID", None)]


def test_read_run_events_maps_run_not_found(fake_workspace, monkeypatch):
    """``RunNotFound`` from the run-control reader maps to ``RunNotFoundError``."""
    from sdk import RunNotFound as SDKRunNotFound

    from orcho_mcp.services import run_events as run_events_mod

    def _raise(run_id, *, cwd):
        raise SDKRunNotFound("missing")

    monkeypatch.setattr(run_events_mod, "_rc_read", _raise)
    with pytest.raises(RunNotFoundError):
        run_events_mod.read_run_events("nope")


def test_read_run_events_maps_no_workspace(fake_workspace, monkeypatch):
    """``NoWorkspace`` from the run-control reader maps to
    ``WorkspaceNotResolvedError``."""
    from sdk import NoWorkspace as SDKNoWorkspace

    from orcho_mcp.errors import WorkspaceNotResolvedError
    from orcho_mcp.services import run_events as run_events_mod

    def _raise(run_id, *, cwd):
        raise SDKNoWorkspace("no workspace")

    monkeypatch.setattr(run_events_mod, "_rc_read", _raise)
    with pytest.raises(WorkspaceNotResolvedError):
        run_events_mod.read_run_events("RID")


def test_read_run_events_missing_run_maps_via_real_reader(fake_workspace):
    """End-to-end: a genuinely missing run raises ``RunNotFoundError``
    through the real run-control reader (no monkeypatch)."""
    from orcho_mcp.services.run_events import read_run_events

    with pytest.raises(RunNotFoundError):
        read_run_events("does_not_exist")


# ── workspace state wiring (orcho_workspace_state) ──────────────────────────

def test_workspace_state_missing_file_safe(fake_workspace):
    """If the state file is deleted between calls, ``orcho_workspace_state``
    returns a fresh empty envelope — no exception, no broken tool."""
    # Seed once so the file exists.
    write_run(
        fake_workspace, "20260101_000001",
        meta={"project": "/p/x", "status": "running", "task": "t"},
        events=[_ev(1)],
    )
    orcho_run_events_summary("20260101_000001")
    sp = state_path(fake_workspace)
    assert sp.is_file()

    sp.unlink()
    state = orcho_workspace_state()
    assert state.version == 1
    assert state.runs == {}


# ── recovery_recommendation projection (T6) ─────────────────────────────────
#
# orcho_run_status carries a lineage-aware recovery_recommendation projected
# from the SAME services.run_lineage resolver as orcho_run_diagnose, so a
# captain gets the typed continuation subject without a separate diagnose call
# and the two surfaces never drift.

_RETAINED_WORKTREE = {"isolation": "worktree", "path": "/tmp/wt/source"}


def _write_dogfood_recovery(workspace):
    write_run(
        workspace, "20260101_000001",
        meta=meta(
            status="failed", project="/p/x", task="source",
            worktree=_RETAINED_WORKTREE,
        ),
    )
    write_run(
        workspace, "20260101_000002",
        meta=meta(
            status="halted", project="/p/x", task="recovery",
            halt_reason="phase_handoff_halt",
            resume_mode="followup", parent_run_id="20260101_000001",
        ),
    )


def test_status_recovery_recommendation_dogfood_shape(fake_workspace):
    _write_dogfood_recovery(fake_workspace)

    s = orcho_run_status("20260101_000002")

    assert s.recovery_recommendation is not None
    rec = s.recovery_recommendation
    assert rec.continuation_subject == "source_run_checkpoint"
    assert rec.recommended_next_action == "resume_source_run"
    assert rec.recommended_run_id == "20260101_000001"
    assert rec.lineage.source_run_id == "20260101_000001"
    assert rec.lineage.source_resumable is True


def test_status_recovery_recommendation_none_for_running_run(fake_workspace):
    # An ordinary running run with no terminality and no active child carries
    # no non-trivial recommendation.
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(status="running", project="/p/x", task="t"),
    )

    s = orcho_run_status("20260101_000001")

    assert s.recovery_recommendation is None


def test_status_and_diagnose_recovery_agree_dogfood(fake_workspace):
    # Consistency invariant: the recommendation from orcho_run_status and from
    # orcho_run_diagnose is identical for the same run (single run_lineage
    # source).
    from orcho_mcp.tools import orcho_run_diagnose

    _write_dogfood_recovery(fake_workspace)

    s = orcho_run_status("20260101_000002")
    d = orcho_run_diagnose("20260101_000002")

    assert s.recovery_recommendation is not None
    assert s.recovery_recommendation.continuation_subject == d.continuation_subject
    assert (
        s.recovery_recommendation.recommended_next_action
        == d.recommended_next_action
    )
    assert s.recovery_recommendation.recommended_run_id == d.recommended_run_id
