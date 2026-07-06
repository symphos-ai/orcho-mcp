"""Unit tests for read-query MCP tools (workspace_info / history /
skills_list / profiles_list).

Backed by ``orcho_mcp.services.read_queries``; the @mcp.tool handlers
are one-line shims so calling them as plain Python validates the
service path end-to-end.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from orcho_mcp.tools import (
    orcho_profiles_list,
    orcho_run_history,
    orcho_skills_list,
    orcho_workspace_info,
)
from tests.fixtures.mcp_workspace import write_run

# ── orcho_workspace_info ─────────────────────────────────────────────────────

def test_workspace_info_reports_resolved_paths(fake_workspace):
    info = orcho_workspace_info()
    assert info.workspace_dir == str(fake_workspace)
    assert info.runs_dir == str(fake_workspace / "runspace" / "runs")
    assert info.recent_projects == []


def test_workspace_info_collects_recent_projects(fake_workspace):
    write_run(fake_workspace, "20260101_000001", meta={"project": "/p/alpha"})
    write_run(fake_workspace, "20260101_000002", meta={"project": "/p/beta"})
    write_run(fake_workspace, "20260101_000003", meta={"project": "/p/alpha"})  # dup

    info = orcho_workspace_info()
    # Newest first; deduped.
    assert info.recent_projects == ["/p/alpha", "/p/beta"]


def test_workspace_info_when_unresolvable(monkeypatch):
    monkeypatch.delenv("ORCHO_WORKSPACE", raising=False)
    # ``find_runs_dir`` also honours ``$ORCHO_RUNSPACE`` (and the suite may
    # run inside a live orcho workspace that exports it); clear it so the
    # resolution is genuinely unresolvable — mirrors the ``fake_workspace``
    # fixture's own ORCHO_RUNSPACE teardown.
    monkeypatch.delenv("ORCHO_RUNSPACE", raising=False)
    monkeypatch.chdir("/")  # nowhere walkup will find a workspace marker
    info = orcho_workspace_info()
    assert info.workspace_dir is None
    assert info.runs_dir is None
    assert info.recent_projects == []


# ── orcho_run_history ────────────────────────────────────────────────────────

def test_history_empty_when_no_runs(fake_workspace):
    result = orcho_run_history()
    assert result.runs == []


def test_history_newest_first(fake_workspace):
    write_run(fake_workspace, "20260101_000001",
              meta={"project": "/p/x", "task": "older", "status": "done",
                    "timestamp": "2026-01-01T00:00:01"},
              metrics={"total_tokens": 100, "total_duration_s": 1.5})
    write_run(fake_workspace, "20260101_000002",
              meta={"project": "/p/x", "task": "newer", "status": "done",
                    "timestamp": "2026-01-01T00:00:02"},
              metrics={"total_tokens": 200, "total_duration_s": 2.0})

    result = orcho_run_history()
    assert [r.run_id for r in result.runs] == ["20260101_000002", "20260101_000001"]
    assert result.runs[0].total_tokens == 200


def test_history_respects_limit(fake_workspace):
    for i in range(5):
        write_run(fake_workspace, f"20260101_00000{i}",
                  meta={"project": "/p/x", "task": f"t{i}",
                        "status": "done", "timestamp": "now"})
    assert len(orcho_run_history(limit=3).runs) == 3


def test_history_filters_by_project_dir(fake_workspace):
    write_run(fake_workspace, "20260101_000001",
              meta={"project": "/p/alpha", "task": "a", "status": "done",
                    "timestamp": "now"})
    write_run(fake_workspace, "20260101_000002",
              meta={"project": "/p/beta", "task": "b", "status": "done",
                    "timestamp": "now"})

    result = orcho_run_history(project_dir="/p/alpha")
    assert [r.project for r in result.runs] == ["/p/alpha"]


def test_history_skips_runs_without_meta(fake_workspace):
    """Half-written runs (meta.json missing) are silently dropped."""
    runs_dir = fake_workspace / "runspace" / "runs"
    (runs_dir / "20260101_999999").mkdir()  # no meta.json
    write_run(fake_workspace, "20260101_000001",
              meta={"project": "/p/x", "task": "ok", "status": "done",
                    "timestamp": "now"})

    result = orcho_run_history()
    assert [r.run_id for r in result.runs] == ["20260101_000001"]


# ── orcho_skills_list ────────────────────────────────────────────────────────

def test_skills_list_empty_when_no_folder(tmp_path, isolated_user_skills):
    r = orcho_skills_list(project_dir=str(tmp_path))
    assert r.skills == []


def test_skills_list_parses_skill_md_directory(tmp_path: Path, isolated_user_skills):
    """Canonical Agent Skills layout: ``<project>/.agents/skills/<name>/SKILL.md``."""
    skill_dir = tmp_path / ".agents" / "skills" / "css_dev"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: css_dev\n"
        "description: handles CSS\n"
        "---\n"
        "Body of the skill prompt.\n",
        encoding="utf-8",
    )
    r = orcho_skills_list(project_dir=str(tmp_path))
    assert len(r.skills) == 1
    only = r.skills[0]
    assert only.name == "css_dev"
    assert only.description == "handles CSS"
    assert only.source == "project"
    assert only.checksum  # canonical sha256 over SKILL.md + manifest


# ── orcho_profiles_list ──────────────────────────────────────────────────────

def test_profiles_list_includes_builtins():
    """Profile catalogue includes the Stage C semantic work-kind set.

    The catalogue is keyed by semantic profile (``feature`` /
    ``small_task`` / ``planning`` / …). The old flat names
    (``advanced`` / ``lite`` / ``enterprise`` / ``plan`` / ``review``)
    are NOT a required built-in identity any more — assert the
    semantic set is present and that the legacy flat names are not
    silently re-introduced as public catalogue entries.
    """
    r = orcho_profiles_list()
    names = {p.name for p in r.profiles}
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
        f"legacy flat profile names leaked into the catalogue: "
        f"{sorted(legacy_public & names)}"
    )


def test_profiles_phases_are_lists():
    """``phases`` field is always a list (v1 source-of-truth, v2
    flattens steps for backwards-compat read)."""
    r = orcho_profiles_list()
    assert all(isinstance(p.phases, list) for p in r.profiles)
    # Profile that has 'plan' phase exists in either shape.
    has_plan = any("plan" in p.phases for p in r.profiles)
    assert has_plan, "no profile contains a 'plan' phase"


def test_profiles_v2_fields_populated_when_v2_source(tmp_path):
    """Semantic built-ins surface semantic metadata, not a flat variant.

    Stage C built-ins are keyed by ``semantic_profile`` and leave the
    plugin/custom typology (``kind`` defaults to ``custom``, ``variant``
    stays ``None``). The semantic identity lives in
    ``semantic_profile`` / ``default_mode`` / ``recipe_kind`` instead.
    """
    r = orcho_profiles_list()
    if r.source != "json_v2":
        pytest.skip("v2 file not present")
    # feature is the canonical full_cycle / pro semantic entry.
    feature = next((p for p in r.profiles if p.name == "feature"), None)
    assert feature is not None, "v2 source but no 'feature' profile"
    assert feature.kind == "custom"
    assert feature.variant is None
    assert feature.description, "v2 entries must carry description"
    assert feature.semantic_profile == "feature"
    assert feature.default_mode == "pro"
    assert feature.recipe_kind == "full_cycle"
    assert feature.internal is False


def test_profiles_internal_flag_marks_engine_profiles():
    """Engine-internal profiles (``task`` / ``correction``) stay in the
    catalogue but are flagged ``internal=true`` and carry no public
    semantic identity, while public work kinds are ``internal=false``."""
    r = orcho_profiles_list()
    if r.source != "json_v2":
        pytest.skip("v2 file not present")
    by_name = {p.name: p for p in r.profiles}
    for internal_name in ("task", "correction"):
        prof = by_name.get(internal_name)
        assert prof is not None, f"catalogue dropped internal '{internal_name}'"
        assert prof.internal is True
        assert prof.semantic_profile is None
        assert prof.recipe_kind == "internal"
    # Public semantic work kinds are not flagged internal.
    assert by_name["feature"].internal is False
    assert by_name["planning"].internal is False


def test_profiles_focused_recipe_kind_surfaced():
    """Focused semantic profiles (e.g. ``planning``) report
    ``recipe_kind='focused'`` and their default mode."""
    r = orcho_profiles_list()
    if r.source != "json_v2":
        pytest.skip("v2 file not present")
    planning = next((p for p in r.profiles if p.name == "planning"), None)
    assert planning is not None, "v2 source but no 'planning' profile"
    assert planning.semantic_profile == "planning"
    assert planning.recipe_kind == "focused"
    assert planning.default_mode == "pro"


def test_profiles_list_surfaces_plan_hypothesis_policy():
    r = orcho_profiles_list()
    if r.source != "json_v2":
        pytest.skip("v2 file not present — hypothesis policy is v2-only")

    # Shipped profiles currently declare ``hypothesis.attempts=0`` on
    # their plan steps. MCP intentionally surfaces that disabled policy
    # as ``hypothesis=None`` because the runtime would skip the
    # hypothesis loop.
    feature = next((p for p in r.profiles if p.name == "feature"), None)
    assert feature is not None, "v2 source but no 'feature' profile"
    assert feature.hypothesis is None


def test_profiles_v1_fields_none_for_v1_source(tmp_path):
    """v1-schema source returns ``kind`` / ``variant`` / ``description``
    as ``None``."""
    r = orcho_profiles_list()
    if r.source != "json":
        pytest.skip("v2 source — kind/variant fields populated by design")
    for p in r.profiles:
        assert p.kind is None
        assert p.variant is None


def test_profiles_cross_gates_surfaces_explicit_block(monkeypatch, tmp_path):
    """``cross_gates`` policy block flows through to ``ProfileRecord``.

    When the v2 profile JSON declares a ``cross_gates`` entry the MCP
    schema surface exposes it as a typed dict so clients can decide
    e.g. whether to render a "manual confirm" prompt before launching
    a run. Missing blocks remain ``None`` — clients should treat that
    as "use orcho-core defaults", not "no policy".
    """
    custom = tmp_path / "pipeline_profiles_v2.json"
    custom.write_text(
        '{\n'
        '  "manual_demo": {\n'
        '    "kind": "custom",\n'
        '    "description": "manual_confirm demo",\n'
        '    "steps": [\n'
        '      {"phase": "plan", "cross": {"scope": "global", "handler": "cross_plan"}},\n'
        '      {"phase": "implement", "cross": {"scope": "project"}}\n'
        '    ],\n'
        '    "cross_gates": {\n'
        '      "contract_check": {\n'
        '        "enabled": true,\n'
        '        "mode": "artifact_bundle",\n'
        '        "run": "manual_confirm",\n'
        '        "on_skip": "allow_with_gap"\n'
        '      }\n'
        '    }\n'
        '  }\n'
        '}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("ORCHO_PROFILES_V2_PATH", str(custom))
    # Isolate operator overlays: ``load_profiles_v2`` patches the raw JSON
    # with any ``profiles_v2`` block from ``$ORCHO_WORKSPACE/.orcho/
    # config.local.json``. When the suite runs inside a live orcho
    # workspace that overlay leaks profiles (e.g. ``plan``) the custom
    # registry never declares, raising ProfileLoadError. Point
    # ORCHO_WORKSPACE at the overlay-free tmp dir so discovery finds none.
    monkeypatch.setenv("ORCHO_WORKSPACE", str(tmp_path))
    # Reload the services read-queries module so the env override is
    # picked up by ``_PROFILES_V2_JSON_PATH`` at module-import time.
    import importlib

    from orcho_mcp.services import read_queries as rq_mod
    importlib.reload(rq_mod)
    try:
        # Call the service directly — same code path as the @mcp.tool
        # handler, no risk of importing a stale shim reference.
        r = rq_mod.get_profiles_list()
        assert r.source == "json_v2"
        manual = next(
            (p for p in r.profiles if p.name == "manual_demo"), None,
        )
        assert manual is not None
        assert manual.cross_gates is not None
        cc = manual.cross_gates["contract_check"]
        assert cc["enabled"] is True
        assert cc["run"] == "manual_confirm"
        assert cc["on_skip"] == "allow_with_gap"
        assert cc["mode"] == "artifact_bundle"
    finally:
        monkeypatch.delenv("ORCHO_PROFILES_V2_PATH", raising=False)
        importlib.reload(rq_mod)


def test_profiles_cross_gates_none_when_absent():
    """Profiles that don't declare cross_gates expose ``cross_gates=None``.

    Under the missing≡off rule in orcho-core (a profile that omits
    cross_gates does NOT run the runner-owned cross gates), the MCP
    surface returns ``None`` verbatim. Clients that want to render
    "no cross gates" for these profiles read None and know not to
    expect contract_check / cross_final_acceptance verdicts.

    The shipped catalogue currently leaves the focused / scoped
    profiles (small_task, planning, research, code_review) without the
    block — they resolve to gates-off in orcho-core; this test pins the
    MCP wire shape on any one of them."""
    r = orcho_profiles_list()
    if r.source != "json_v2":
        pytest.skip("v1 source — cross_gates field is v2-only")
    # Any shipped profile that omits the block works for this
    # assertion. The focused profiles are least likely to gain an
    # explicit policy soon.
    fallback_target = next(
        (
            p for p in r.profiles
            if p.name in ("small_task", "planning", "research", "code_review")
        ),
        None,
    )
    if fallback_target is None:
        pytest.skip(
            "no opt-out profile in the catalogue — "
            "the JSON has moved every profile to explicit policy"
        )
    assert fallback_target.cross_gates is None


# ── source="missing" graceful fallback ──────────────────────────────────────

class TestProfilesListMissingFallback:
    """When the v2 profile catalogue is absent (orcho-mcp installed
    without orcho-core, or ORCHO_PROFILES_V2_PATH points at a missing
    file), the tool MUST return:

      * profiles=[]
      * source="missing"
      * diagnostic with actionable text
    """

    def test_missing_v2_file_returns_source_missing(
        self, monkeypatch, tmp_path,
    ):
        """ORCHO_PROFILES_V2_PATH override points at a non-existent
        path → source='missing' + diagnostic."""
        nowhere = tmp_path / "does-not-exist.json"
        monkeypatch.setenv("ORCHO_PROFILES_V2_PATH", str(nowhere))
        # Reload the module-level constant so the env var takes effect
        # for this call.
        from orcho_mcp.services import read_queries as _rq
        monkeypatch.setattr(
            _rq,
            "_PROFILES_V2_JSON_PATH",
            _rq._resolve_profiles_v2_path(),
        )
        r = orcho_profiles_list()
        assert r.profiles == []
        assert r.source == "missing"
        assert r.diagnostic is not None
        assert "v2 profile catalogue not found" in r.diagnostic
        assert "orcho-core" in r.diagnostic.lower()

    def test_diagnostic_mentions_env_var_override(
        self, monkeypatch, tmp_path,
    ):
        """The diagnostic guides the user to the fix — naming the env
        var override that lets them point at a custom file."""
        nowhere = tmp_path / "missing.json"
        monkeypatch.setenv("ORCHO_PROFILES_V2_PATH", str(nowhere))
        from orcho_mcp.services import read_queries as _rq
        monkeypatch.setattr(
            _rq,
            "_PROFILES_V2_JSON_PATH",
            _rq._resolve_profiles_v2_path(),
        )
        r = orcho_profiles_list()
        assert "ORCHO_PROFILES_V2_PATH" in (r.diagnostic or "")

    def test_present_file_returns_json_v2_no_diagnostic(self):
        """When the v2 file is present, source='json_v2' and
        diagnostic=None (success path)."""
        r = orcho_profiles_list()
        if r.source != "json_v2":
            pytest.skip("v2 file not present in this environment")
        assert r.diagnostic is None
        assert len(r.profiles) > 0
