"""orcho_mcp.services.pending_decisions — workspace pending-decision projector.

Backs the ``orcho_workspace_pending_decisions`` MCP read tool. It recovers
every run currently paused on ``status=awaiting_phase_handoff`` straight
from the runs' durable artifacts — ``meta.json`` (merged with supervisor
truth) plus the ``phase_handoff_decisions/`` directory — so a captain can
re-find pending operator decisions without relying on chat memory or the
advisory ``mcp/state.json`` cache.

It is an *artifact* projector, not an advisory one: every row is built by
reusing the shared :func:`project_pending_handoff` read-model (handoff id /
phase / trigger / verdict / round label / available actions /
decision-artifact flag / suggested next action), and the per-row
``next_actions`` are decision-coherent with the resume pre-flight guard and
``orcho_run_diagnose``:

- decision artifact already recorded ⇒ a ready ``orcho_run_resume`` call;
- no decision yet ⇒ an ``operator_input_required``
  ``orcho_phase_handoff_decide`` carrying the available verbs.

Bounded by construction: the scan stops at a run-directory ceiling, the
returned rows are capped, and ``truncated`` / ``scanned_count`` expose the
bound. No row carries raw findings, reviewer output, event payloads, or
logs — only the compact projected fields.

Default vs forensic view: the default answer to "what needs my decision now"
returns only *actionable* rows — runs whose recorded project path still
exists and lives under the resolved workspace scope. For a standard
``workspace-orchestrator`` layout, that scope includes both the orchestrator
directory itself and its parent project group, where the sibling project
repos live. Non-actionable paused runs (no/missing project, a temp/scratch
project path, or a project outside the workspace scope) are hidden but never
silently dropped: each is tallied in ``hidden_count`` and its per-reason
breakdown. The forensic ``include_stale`` escape hatch returns the hidden
rows too, each carrying its real ``classification``.

Classification is deterministic with a fixed rule order in which
**workspace-valid beats temp**: a run whose project exists under the
workspace scope is ``actionable`` even when that scope itself lives in a
temp/test directory (the fixture blocker). The hidden counters are computed
by ``classification`` over the scan window only (bounded by ``_SCAN_CAP``) and
are therefore a window-local tally, not a global count; they are computed
identically whether or not ``include_stale`` returns the hidden rows.

Defensive: a corrupt / unreadable single run is skipped (mirrors the
``_safe_*`` pattern in ``services.run_projection``) so one bad run never
collapses the whole scan. The workspace root is resolved defensively — when
it cannot be resolved, the workspace-relative rules (actionable-by-root and
out_of_workspace) switch off while temp/missing classification keeps working.
"""
from __future__ import annotations

import os
from fnmatch import fnmatch
from pathlib import Path

from sdk import load_meta as _sdk_load_meta

from orcho_mcp.schemas import (
    NextActionRecord,
    WorkspacePendingDecisionRow,
    WorkspacePendingDecisionsResult,
)
from orcho_mcp.services.run_lookup import (
    runs_dir_or_raise,
    workspace_root_or_none,
)
from orcho_mcp.services.run_projection import (
    PendingHandoffProjection,
    project_pending_handoff,
)
from orcho_mcp.services.status_merge import merged_status_from_meta

# The single paused status this projector recovers — mirrors
# ``run_projection._PENDING_HANDOFF_STATUS`` (kept local so this module does
# not reach into the projection owner's private constant).
_PENDING_HANDOFF_STATUS = "awaiting_phase_handoff"

# Scan / payload bounds. The scan ceiling caps how many run directories we
# examine (newest first), and the row limit caps how many paused rows we
# return — both keep the payload bounded on a large workspace.
_SCAN_CAP = 200
_DEFAULT_ROW_LIMIT = 50

# Classification labels (mirror ``WorkspacePendingDecisionRow.classification``).
_ACTIONABLE = "actionable"
_MISSING_PROJECT = "missing_project"
_TEMP_PROJECT = "temp_project"
_OUT_OF_WORKSPACE = "out_of_workspace"

# The standard workspace-init layout is:
#   <project-group>/workspace-orchestrator/runspace/runs
# with target projects as siblings of ``workspace-orchestrator``.
_WORKSPACE_SUBDIR_NAME = "workspace-orchestrator"

# Temp/scratch path roots a project path under which marks the run as a
# throwaway demo/test run rather than an operator-actionable workspace run.
# ``$TMPDIR`` (when set) is appended at classification time.
_TEMP_ROOTS = (
    "/private/var/folders",
    "/var/folders",
    "/tmp",
    "/private/tmp",
)
# pytest's tmp-path factory names directories ``pytest-of-<user>`` /
# ``pytest-<n>``; either segment anywhere in the path marks a test run.
_PYTEST_SEGMENT_GLOBS = ("pytest-of-*", "pytest-*")


