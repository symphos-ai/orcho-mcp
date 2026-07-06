"""orcho_mcp.services.read_queries — shared read-side query implementations.

These functions back four MCP tools (``orcho_workspace_info``,
``orcho_run_history``, ``orcho_profiles_list``, ``orcho_skills_list``)
and the matching ``orcho://*`` resource URIs. Tools and resources are
sibling adapters over this layer — neither should call the other.

All four are synchronous and read-only. Errors raise
``orcho_mcp.errors`` subclasses that FastMCP maps to JSON-RPC errors at
the wire boundary.
"""
from __future__ import annotations

from pathlib import Path

from core.infra import config as _core_config
from pipeline.skills import discover_skills
from sdk import (
    NoWorkspace as _SDKNoWorkspace,
    get_run_metrics,
    list_history,
    load_meta,
)
from sdk.profiles import catalogue_path, list_profiles

from orcho_mcp.schemas import (
    HistoryResult,
    ProfileRecord,
    ProfileSelectorRecord,
    ProfilesListResult,
    RunRecord,
    SkillRecord,
    SkillsListResult,
    WorkspaceInfo,
)
from orcho_mcp.services.run_lookup import runs_dir_or_raise

# Profile *selector* token — a ``profile`` value resolved dynamically before
# profile resolution, NOT an executable recipe. Import the canonical token
# defensively (single source of truth) so a stale core that predates the
# constant still loads (falling back to the literal).
try:
    from pipeline.project.auto_detect import (
        AUTO_DETECT_PROFILE_TOKEN as _AUTO_DETECT_PROFILE_TOKEN,
    )
except ImportError:  # pragma: no cover - exercised by the stale-core unit test
    _AUTO_DETECT_PROFILE_TOKEN = "auto-detect"

# ── orcho_workspace_info backing ────────────────────────────────────────────

def get_workspace_info() -> WorkspaceInfo:
    """Return where orcho reads/writes runs and which projects appear in recent history."""
    try:
        workspace_dir = str(_core_config.get_workspace_dir())
    except Exception:  # noqa: BLE001
        workspace_dir = None

    try:
        runs_dir = str(_core_config.get_runs_dir())
    except Exception:  # noqa: BLE001
        runs_dir = None

    recent_projects: list[str] = []
    if runs_dir is not None:
        try:
            summaries = list_history(runs_dir=Path(runs_dir))
        except _SDKNoWorkspace:
            summaries = []
        seen: set[str] = set()
        for s in summaries:
            if s.project and s.project not in seen:
                seen.add(s.project)
                recent_projects.append(s.project)
            if len(recent_projects) >= 20:
                break

    return WorkspaceInfo(
        workspace_dir=workspace_dir,
        runs_dir=runs_dir,
        recent_projects=recent_projects,
    )


# ── orcho_run_history backing ───────────────────────────────────────────────

def get_run_history(
    limit: int = 10, project_dir: str | None = None,
) -> HistoryResult:
    """List the most recent runs, newest first.

    ``limit`` default mirrors the MCP tool's wire-default (10) — callers
    that want a longer catalogue (e.g. ``runs_resource`` for the
    ``orcho://runs`` URI) pass ``limit=50`` explicitly. Do not raise the
    default here without regenerating ``docs/mcp_schema.json``.
    """
    rd = runs_dir_or_raise()
    if not rd.is_dir():
        return HistoryResult(runs=[])

    summaries = list_history(runs_dir=rd)

    out: list[RunRecord] = []
    for s in summaries:
        meta = load_meta(s.run_dir)
        if not meta:
            # Skip runs whose meta.json is missing / unreadable / empty.
            continue
        project = str(meta.get("project") or "")
        if project_dir is not None and project != project_dir:
            continue

        m = get_run_metrics(s.run_id, runs_dir=rd)
        out.append(RunRecord(
            run_id=s.run_id,
            project=project or "?",
            task=str(meta.get("task") or "?"),
            status=str(meta.get("status") or "?"),
            timestamp=str(meta.get("timestamp") or s.run_id),
            total_tokens=m.total_tokens,
            total_duration_s=m.total_duration_s,
            rounds=m.total_rounds,
        ))
        if len(out) >= limit:
            break

    return HistoryResult(runs=out)


