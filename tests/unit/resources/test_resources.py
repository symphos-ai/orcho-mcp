"""Unit tests for orcho://* resources called as plain Python functions.

L1 of the 4-layer testing methodology: synthetic fixtures, no transport,
millisecond execution. The stdio E2E for resources is covered in
test_initialize_handshake.py (L3) and test_resources_e2e.py.
"""
from __future__ import annotations

import json

import pytest

from orcho_mcp.errors import InvalidPlanError, RunNotFoundError
from orcho_mcp.resources import (
    decode_project_dir,
    encode_project_dir,
    profile_resource,
    profiles_resource,
    project_skills_resource,
    run_diff_patch_resource,
    run_events_resource,
    run_evidence_resource,
    run_meta_resource,
    run_metrics_resource,
    run_parsed_plan_resource,
    run_phase_diff_patch_resource,
    run_summary_resource,
    runs_resource,
    workspace_resource,
)
from tests.fixtures.mcp_workspace import write_run  # type: ignore[import-not-found]

# ── encode_project_dir / decode_project_dir round-trip ───────────────────────

def test_encode_decode_roundtrip():
    raw = "/Users/me/projects/orcho-core"
    enc = encode_project_dir(raw)
    assert "=" not in enc          # padding stripped — URI-safe
    assert "/" not in enc          # no path-segment confusion
    assert decode_project_dir(enc) == raw


def test_decode_rejects_garbage():
    with pytest.raises(ValueError):
        decode_project_dir("not!base64!")


# ── orcho://workspace ────────────────────────────────────────────────────────

def test_workspace_resource_returns_json(fake_workspace):
    payload = json.loads(workspace_resource())
    assert payload["workspace_dir"] == str(fake_workspace)
    assert payload["runs_dir"] == str(fake_workspace / "runspace" / "runs")
    assert payload["recent_projects"] == []


# ── orcho://runs ─────────────────────────────────────────────────────────────

def test_runs_resource_lists_recent(fake_workspace):
    write_run(fake_workspace, "20260101_000001",
              meta={"project": "/p/x", "task": "t1", "status": "done",
                    "timestamp": "2026-01-01T00:00:01"})
    write_run(fake_workspace, "20260101_000002",
              meta={"project": "/p/x", "task": "t2", "status": "running",
                    "timestamp": "2026-01-01T00:00:02"})

    payload = json.loads(runs_resource())
    assert [r["run_id"] for r in payload["runs"]] == [
        "20260101_000002", "20260101_000001",
    ]


# ── orcho://runs/{run_id}/meta ───────────────────────────────────────────────

def test_run_meta_resource(fake_workspace):
    write_run(fake_workspace, "20260101_000001",
              meta={"project": "/p/x", "status": "done", "task": "hello"})
    payload = json.loads(run_meta_resource("20260101_000001"))
    assert payload["task"] == "hello"
    assert payload["status"] == "done"


def test_run_meta_resource_missing(fake_workspace):
    with pytest.raises(RunNotFoundError):
        run_meta_resource("nope")


# ── orcho://runs/{run_id}/metrics ────────────────────────────────────────────

def test_run_metrics_resource(fake_workspace):
    write_run(fake_workspace, "20260101_000001",
              meta={"project": "/p/x", "status": "done", "task": "t"},
              metrics={"total_tokens": 1234})
    payload = json.loads(run_metrics_resource("20260101_000001"))
    assert payload["total_tokens"] == 1234


def test_run_metrics_resource_missing_when_no_file(fake_workspace):
    write_run(fake_workspace, "20260101_000001",
              meta={"project": "/p/x", "status": "running", "task": "t"})
    with pytest.raises(RunNotFoundError):
        run_metrics_resource("20260101_000001")


# ── orcho://runs/{run_id}/events ─────────────────────────────────────────────

def _ev(seq, **kw):
    return {"seq": seq, "ts": f"2026-01-01T00:00:{seq:02d}",
            "kind": kw.get("kind", "phase.start"),
            "phase": kw.get("phase", "plan"),
            "payload": kw.get("payload", {})}


def test_run_events_resource_returns_ndjson(fake_workspace):
    write_run(fake_workspace, "20260101_000001",
              meta={"project": "/p/x", "status": "done", "task": "t"},
              events=[_ev(1), _ev(2, kind="run.end")])

    body = run_events_resource("20260101_000001")
    lines = [line for line in body.splitlines() if line.strip()]
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    assert parsed[0]["seq"] == 1
    assert parsed[1]["kind"] == "run.end"