def _resolve_row_limit(limit: int | None) -> int:
    """Clamp the caller's optional ``limit`` to a sane bounded range.

    ``None`` → the default cap. Anything below 1 clamps to 1; anything
    above the scan ceiling clamps to it (a row can only exist for a scanned
    run, so a larger request can never surface more than ``_SCAN_CAP`` rows).
    """
    if limit is None:
        return _DEFAULT_ROW_LIMIT
    if limit < 1:
        return 1
    return min(limit, _SCAN_CAP)


def _coerce_project(raw: object) -> str | None:
    """Coerce ``meta.project`` to a non-empty str, else ``None``."""
    return raw if isinstance(raw, str) and raw else None


def _safe_resolve(path: Path | None) -> Path | None:
    """Resolve ``path`` defensively, falling back to the unresolved path.

    ``resolve()`` normalises symlinks (e.g. macOS ``/var`` → ``/private/var``)
    so workspace-relative comparisons line up on both sides. Any OS error
    degrades to the unresolved path rather than raising.
    """
    if path is None:
        return None
    try:
        return path.resolve()
    except OSError:
        return path


def _is_under(child: Path | None, parent: Path | None) -> bool:
    """``True`` when ``child`` is ``parent`` or lives beneath it; else ``False``.

    Defensive: a non-comparable pair (different anchors) or an OS error
    answers ``False`` instead of raising.
    """
    if child is None or parent is None:
        return False
    try:
        return child.is_relative_to(parent)
    except (ValueError, OSError):
        return False


def _workspace_scope_roots(ws_root: Path | None) -> tuple[Path, ...]:
    """Return roots that count as workspace-owned for pending decisions.

    ``ORCHO_WORKSPACE`` points at the orchestrator directory, but the normal
    multi-project layout keeps the actual project repos beside it under the
    parent project group. A paused run for such a sibling repo is still an
    operator-actionable run for this workspace, so both roots are considered.

    Custom test/demo workspaces that do not use the ``workspace-orchestrator``
    directory name keep the old narrower behaviour: only the workspace root
    itself is authoritative.
    """
    resolved_root = _safe_resolve(ws_root)
    if resolved_root is None:
        return ()

    roots = [resolved_root]
    if resolved_root.name == _WORKSPACE_SUBDIR_NAME:
        parent = _safe_resolve(resolved_root.parent)
        if parent is not None and parent not in roots:
            roots.append(parent)
    return tuple(roots)


def _is_temp_path(proj_path: Path, resolved_proj: Path | None) -> bool:
    """``True`` when the project path is a temp/scratch or pytest path.

    Checks both the raw and the resolved path against the temp roots (plus
    ``$TMPDIR`` when set) and looks for a pytest tmp-factory segment anywhere
    in either path. All comparisons are defensive.
    """
    candidates = [p for p in (proj_path, resolved_proj) if p is not None]
    roots: list[str] = list(_TEMP_ROOTS)
    tmpdir = os.environ.get("TMPDIR")
    if tmpdir:
        roots.append(tmpdir)
    for cand in candidates:
        for part in cand.parts:
            if any(fnmatch(part, glob) for glob in _PYTEST_SEGMENT_GLOBS):
                return True
        for root in roots:
            if _is_under(cand, Path(root)):
                return True
    return False


