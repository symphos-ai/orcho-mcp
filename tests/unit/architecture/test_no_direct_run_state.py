"""Negative-import gate for MCP read paths.

MCP read tools must speak SDK; they cannot quietly reintroduce direct
parsing of ``meta.json`` / ``metrics.json`` / ``evidence.json`` or
custom run-discovery walks. This test enforces the architectural rule
structurally so it survives refactors and reviewer fatigue.

The gate operates at AST level — it inspects every ``ImportFrom`` node
in ``orcho_mcp/`` and refuses to ship if a forbidden symbol slips in.

Allow-listed exceptions (each with a documented reason):

- ``orcho_mcp/supervisor/`` keeps its own subprocess machinery;
  the negative gate ignores symbols only relevant to *read* paths.
-- ``pipeline.plan_parser`` stays allowed in the authoring domain
  because ``orcho_plan_validate`` validates plans directly.

The gate also bans MCP-side helpers named ``_load_json`` / ``_load_meta``
/ ``_load_metrics`` / ``_load_evidence`` that would re-implement the
parsing; SDK's ``load_meta`` and ``get_run_metrics`` are the canonical
entry points.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

PKG_ROOT = Path(__file__).resolve().parents[3] / "src" / "orcho_mcp"


# (module_path_prefix, attribute_name) — banned imports in MCP read paths.
FORBIDDEN_IMPORTS: set[tuple[str, str]] = {
    # Direct run-state model access — must go through SDK.
    ("core.observability.metrics", "load_historical_runs"),
    ("core.observability.metrics", "format_history_table"),
    ("core.observability.metrics", "MetricsCollector"),
    ("pipeline.evidence", "collect_evidence"),
    ("pipeline.evidence", "render_evidence_md"),
    ("pipeline.evidence", "validate_bundle"),
}

# Files where a forbidden import is allow-listed. Empty set means "no
# exceptions" — the rule applies to every Python file under orcho_mcp/.
PER_FILE_EXEMPTIONS: dict[str, set[tuple[str, str]]] = {}

# Symbol names a parallel-parser MCP helper would use. The SDK's
# ``load_meta`` and ``get_run_metrics`` are the only sanctioned entry
# points — these names are reserved for accidental rebirth.
FORBIDDEN_HELPER_NAMES = {
    "_load_json",
    "_load_meta",
    "_load_metrics",
    "_load_evidence",
    "_walk_runs_dir",
}


def _python_files() -> list[Path]:
    return [p for p in PKG_ROOT.rglob("*.py") if "__pycache__" not in p.parts]


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.name)
def test_no_direct_run_state_imports(path: Path) -> None:
    """Every ``from X import Y`` in MCP must avoid forbidden run-state imports."""
    rel = path.relative_to(PKG_ROOT).as_posix()
    exemptions = PER_FILE_EXEMPTIONS.get(rel, set())

    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.module is None:
            continue
        module = node.module
        for alias in node.names:
            pair = (module, alias.name)
            if pair in FORBIDDEN_IMPORTS and pair not in exemptions:
                pytest.fail(
                    f"{rel}: forbidden import `from {module} import {alias.name}`. "
                    "MCP read tools must speak SDK — use the equivalent "
                    "``from sdk import …`` symbol. If the SDK "
                    "doesn't yet expose this surface, file a SDK gap and "
                    "do a small core pre-step before re-introducing the "
                    "direct import here."
                )


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.name)
def test_no_parallel_parser_helpers(path: Path) -> None:
    """Reject MCP helpers that would re-implement run-state parsing."""
    rel = path.relative_to(PKG_ROOT).as_posix()
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name in FORBIDDEN_HELPER_NAMES:
            pytest.fail(
                f"{rel}:{node.lineno} defines `{node.name}` — that name "
                "is reserved for the parallel-parser anti-pattern this "
                "gate exists to prevent. Use ``from sdk import "
                "load_meta, get_run_metrics, collect_evidence`` instead. "
                "If you need a name like this for unrelated reasons, "
                "rename it and update FORBIDDEN_HELPER_NAMES in this test."
            )


APPROVED_SDK_SENTINEL_SURFACE: tuple[Path, ...] = (
    PKG_ROOT / "tools.py",
    PKG_ROOT / "services" / "run_lookup.py",
    PKG_ROOT / "services" / "read_queries.py",
    PKG_ROOT / "services" / "run_events.py",
    PKG_ROOT / "services" / "run_reads.py",
    PKG_ROOT / "services" / "status_merge.py",
    PKG_ROOT / "observe" / "summary.py",
    PKG_ROOT / "observe" / "handoff_hints.py",
    PKG_ROOT / "observe" / "watch.py",
    PKG_ROOT / "observe" / "observation.py",
    PKG_ROOT / "inspection" / "evidence.py",
    PKG_ROOT / "inspection" / "diff.py",
)


def test_sdk_sentinel_symbols_present_in_approved_surface() -> None:
    """Approved SDK surface for read-path sentinel symbols.

    The expected set ({find_run, find_runs_dir, get_run_metrics,
    read_run_events, list_history, load_meta, load_status}) is the minimum
    SDK vocabulary every MCP read path depends on. The gate checks that
    the *union* of ``from sdk import …`` / ``from sdk.run_control import …``
    statements across files in ``APPROVED_SDK_SENTINEL_SURFACE`` contains
    the expected set — otherwise someone has deleted a read tool and
    removed the SDK import, and the negative-import gate would go silent
    because there's nothing to compare against.

    ``read_run_events`` is sourced from ``sdk.run_control`` (the Stage-5
    run-control read model that ``services/run_events.py`` routes
    through), not from the top-level ``sdk`` namespace, so the scan
    accepts both module paths.

    This is NOT a global allowlist of SDK imports across the
    package — other modules (e.g. ``resources/runs.py`` for per-run
    artefacts, ``supervisor/`` for run lifecycle) also import from
    ``sdk`` legitimately. ``APPROVED_SDK_SENTINEL_SURFACE`` just
    enumerates the canonical read-path implementation files where the
    sentinel symbols must collectively live. A file inside the tuple
    is free to import zero SDK symbols (e.g. ``tools.py``,
    ``observe/observation.py``, ``services/status_merge.py``); the
    union check is what matters.
    """
    expected = {
    "find_run", "find_runs_dir", "get_run_metrics",
        "read_run_events", "list_history", "load_cross_execution_graph",
        "load_cross_execution_graph_state", "load_meta", "load_status",
    }
    seen: set[str] = set()
    for path in APPROVED_SDK_SENTINEL_SURFACE:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in (
                "sdk", "sdk.run_control",
            ):
                for alias in node.names:
                    seen.add(alias.name)
    missing = expected - seen
    assert not missing, (
        f"SDK symbols missing across approved read surface: "
        f"{sorted(missing)}. Restore the import in tools.py, in one of "
        "services/{run_lookup,read_queries,run_events,run_reads,status_merge}.py, "
        "in one of observe/{summary,handoff_hints,watch,observation}.py, "
        "or in one of inspection/{evidence,diff}.py."
    )


def test_no_raw_event_read_imports() -> None:
    """Run-event reads must go through ``sdk.list_events``.

    Direct core event-store reads couple MCP to the engine's internal
    JSONL replay helper. Supervisor append-side writes are a different
    contract and may continue to use ``append_event``.
    """
    offenders: list[str] = []
    for path in _python_files():
        rel = path.relative_to(PKG_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module != "core.observability.events":
                    continue
                names = {alias.name for alias in node.names}
                if "read_all" in names or "*" in names:
                    offenders.append(
                        f"{rel}:{node.lineno}: from {module} import "
                        + ", ".join(sorted(names))
                    )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "core.observability.events":
                        offenders.append(
                            f"{rel}:{node.lineno}: import {alias.name}"
                        )
    assert not offenders, (
        "MCP read-side event replay must use ``sdk.list_events`` via "
        "``orcho_mcp.services.run_events``. Offenders:\n  "
        + "\n  ".join(offenders)
    )


def test_package_scan_targets_are_present() -> None:
    """Guard: parametrized gates must have something to scan.

    Without this, an off-by-one in ``PKG_ROOT`` (e.g. after a layout
    migration) would silently empty the parametrize list, and the
    architectural gate above would report "no tests ran" instead of
    failing. Keep the guard explicit so the intent is load-bearing.
    """
    assert _python_files(), f"no package files found under {PKG_ROOT}"


# Every implementation domain lives outside tools.py; the file is a
# pure MCP adapter layer. This gate fails fast if SDK calls, raw event
# reads, or plan-parser internals drift back into tools.py.
_TOOLS_PY_FORBIDDEN_MODULES: frozenset[str] = frozenset({
    "sdk",
    "core.observability.events",
    "pipeline.plan_parser",
})


def _matches_forbidden_module(module: str) -> bool:
    """Return True if ``module`` is a forbidden root or any of its submodules.

    Match logic uses exact equality plus a ``f"{forbidden}."`` prefix
    so submodule paths (``sdk.status``,
    ``core.observability.events.reader``,
    ``pipeline.plan_parser.contract``) are caught alongside the bare
    root. The trailing-dot guard prevents false positives on
    similarly-named-but-unrelated packages like ``sdkish`` or
    ``pipeline.plan_parser_extra``.
    """
    return any(
        module == forbidden or module.startswith(f"{forbidden}.")
        for forbidden in _TOOLS_PY_FORBIDDEN_MODULES
    )


def test_tools_py_stays_wire_adapter() -> None:
    """``orcho_mcp.tools`` must remain a thin MCP adapter layer.

    Every domain previously implemented in tools.py now lives in a
    dedicated submodule (services/, observe/, run_control/,
    inspection/, authoring/). tools.py keeps only ``@mcp.tool``
    handler definitions whose bodies delegate to those modules.

    Any direct import — exact root **or** submodule path — of:

    - ``sdk`` (read-path SDK belongs in services / inspection / observe);
    - ``core.observability.events`` (raw events belong in services and
      observe);
    - ``pipeline.plan_parser`` (plan parsing belongs in authoring);

    is a regression. The check matches submodule paths too
    (``sdk.status``, ``core.observability.events.reader``, etc.) so a
    future drift cannot slip past by importing the deeper API
    directly.
    """
    tree = ast.parse((PKG_ROOT / "tools.py").read_text(encoding="utf-8"))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if _matches_forbidden_module(module):
                names = ", ".join(alias.name for alias in node.names)
                offenders.append(f"line {node.lineno}: from {module} import {names}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if _matches_forbidden_module(alias.name):
                    offenders.append(f"line {node.lineno}: import {alias.name}")
    assert not offenders, (
        "tools.py is the MCP adapter layer and must not import these "
        f"modules directly: {sorted(_TOOLS_PY_FORBIDDEN_MODULES)}. "
        "Move the call into the matching domain module "
        "(services / observe / run_control / inspection / authoring) "
        "and keep the @mcp.tool handler as a one-line delegation.\n"
        "Offenders:\n  " + "\n  ".join(offenders)
    )


def test_forbidden_module_matcher_catches_submodules() -> None:
    """Lock in ``_matches_forbidden_module`` semantics.

    Without this, a future "simplification" that drops the
    ``startswith(f"{forbidden}.")`` branch would silently weaken the
    adapter guard: submodule imports like ``sdk.status`` would slip
    past. The matcher must also stay free of false positives on
    look-alike names
    (``sdkish``, ``pipeline.plan_parser_extra``) — that's what the
    trailing-dot guard buys.
    """
    # Roots and submodules of forbidden roots → caught.
    assert _matches_forbidden_module("sdk")
    assert _matches_forbidden_module("sdk.status")
    assert _matches_forbidden_module("core.observability.events")
    assert _matches_forbidden_module("core.observability.events.reader")
    assert _matches_forbidden_module("pipeline.plan_parser")
    assert _matches_forbidden_module("pipeline.plan_parser.contract")

    # Legitimate imports tools.py / domain modules still need → not caught.
    assert not _matches_forbidden_module("orcho_mcp.services.run_reads")
    assert not _matches_forbidden_module("core.io.prompt_loader")
    assert not _matches_forbidden_module("core.observability.metrics")

    # Look-alike prefixes that share a name fragment but not the path
    # boundary → not caught (false-positive guard).
    assert not _matches_forbidden_module("sdkish")
    assert not _matches_forbidden_module("pipeline.plan_parser_extra")
    assert not _matches_forbidden_module("core.observability.events_v2")


# ── Projection owner: meta.phase_handoff read-model parsing ─────────────────
#
# The pause / handoff read-model (the ``meta.phase_handoff`` payload:
# available_actions / id / phase / trigger / artifacts / findings) is
# parsed in exactly one place — ``services/run_projection.py``.
# ``observe/handoff_hints.py`` renders that read-model; it must not read
# the key itself, and neither may ``run_control`` or any other domain.
# This keeps a single normalisation contract instead of N drifting copies.

PHASE_HANDOFF_PARSE_OWNER = "services/run_projection.py"


def _phase_handoff_key_accesses(tree: ast.AST) -> list[int]:
    """Line numbers where the exact dict key ``"phase_handoff"`` is read.

    Detects the read-model extraction signature — ``x.get("phase_handoff")``
    and ``x["phase_handoff"]``. Uses an *exact* string match so the
    status constant ``"awaiting_phase_handoff"``, the tool name
    ``"orcho_phase_handoff_decide"``, and docstrings that mention
    ``meta.phase_handoff`` in prose never trip the gate.
    """
    out: list[int] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "phase_handoff"
        ):
            out.append(node.lineno)
        elif isinstance(node, ast.Subscript):
            sl = node.slice
            if isinstance(sl, ast.Constant) and sl.value == "phase_handoff":
                out.append(node.lineno)
    return out


def test_phase_handoff_parsed_only_in_projection_owner() -> None:
    """The ``meta.phase_handoff`` key is read in exactly one module.

    ``services/run_projection.py`` owns the pause read-model. Any other
    file under ``orcho_mcp/`` that reaches for the ``phase_handoff`` key
    is re-implementing the projection — push it into the projector and
    consume ``HandoffReadModel`` instead.
    """
    offenders: list[str] = []
    for path in _python_files():
        rel = path.relative_to(PKG_ROOT).as_posix()
        if rel == PHASE_HANDOFF_PARSE_OWNER:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for lineno in _phase_handoff_key_accesses(tree):
            offenders.append(f"{rel}:{lineno}")
    assert not offenders, (
        "meta.phase_handoff read-model parsing must live only in "
        f"{PHASE_HANDOFF_PARSE_OWNER}. Offenders re-parse the key:\n  "
        + "\n  ".join(offenders)
        + "\n\nUse "
        "``orcho_mcp.services.run_projection.project_handoff_read_model`` "
        "and render the returned ``HandoffReadModel`` instead."
    )


def test_phase_handoff_matcher_catches_reparse() -> None:
    """Negative proof: the matcher flags a re-parse and stays quiet on
    the look-alike status constant / tool name / prose.

    If someone moved the ``meta.get("phase_handoff")`` extraction back
    into ``observe`` (or anywhere outside the projector), the gate above
    would fire — this self-test pins that the matcher actually detects
    the pattern, and does not false-positive on the strings that legally
    contain ``phase_handoff`` as a substring.
    """
    bad_get = ast.parse('meta.get("phase_handoff")')
    bad_sub = ast.parse('meta["phase_handoff"]')
    assert _phase_handoff_key_accesses(bad_get)
    assert _phase_handoff_key_accesses(bad_sub)

    # Look-alikes that must NOT trip the gate.
    ok = ast.parse(
        'x = "awaiting_phase_handoff"\n'
        'y = "orcho_phase_handoff_decide"\n'
        'z = meta.get("status")\n'
        '"""docstring mentioning meta.phase_handoff in prose"""\n'
    )
    assert not _phase_handoff_key_accesses(ok)


# ── Error-mapping owner: SDK exception types ────────────────────────────────
#
# The SDK exception types (RunNotFound / NoWorkspace /
# InvalidPhaseHandoffState) are imported — and therefore caught — in one
# translation owner, ``services/errors.py``. The only other files that
# may import them are the direct SDK-call sources that translate at their
# own run-resolution boundary (documented here, analogous to
# APPROVED_SDK_SENTINEL_SURFACE). Catching an SDK type requires importing
# it, so gating the import gates the catch.

SDK_ERROR_TYPES: frozenset[str] = frozenset({
    "RunNotFound", "NoWorkspace", "InvalidPhaseHandoffState",
})

# The translation owner (T2): the single ``map_sdk_errors`` /
# ``map_command_errors`` home.
SDK_ERROR_TYPE_OWNER = "services/errors.py"

# Direct SDK-call sources that resolve / translate at their own boundary.
# Each is a run-lookup or read primitive whose SDK call predates (and is
# narrower than) the shared owner; the catch is local and documented.
SDK_ERROR_TYPE_ALLOWLIST: frozenset[str] = frozenset({
    "services/run_lookup.py",    # find_run_dir / runs_dir_or_raise
    "services/run_events.py",    # read_run_events tail
    "services/read_queries.py",  # get_workspace_info soft NoWorkspace fallback
    # Supervisor operation modules now delegate the detached launch /
    # respawn / signal mechanics to ``sdk.run_control.launch`` and
    # translate its typed errors (NoWorkspace / RunNotFound) into the MCP
    # hierarchy at their own delegation boundary — a run-control primitive
    # that resolves the run and maps at the seam, exactly like the read
    # sources above.
    "supervisor/spawn.py",       # launch_run: NoWorkspace → WorkspaceNotResolved
    "supervisor/resume.py",      # resume_run: RunNotFound → RunNotFoundError
    "supervisor/cancel.py",      # cancel_run: RunNotFound → RunNotFoundError
})


def _sdk_error_type_imports(tree: ast.AST) -> list[str]:
    """``"<lineno>: <name>"`` for every SDK exception-type import.

    Matches ``from sdk import RunNotFound`` and submodule variants
    (``from sdk.foo import NoWorkspace``) regardless of the ``as`` alias.
    """
    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        if node.module != "sdk" and not node.module.startswith("sdk."):
            continue
        for alias in node.names:
            if alias.name in SDK_ERROR_TYPES:
                out.append(f"{node.lineno}: {alias.name}")
    return out


def test_sdk_error_types_caught_only_in_owner() -> None:
    """SDK exception types are imported only by the error-mapping owner
    plus the allow-listed direct SDK-call sources.

    Catching an SDK exception type anywhere else means a domain is doing
    its own SDK→MCP translation instead of routing through
    ``orcho_mcp.services.errors.map_sdk_errors`` — the regression this
    gate prevents.
    """
    allowed = {SDK_ERROR_TYPE_OWNER} | set(SDK_ERROR_TYPE_ALLOWLIST)
    offenders: list[str] = []
    for path in _python_files():
        rel = path.relative_to(PKG_ROOT).as_posix()
        if rel in allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for hit in _sdk_error_type_imports(tree):
            offenders.append(f"{rel}:{hit}")
    assert not offenders, (
        "SDK exception types may be imported/caught only in "
        f"{SDK_ERROR_TYPE_OWNER} (the translation owner) plus the "
        f"documented direct-call allowlist {sorted(SDK_ERROR_TYPE_ALLOWLIST)}. "
        "Offenders translate SDK errors themselves:\n  "
        + "\n  ".join(offenders)
        + "\n\nWrap the SDK call in "
        "``orcho_mcp.services.errors.map_sdk_errors(run_id)`` instead."
    )


def test_sdk_error_type_matcher_catches_import() -> None:
    """Negative proof: the matcher flags an SDK error-type import (aliased
    or not) and ignores unrelated SDK reads / look-alike names."""
    assert _sdk_error_type_imports(ast.parse("from sdk import RunNotFound"))
    assert _sdk_error_type_imports(
        ast.parse("from sdk import NoWorkspace as _SDKNoWorkspace"),
    )
    assert _sdk_error_type_imports(
        ast.parse("from sdk.run_control import InvalidPhaseHandoffState"),
    )
    # SDK read functions and look-alike names are not error types.
    assert not _sdk_error_type_imports(ast.parse("from sdk import load_meta"))
    assert not _sdk_error_type_imports(
        ast.parse("from sdk import list_findings, load_status"),
    )


# ── Command-error owner: run_control delegation wrapping ────────────────────
#
# Run-control commands delegate to the supervisor and must not let a bare
# (non-OrchoMCPError) exception escape — every ``supervisor.spawn`` /
# ``.resume`` / ``.cancel`` await is wrapped in
# ``map_command_errors()`` so a leak (e.g. the invalid cancel-mode
# ``ValueError``) becomes a typed ``InvalidPlanError`` /
# ``PipelineSpawnError``. The behavioural proof (invalid mode →
# InvalidPlanError) lives in ``tests/unit/run_control/test_lifecycle_tools.py``;
# this structural gate keeps the wrapping from silently disappearing.

RUN_CONTROL_DIR = "run_control"
_COMMAND_DELEGATIONS = frozenset({"spawn", "resume", "cancel"})


class _CommandWrapChecker(ast.NodeVisitor):
    """Flags ``<name>.spawn|resume|cancel(...)`` calls not lexically inside
    a ``with map_command_errors():`` block."""

    def __init__(self) -> None:
        self._wrap_depth = 0
        self.offenders: list[int] = []

    @staticmethod
    def _is_map_command_with(node: ast.With | ast.AsyncWith) -> bool:
        for item in node.items:
            ctx = item.context_expr
            if isinstance(ctx, ast.Call):
                func = ctx.func
                name = (
                    func.attr if isinstance(func, ast.Attribute)
                    else getattr(func, "id", None)
                )
                if name == "map_command_errors":
                    return True
        return False

    def visit_With(self, node: ast.With) -> None:
        wrap = self._is_map_command_with(node)
        if wrap:
            self._wrap_depth += 1
        self.generic_visit(node)
        if wrap:
            self._wrap_depth -= 1

    visit_AsyncWith = visit_With  # type: ignore[assignment]

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in _COMMAND_DELEGATIONS
            and isinstance(func.value, ast.Name)
            and self._wrap_depth == 0
        ):
            self.offenders.append(node.lineno)
        self.generic_visit(node)


def _unwrapped_command_delegations(tree: ast.AST) -> list[int]:
    checker = _CommandWrapChecker()
    checker.visit(tree)
    return checker.offenders


def test_run_control_wraps_supervisor_delegations() -> None:
    """Every supervisor delegation in ``run_control/`` is wrapped in
    ``map_command_errors()`` so no bare exception leaks out of the MCP
    boundary.
    """
    offenders: list[str] = []
    rc_root = PKG_ROOT / RUN_CONTROL_DIR
    for path in sorted(rc_root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(PKG_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for lineno in _unwrapped_command_delegations(tree):
            offenders.append(f"{rel}:{lineno}")
    assert not offenders, (
        "run_control supervisor delegations must be wrapped in "
        "``with map_command_errors():`` so a bare leak (e.g. an invalid "
        "cancel mode ValueError) is translated to a typed OrchoMCPError. "
        "Unwrapped delegations:\n  " + "\n  ".join(offenders)
    )


def test_command_wrap_matcher_catches_unwrapped_delegation() -> None:
    """Negative proof: the matcher flags an unwrapped supervisor call and
    stays quiet once it is wrapped in ``map_command_errors()``."""
    unwrapped = ast.parse(
        "async def f(sup):\n"
        "    return await sup.cancel(run_id, mode=mode)\n"
    )
    assert _unwrapped_command_delegations(unwrapped)

    wrapped = ast.parse(
        "async def f(sup):\n"
        "    with map_command_errors():\n"
        "        result = await sup.cancel(run_id, mode=mode)\n"
        "    return result\n"
    )
    assert not _unwrapped_command_delegations(wrapped)