def test_run_events_resource_empty_when_no_events_file(fake_workspace):
    write_run(fake_workspace, "20260101_000001",
              meta={"project": "/p/x", "status": "running", "task": "t"})
    assert run_events_resource("20260101_000001") == ""


def test_run_events_resource_is_raw_passthrough_byte_for_byte(fake_workspace):
    """Stage-5 no-change guard: ``orcho://runs/{id}/events`` returns the
    on-disk ``events.jsonl`` verbatim — NOT reframed through a typed
    RunEvent / RunSnapshot model.

    Routing this resource through ``sdk.run_control`` (typed RunEvent
    tuples) would re-serialise the stream and change the wire shape
    (NDJSON text → re-encoded JSON), so it stays a raw passthrough. This
    pins byte-for-byte identity with the file the pipeline wrote.
    """
    run_dir = write_run(
        fake_workspace, "20260101_000001",
        meta={"project": "/p/x", "status": "done", "task": "t"},
        events=[_ev(1), _ev(2, kind="phase.end"), _ev(3, kind="run.end")],
    )
    on_disk = (run_dir / "events.jsonl").read_text(encoding="utf-8")
    assert run_events_resource("20260101_000001") == on_disk


def test_run_summary_resource_returns_latest_bounded_summary(fake_workspace):
    write_run(
        fake_workspace,
        "20260101_000001",
        meta={"project": "/p/x", "status": "running", "task": "t"},
        events=[
            _ev(1, kind="run.start", phase=None),
            *[
                _ev(seq, kind="phase.progress", phase="plan")
                for seq in range(2, 205)
            ],
            _ev(205, kind="phase.start", phase="validate_plan"),
        ],
    )

    payload = json.loads(run_summary_resource("20260101_000001"))
    assert payload["run_id"] == "20260101_000001"
    assert payload["next_seq"] == 205
    assert payload["current_phase"] == "validate_plan"
    assert payload["total_count"] == 50
    assert payload["last_n"][-1]["kind"] == "phase.start"


# ── orcho://runs/{run_id}/parsed_plan.json ──────────────────────────────────

def test_run_parsed_plan_resource(fake_workspace):
    run_dir = write_run(
        fake_workspace, "20260101_000001",
        meta={"project": "/p/x", "status": "done", "task": "t"},
    )
    (run_dir / "parsed_plan.json").write_text(
        json.dumps({"artifact_version": 1, "plan": {"short_summary": "Ship it"}}),
        encoding="utf-8",
    )

    payload = json.loads(run_parsed_plan_resource("20260101_000001"))
    assert payload["plan"]["short_summary"] == "Ship it"


def test_run_parsed_plan_resource_missing(fake_workspace):
    write_run(fake_workspace, "20260101_000001",
              meta={"project": "/p/x", "status": "done", "task": "t"})
    with pytest.raises(RunNotFoundError):
        run_parsed_plan_resource("20260101_000001")


# ── orcho://runs/{run_id}/evidence ──────────────────────────────────────────

def test_run_evidence_resource_returns_bundle_body(fake_workspace):
    write_run(
        fake_workspace, "20260101_000001",
        meta={"project": "/p/x", "status": "done", "task": "t"},
        events=[
            _ev(1, kind="run.start", phase=None),
            _ev(
                2,
                kind="plan.parsed",
                phase="plan",
                payload={
                    "source": "json",
                    "short_summary": "Resource plan",
                    "subtask_count": 2,
                    "has_contract": True,
                },
            ),
            _ev(3, kind="run.end", phase=None, payload={"status": "done"}),
        ],
    )

    payload = json.loads(run_evidence_resource("20260101_000001"))
    assert payload["run_id"] == "20260101_000001"
    assert payload["plan"]["short_summary"] == "Resource plan"


# ── orcho://runs/{run_id}/diff.patch ────────────────────────────────────────

def test_run_diff_patch_resource(fake_workspace):
    run_dir = write_run(
        fake_workspace, "20260101_000001",
        meta={"project": "/p/x", "status": "done", "task": "t"},
    )
    patch = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    (run_dir / "diff.patch").write_text(patch, encoding="utf-8")

    assert run_diff_patch_resource("20260101_000001") == patch


def test_run_diff_patch_resource_missing(fake_workspace):
    write_run(fake_workspace, "20260101_000001",
              meta={"project": "/p/x", "status": "done", "task": "t"})
    with pytest.raises(RunNotFoundError):
        run_diff_patch_resource("20260101_000001")