def classify(project: str | None, ws_root: Path | None) -> str:
    """Classify a paused run's project path deterministically.

    Rules are applied in a fixed order, and **workspace-valid beats temp**.
    The ordering matters: a legitimate workspace that happens to have been
    launched from a temp/test directory (so the workspace scope resolves under
    ``/private/var/folders`` etc.) must classify as ``actionable`` *before*
    the temp heuristic can hide it — otherwise real working runs vanish from
    the default inbox (the fixture blocker). The order is:

    1. ``actionable`` — ``project`` is recorded, its path exists, ``ws_root``
       resolved, and the path lies under the workspace scope.
    2. ``temp_project`` — the path is under a temp root (``/private/var/folders``,
       ``/var/folders``, ``/tmp``, ``/private/tmp``, ``$TMPDIR``) or contains a
       pytest tmp segment (``pytest-of-*`` / ``pytest-*``).
    3. ``missing_project`` — ``project`` is ``None`` or its path does not exist.
    4. ``out_of_workspace`` — ``ws_root`` resolved and the path is not under
       the workspace scope.

    Otherwise ``actionable`` (e.g. ``ws_root`` could not be resolved, so the
    workspace-relative rules are disabled and we do not hide the row).

    Defensive throughout: every ``Path`` operation swallows OS errors, and a
    ``None`` ``ws_root`` disables rules 1-by-scope and 4 while temp/missing
    classification keeps working.
    """
    proj_path: Path | None = None
    if project is not None:
        try:
            proj_path = Path(project)
        except (TypeError, ValueError):
            proj_path = None

    proj_exists = False
    if proj_path is not None:
        try:
            proj_exists = proj_path.exists()
        except OSError:
            proj_exists = False

    resolved_proj = _safe_resolve(proj_path)
    scope_roots = _workspace_scope_roots(ws_root)

    # RULE 1 — actionable (workspace-valid), checked before the temp heuristic.
    if (
        proj_path is not None
        and proj_exists
        and scope_roots
        and any(_is_under(resolved_proj, root) for root in scope_roots)
    ):
        return _ACTIONABLE

    # RULE 2 — temp/scratch project path.
    if proj_path is not None and _is_temp_path(proj_path, resolved_proj):
        return _TEMP_PROJECT

    # RULE 3 — missing project (no path recorded, or it no longer exists).
    if proj_path is None or not proj_exists:
        return _MISSING_PROJECT

    # RULE 4 — exists but outside the resolved workspace scope.
    if scope_roots:
        return _OUT_OF_WORKSPACE

    # Workspace root unresolved: workspace-relative rules are off — do not hide.
    return _ACTIONABLE


def _candidate_run_dirs(runs_dir: Path) -> list[Path]:
    """Run directories under ``runs_dir``, newest id first.

    Directories are sorted by name descending so the most recent runs are
    examined first (run ids are timestamp-prefixed). Dotfiles and
    non-directories are skipped. A failure enumerating the directory yields
    an empty list so the projector degrades to "nothing pending" rather than
    raising.
    """
    try:
        entries = [
            e for e in runs_dir.iterdir()
            if e.is_dir() and not e.name.startswith(".")
        ]
    except OSError:
        return []
    entries.sort(key=lambda p: p.name, reverse=True)
    return entries


def _row_next_actions(
    run_id: str, pending: PendingHandoffProjection,
) -> list[NextActionRecord]:
    """Typed, decision-coherent next step for one paused row.

    Coherent with the resume pre-flight guard / ``orcho_run_diagnose``:
    once a decision artifact exists the run only needs resume, so the row
    carries a ready ``orcho_run_resume``; otherwise it carries an
    ``operator_input_required`` ``orcho_phase_handoff_decide`` whose
    ``choices`` are the runtime's available verbs (the operator still picks a
    verb and supplies feedback where the verb requires it).
    """
    if pending.decision_state == "degraded":
        return [
            NextActionRecord(
                intent="Inspect the decision-read failure before attempting a mutation.",
                tool="orcho_run_diagnose", args={"run_id": run_id}, optional=False,
                kind="ready_call",
            ),
        ]
    if pending.decision_artifact_exists:
        return [
            NextActionRecord(
                intent=(
                    "A phase-handoff decision is already recorded — resume "
                    "to apply it and continue the run."
                ),
                tool="orcho_run_resume",
                args={"run_id": run_id},
                optional=False,
                kind="ready_call",
            ),
        ]

    args: dict[str, object] = {"run_id": run_id}
    if pending.handoff_id:
        args["handoff_id"] = pending.handoff_id
    return [
        NextActionRecord(
            intent=(
                "Resolve the paused phase handoff (choose an action; supply "
                "feedback where the verb requires it)."
            ),
            tool="orcho_phase_handoff_decide",
            args=args,
            optional=False,
            kind="operator_input_required",
            requires_operator_input=True,
            choices=list(pending.available_actions),
        ),
    ]