# ── orcho_skills_list backing ───────────────────────────────────────────────

def get_project_skills(project_dir: str) -> SkillsListResult:
    """Discover Agent Skills packages visible to the project."""
    from core.infra.platform import workspace_dir as _resolve_workspace
    workspace = _resolve_workspace() or Path(project_dir)
    # Introspection: surface every visible skill so callers can see what
    # they would opt into, even when project / compat sources are not
    # trusted by default. Runtime activation remains gated separately.
    registry = discover_skills(
        project_dir=project_dir,
        workspace_dir=workspace,
        include_untrusted=True,
    )
    return SkillsListResult(
        project_dir=project_dir,
        skills=[
            SkillRecord(
                name=pkg.name,
                description=pkg.description,
                source=pkg.source,
                checksum=pkg.checksum,
                root_dir=str(pkg.root_dir),
            )
            for pkg in registry.values()
        ],
    )


# ── orcho_profiles_list backing ─────────────────────────────────────────────

def _profile_selectors() -> list[ProfileSelectorRecord]:
    """The catalogue's dynamic profile selectors.

    Currently the single ``auto-detect`` selector. The token comes from the
    core constant (defensive fallback above) so it stays a single source of
    truth with the spawn / status paths. Independent of the v2 catalogue
    file — returned in every branch, including ``source='missing'``.
    """
    return [
        ProfileSelectorRecord(
            name=_AUTO_DETECT_PROFILE_TOKEN,
            description=(
                "Semantic selector (non-executable): core classifies the "
                "work kind and selects the matching semantic profile + mode. "
                "Pass as the ``profile`` argument to orcho_run_start."
            ),
        ),
    ]


def get_profiles_list() -> ProfilesListResult:
    """Return the catalogue of pipeline profiles (v2 only).

    The catalogue is read exclusively through ``sdk.profiles`` — the SDK
    owns catalogue-path resolution (honouring the ``ORCHO_PROFILES_V2_PATH``
    override) and the field projection. The ``source='missing'`` branch and
    the ``selectors`` block stay here on the MCP side: a selector like
    ``auto-detect`` is resolved by core before profile resolution and does
    not depend on the v2 catalogue file being present, so it is surfaced in
    EVERY branch — including ``source='missing'``.
    """
    path = catalogue_path()
    if not path.is_file():
        diagnostic = (
            f"v2 profile catalogue not found at {path}. "
            "orcho-mcp expects orcho-core's "
            "_config/pipeline_profiles_v2.json — make sure orcho-core "
            "≥ 0.5d-5 is installed in the same Python environment, or "
            "override the search path via the ORCHO_PROFILES_V2_PATH "
            "env var."
        )
        return ProfilesListResult(
            profiles=[], selectors=_profile_selectors(),
            source="missing", diagnostic=diagnostic,
        )
    # Field-for-field projection of the SDK summary onto the wire record.
    # ``list_profiles`` already excludes the ``auto-detect`` selector token,
    # keeping ``profiles`` and ``selectors`` disjoint. ``phases`` is a tuple
    # from the SDK → materialise as list; ``description`` collapses the SDK's
    # empty string back to ``None``; ``hypothesis`` is the SDK's
    # ``{'attempts', 'format'}`` dict (or None), coerced to
    # ``ProfileHypothesisRecord`` by Pydantic. ``summary.isolated`` has no
    # ProfileRecord field and is intentionally ignored.
    records = [
        ProfileRecord(
            name=summary.name,
            phases=list(summary.phases),
            kind=summary.kind,
            variant=summary.variant,
            description=summary.description or None,
            cross_gates=summary.cross_gates,
            hypothesis=summary.hypothesis,
            semantic_profile=summary.semantic_profile,
            default_mode=summary.default_mode,
            recipe_kind=summary.recipe_kind,
            internal=summary.internal,
        )
        for summary in list_profiles()
    ]
    return ProfilesListResult(
        profiles=records, selectors=_profile_selectors(), source="json_v2",
    )


__all__ = [
    "get_profiles_list",
    "get_project_skills",
    "get_run_history",
    "get_workspace_info",
]
