"""Unit tests for ``orcho_mcp.services.meta_summary``.

Pure-function coverage for the summary-only projection that keeps
``orcho_run_status`` polling cheap: phase bodies become size / count
markers, the task text truncates, gate verdicts pass through, and
``include`` re-admits specific body families (``"all"`` = identity).
"""
from __future__ import annotations

import json

from orcho_mcp.services.meta_summary import summarize_run_meta


def _bulky_meta() -> dict:
    """A meta.json shaped like a real subtask_dag run (heavy bodies)."""
    return {
        "task": "T" * 5000,
        "project": "/p/x",
        "status": "done",
        "phases": {
            "plan": [
                {
                    "attempt": 1,
                    "output": "PLAN MARKDOWN " * 1000,
                    "total_atomic_tasks": 5,
                    "parsed_file_paths": ["a.py", "b.py", "c.py"],
                    "existing_files": ["a.py"],
                    "missing_files": ["b.py", "c.py"],
                    "session_id": "sid-plan",
                    "prompt_render": {"big": "x" * 800},
                    "context_growth": {"k": "v" * 400},
                },
            ],
            "validate_plan": [
                {
                    "attempt": 1,
                    "verdict": "APPROVED",
                    "approved": True,
                    "short_summary": "looks good",
                    "critique": "C" * 3000,
                    "raw_response": "R" * 3000,
                    "findings": [{"id": 1}, {"id": 2}],
                    "prompt_render": {"big": "x" * 800},
                },
            ],
            "implement": {
                "output": "AGENT OUTPUT " * 2000,
                "delivery_clean": True,
                "progress": {"done": 5},
                "implementation_receipts": [
                    {
                        "subtask_id": "T1",
                        "state": "done",
                        "runtime": "claude",
                        "model": "claude-opus-4-8",
                        "duration": 12.3,
                        "done_criteria": ["x"] * 50,
                        "criteria_report": [{"c": "y" * 500}],
                        "attestation_summary": "A" * 300,
                    },
                ],
                "prompt_render": {"big": "x" * 800},
            },
            "rounds": [
                {
                    "round": 1,
                    "critique": "RC" * 1500,
                    "repair_output": "RO" * 1500,
                    "repair_model": "claude-opus-4-8",
                    "repair_receipt": {
                        "subtask_id": "T3",
                        "state": "done",
                        "criteria_report": [{"c": "z" * 400}],
                    },
                    "context_pressure_repair": {"k": "v" * 400},
                },
            ],
            "final_acceptance": {
                "approved": True,
                "verdict": "APPROVED",
                "ship_ready": True,
                "short_summary": "ship it",
                "critique": "FC" * 700,
                "raw_response": "FR" * 700,
                "findings": [{"id": 1}],
                "release_blockers": [],
                "contract_status": {"api": "ok"},
            },
            "contract_check": {
                "api": {
                    "verdict": "SKIPPED",
                    "skipped": True,
                    "skip_reason": "operator_decision",
                    "operator_feedback": "tiny change",
                    "findings": [],
                    "short_summary": "skipped",
                },
            },
        },
    }


def _chars(obj) -> int:
    return len(json.dumps(obj, ensure_ascii=False, default=str))


# ── default summary ──────────────────────────────────────────────────────────

def test_default_summary_is_far_smaller():
    raw = _bulky_meta()
    out = summarize_run_meta(raw)
    assert _chars(out) < _chars(raw) / 5  # >80% smaller


def test_task_truncated_with_char_marker():
    out = summarize_run_meta(_bulky_meta())
    assert len(out["task"]) == 280
    assert out["task_chars"] == 5000
    assert out["task_truncated"] is True


def test_short_task_left_alone():
    out = summarize_run_meta({"task": "tiny", "phases": {}})
    assert out["task"] == "tiny"
    assert "task_chars" not in out


def test_plan_body_elided_to_chars_and_counts():
    plan = summarize_run_meta(_bulky_meta())["phases"]["plan"][0]
    assert "output" not in plan
    assert plan["output_chars"] > 1000
    assert plan["parsed_file_paths_count"] == 3
    assert plan["existing_files_count"] == 1
    assert plan["missing_files_count"] == 2
    assert plan["total_atomic_tasks"] == 5  # scalar passes through
    assert "prompt_render" not in plan      # observability dropped
    assert "context_growth" not in plan


def test_validate_plan_keeps_verdict_drops_critique():
    vp = summarize_run_meta(_bulky_meta())["phases"]["validate_plan"][0]
    assert vp["verdict"] == "APPROVED"
    assert vp["approved"] is True
    assert vp["short_summary"] == "looks good"
    assert "critique" not in vp
    assert vp["critique_chars"] == 3000
    assert vp["raw_response_chars"] == 3000
    assert vp["findings_count"] == 2