def _pending_row(run_dir: Path) -> WorkspacePendingDecisionRow | None:
    """Build one paused-run row, or ``None`` when the run is not paused.

    Reads the run's ``meta.json`` once for the merged status filter and the
    project path, then reuses :func:`project_pending_handoff` for the
    operator read-model. Returns ``None`` for any run that is not actually
    paused on a phase handoff so the caller skips it.
    """
    run_id = run_dir.name
    meta = _sdk_load_meta(run_dir) or {}
    if not isinstance(meta, dict):
        return None
    status = merged_status_from_meta(meta, run_dir)
    if status != _PENDING_HANDOFF_STATUS:
        return None

    pending = project_pending_handoff(run_id)
    if not pending.is_pending_handoff:
        return None

    return WorkspacePendingDecisionRow(
        run_id=run_id,
        project=_coerce_project(meta.get("project")),
        handoff_id=pending.handoff_id,
        phase=pending.phase,
        trigger=pending.trigger,
        verdict=pending.verdict,
        round_label=pending.round_label,
        available_actions=list(pending.available_actions),
        decision_artifact_exists=pending.decision_artifact_exists,
        decision_state=pending.decision_state,
        decision_degraded_reason=pending.decision_degraded_reason,
        suggested_next_action=pending.suggested_next_action,
        next_actions=_row_next_actions(run_id, pending),
    )


def _safe_pending_row(run_dir: Path) -> WorkspacePendingDecisionRow | None:
    """Project one row, swallowing read errors to ``None``.

    Defensive: a corrupt / partial meta or decisions read for a single run
    must skip that run, never collapse the whole scan (mirrors the
    ``_safe_*`` helpers in ``services.run_projection``).
    """
    try:
        return _pending_row(run_dir)
    except Exception:
        return None


def project_pending_decisions(
    limit: int | None = None,
    include_stale: bool = False,
) -> WorkspacePendingDecisionsResult:
    """Recover every run paused awaiting an operator phase-handoff decision.

    See the ``orcho_workspace_pending_decisions`` tool docstring (in
    ``orcho_mcp.tools``) for the wire contract. Enumerates the runs
    directory newest-first up to a bounded ceiling, selects the
    ``awaiting_phase_handoff`` runs from their merged artifacts, classifies
    each by its durable project path, and builds a capped list of bounded
    rows with decision-coherent ``next_actions``.

    By default (``include_stale=False``) only ``actionable`` rows are
    returned — the operator's real decision inbox. Non-actionable rows are
    hidden but counted: ``hidden_count`` and its per-reason breakdown tally
    every non-actionable paused run *within the scan window*, computed by
    ``classification`` and therefore identical whether or not
    ``include_stale`` surfaces those rows. With ``include_stale=True`` the
    hidden rows are returned too, each carrying its real ``classification``.

    Counting happens over the whole scan window before the visible rows are
    truncated to the row limit, so the counters never depend on the row cap
    or on ``include_stale``. ``WorkspaceNotResolvedError`` propagates from
    ``runs_dir_or_raise`` when no workspace is configured; the workspace root
    used for classification is resolved defensively (``None`` on failure).
    """
    row_limit = _resolve_row_limit(limit)
    runs_dir = runs_dir_or_raise()
    ws_root = workspace_root_or_none()

    # Phase 1 — scan the bounded window, classifying every paused row. The
    # window (capped by ``_SCAN_CAP``) is identical regardless of
    # ``include_stale`` or the row limit, so the hidden counters below are too.
    classified: list[tuple[WorkspacePendingDecisionRow, str]] = []
    scanned = 0
    truncated = False
    for entry in _candidate_run_dirs(runs_dir):
        if scanned >= _SCAN_CAP:
            # More directories remain but the scan ceiling stopped us.
            truncated = True
            break
        scanned += 1
        row = _safe_pending_row(entry)
        if row is None:
            continue
        classified.append((row, classify(row.project, ws_root)))

    # Hidden counters are computed by ``classification`` over the scan window,
    # always — independent of which rows visibility actually returns.
    hidden_missing = sum(1 for _, c in classified if c == _MISSING_PROJECT)
    hidden_temp = sum(1 for _, c in classified if c == _TEMP_PROJECT)
    hidden_out = sum(1 for _, c in classified if c == _OUT_OF_WORKSPACE)

    # Phase 2 — select visible rows (newest-id-first order preserved) and cap
    # them. Default view shows only actionable rows; forensic view shows all
    # rows, each stamped with its real classification.
    rows: list[WorkspacePendingDecisionRow] = []
    for row, classification in classified:
        if classification != _ACTIONABLE and not include_stale:
            continue
        if len(rows) >= row_limit:
            # More visible paused runs exist than the row limit allows.
            truncated = True
            break
        row.classification = classification
        rows.append(row)

    return WorkspacePendingDecisionsResult(
        runs=rows,
        scanned_count=scanned,
        returned_count=len(rows),
        truncated=truncated,
        hidden_count=hidden_missing + hidden_temp + hidden_out,
        hidden_missing_project_count=hidden_missing,
        hidden_temp_project_count=hidden_temp,
        hidden_out_of_workspace_count=hidden_out,
    )


__all__ = ["classify", "project_pending_decisions"]