# ── orcho://runs/{run_id}/phases/{phase}/diff.patch ─────────────────────────

def test_run_phase_diff_patch_resource(fake_workspace):
    run_dir = write_run(
        fake_workspace, "20260601_500001",
        meta={"project": "/p/x", "status": "done", "task": "t"},
    )
    patch = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+phase-edit\n"
    )
    phase_dir = run_dir / "phases" / "implement"
    phase_dir.mkdir(parents=True)
    (phase_dir / "diff.patch").write_text(patch, encoding="utf-8")

    assert (
        run_phase_diff_patch_resource("20260601_500001", "implement")
        == patch
    )


def test_run_phase_diff_patch_resource_missing_phase_artifact(fake_workspace):
    """A run with a cumulative diff but no per-phase artifact must
    still raise — no silent fallback to the cumulative diff under a
    per-phase URI.
    """
    run_dir = write_run(
        fake_workspace, "20260601_500002",
        meta={"project": "/p/x", "status": "done", "task": "t"},
    )
    (run_dir / "diff.patch").write_text(
        "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n",
        encoding="utf-8",
    )
    with pytest.raises(RunNotFoundError, match="repair_changes"):
        run_phase_diff_patch_resource("20260601_500002", "repair_changes")


def test_run_phase_diff_patch_resource_missing_run(fake_workspace):
    with pytest.raises(RunNotFoundError):
        run_phase_diff_patch_resource("does-not-exist", "implement")


@pytest.mark.parametrize(
    "bad_phase",
    ["", "   ", "..", "../etc", "phases/implement", "a\\b"],
)
def test_run_phase_diff_patch_resource_rejects_unsafe_phase(
    fake_workspace, bad_phase,
):
    write_run(
        fake_workspace, "20260601_500003",
        meta={"project": "/p/x", "status": "done", "task": "t"},
    )
    with pytest.raises(InvalidPlanError):
        run_phase_diff_patch_resource("20260601_500003", bad_phase)


# ── orcho://profiles ─────────────────────────────────────────────────────────

def test_profiles_resource_includes_builtins():
    """The ``orcho://profiles`` resource exposes the Stage C semantic
    work-kind catalogue (feature / small_task / planning / …) and no
    longer presents the legacy flat names as built-in identity."""
    payload = json.loads(profiles_resource())
    names = {p["name"] for p in payload["profiles"]}
    semantic_set = {
        "small_task", "feature", "complex_feature", "planning",
        "code_review", "delivery_audit", "research", "refactor",
        "migration",
    }
    assert semantic_set <= names, (
        f"expected semantic profile set, missing "
        f"{sorted(semantic_set - names)}; got {sorted(names)}"
    )
    legacy_public = {"advanced", "lite", "enterprise", "plan", "review"}
    assert not (legacy_public & names), (
        f"legacy flat profile names leaked into the resource: "
        f"{sorted(legacy_public & names)}"
    )


def test_profile_resource_single():
    """Pick a semantic profile that ships in the Stage C catalogue —
    ``feature`` is the default full-cycle work kind."""
    payload = json.loads(profile_resource("feature"))
    assert payload["name"] == "feature"
    assert "plan" in payload["phases"]
    assert payload["semantic_profile"] == "feature"


def test_profile_resource_unknown_raises():
    with pytest.raises(KeyError):
        profile_resource("nope")


# ── orcho://projects/{b64}/skills ────────────────────────────────────────────

def test_project_skills_resource_empty_when_no_skills(tmp_path, isolated_user_skills):
    enc = encode_project_dir(str(tmp_path))
    payload = json.loads(project_skills_resource(enc))
    assert payload["skills"] == []
    assert payload["project_dir"] == str(tmp_path)


def test_project_skills_resource_parses_skill_md_directory(tmp_path, isolated_user_skills):
    skill_dir = tmp_path / ".agents" / "skills" / "frontend"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: frontend\n"
        "description: handles React\n"
        "---\n"
        "Body.\n",
        encoding="utf-8",
    )
    enc = encode_project_dir(str(tmp_path))
    payload = json.loads(project_skills_resource(enc))
    assert len(payload["skills"]) == 1
    assert payload["skills"][0]["name"] == "frontend"
    assert payload["skills"][0]["source"] == "project"


def test_project_skills_resource_rejects_bad_b64():
    with pytest.raises(ValueError):
        project_skills_resource("!!!not-base64!!!")