def test_implement_output_elided_receipts_collapsed():
    impl = summarize_run_meta(_bulky_meta())["phases"]["implement"]
    assert "output" not in impl
    assert impl["output_chars"] > 1000
    assert impl["delivery_clean"] is True
    r = impl["implementation_receipts"][0]
    assert r["subtask_id"] == "T1"
    assert r["state"] == "done"
    assert r["duration"] == 12.3
    assert "criteria_report" not in r
    assert "done_criteria" not in r
    assert "attestation_summary" not in r


def test_rounds_bodies_elided_repair_receipt_collapsed():
    rnd = summarize_run_meta(_bulky_meta())["phases"]["rounds"][0]
    assert rnd["round"] == 1
    assert "critique" not in rnd
    assert rnd["critique_chars"] == 3000
    assert rnd["repair_output_chars"] == 3000
    assert "criteria_report" not in rnd["repair_receipt"]
    assert rnd["repair_receipt"]["subtask_id"] == "T3"
    assert "context_pressure_repair" not in rnd


def test_final_acceptance_keeps_gate_fields():
    fa = summarize_run_meta(_bulky_meta())["phases"]["final_acceptance"]
    assert fa["verdict"] == "APPROVED"
    assert fa["ship_ready"] is True
    assert fa["contract_status"] == {"api": "ok"}
    assert "critique" not in fa
    assert fa["critique_chars"] > 0
    assert fa["findings_count"] == 1


def test_contract_check_gate_entry_passes_through():
    cc = summarize_run_meta(_bulky_meta())["phases"]["contract_check"]["api"]
    assert cc["verdict"] == "SKIPPED"
    assert cc["skipped"] is True
    assert cc["skip_reason"] == "operator_decision"
    assert cc["operator_feedback"] == "tiny change"
    assert cc["findings_count"] == 0


# ── include re-admits bodies ─────────────────────────────────────────────────

def test_include_all_is_identity():
    raw = _bulky_meta()
    assert summarize_run_meta(raw, include=frozenset({"all"})) == raw


def test_include_task_keeps_full_text():
    out = summarize_run_meta(_bulky_meta(), include=frozenset({"task"}))
    assert len(out["task"]) == 5000
    assert "task_chars" not in out


def test_include_plan_keeps_markdown():
    out = summarize_run_meta(_bulky_meta(), include=frozenset({"plan"}))
    plan = out["phases"]["plan"][0]
    assert "output" in plan and plan["output"].startswith("PLAN MARKDOWN")
    assert plan["parsed_file_paths"] == ["a.py", "b.py", "c.py"]


def test_include_output_keeps_implement_body_only():
    out = summarize_run_meta(_bulky_meta(), include=frozenset({"output"}))
    assert "output" in out["phases"]["implement"]
    # plan output is a different family; still elided
    assert "output" not in out["phases"]["plan"][0]


def test_include_critiques_keeps_critique_bodies():
    out = summarize_run_meta(_bulky_meta(), include=frozenset({"critiques"}))
    assert "critique" in out["phases"]["validate_plan"][0]
    assert "critique" in out["phases"]["rounds"][0]
    assert out["phases"]["validate_plan"][0]["findings"] == [{"id": 1}, {"id": 2}]


def test_include_receipts_keeps_full_receipt():
    out = summarize_run_meta(_bulky_meta(), include=frozenset({"receipts"}))
    r = out["phases"]["implement"]["implementation_receipts"][0]
    assert "criteria_report" in r
    assert "attestation_summary" in r


def test_include_tokens_case_insensitive():
    out = summarize_run_meta(_bulky_meta(), include=frozenset({"ALL"}))
    assert out == _bulky_meta()


# ── defensive contract ───────────────────────────────────────────────────────

def test_non_dict_meta_returned_unchanged():
    assert summarize_run_meta(None) is None  # type: ignore[arg-type]
    assert summarize_run_meta("oops") == "oops"  # type: ignore[arg-type]


def test_corrupt_phases_does_not_raise():
    out = summarize_run_meta(
        {"task": "t", "phases": {"plan": "not-a-list", "implement": 42}},
    )
    assert out["phases"]["plan"] == "not-a-list"
    assert out["phases"]["implement"] == 42


def test_unknown_phase_light_summary():
    out = summarize_run_meta(
        {"phases": {"mystery": {"critique": "Z" * 100, "ok": True}}},
    )
    myst = out["phases"]["mystery"]
    assert "critique" not in myst
    assert myst["critique_chars"] == 100
    assert myst["ok"] is True


def test_input_not_mutated():
    raw = _bulky_meta()
    before = json.dumps(raw, default=str)
    summarize_run_meta(raw)
    assert json.dumps(raw, default=str) == before
