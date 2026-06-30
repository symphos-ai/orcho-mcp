"""Import graph boundary contract — per-domain forbidden edges.

Generalises the per-file ``test_tools_py_stays_wire_adapter`` guard
into a domain-wide direction check. Every Python module under
``src/orcho_mcp/<domain>/`` has its imports walked; an import that
lands in ``FORBIDDEN_EDGES[domain]`` fails the build.

The contract direction encodes:

- **Implementation domains never import the wire adapter.** Services
  / observe / run_control / inspection / authoring / supervisor must
  not import ``orcho_mcp.tools`` — the dependency goes
  tools → domains, never back.
- **Services never reach into the resource adapter** and vice versa.
  The two adapter surfaces (``resources/``, ``tools.py``) share
  business logic through the ``services/`` layer, not through each
  other.
- **Resources never speak SDK.** SDK reads are consolidated in
  ``services/``; ``resources/`` modules call services, which call
  SDK. This mirrors the ``test_resources_boundary`` rule but uses
  the per-domain matcher so the same machinery covers every domain.
- **schemas/ depends only on stdlib + pydantic + schemas.shared.**
  Wire models must not pull in any implementation domain, the SDK,
  ``core``, or ``pipeline`` — pulling them in drags business
  semantics into the wire layer.

This is the **floor** — forbidden edges. A future tightening can
flip to a positive allowlist once the per-domain dependency sets
stabilise. The forbidden-edge form is intentionally narrow: it
encodes the rules whose violation would be a real architectural
regression, and stays silent about edges that don't matter today.

What is NOT enforced (and why):

- Edges between operation modules WITHIN a domain (e.g. supervisor's
  ``spawn`` → ``state``). Those are private to the package and
  guarded by ``test_supervisor_boundary``.
- Imports from ``orcho_mcp.errors`` and ``orcho_mcp.instance``
  anywhere. Errors are the typed exception surface and ``instance``
  is the shared FastMCP handle; both are legitimately ambient.
- Imports of stdlib / pydantic / third-party libs. Not relevant to
  the contract.

The matcher mirrors ``_matches_forbidden_module`` in
``test_no_direct_run_state.py``: a forbidden root matches the root
itself plus the ``f"{root}."`` prefix, with the trailing-dot guard
preventing false positives on look-alike names (``sdkish``,
``orcho_mcp.toolsX``).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

PKG_ROOT = Path(__file__).resolve().parents[3] / "src" / "orcho_mcp"


# Per-domain forbidden import targets. The matcher catches the
# bare root + any submodule via prefix.
FORBIDDEN_EDGES: dict[str, set[str]] = {
    "services": {
        "orcho_mcp.tools",
        "orcho_mcp.resources",
    },
    "resources": {
        "orcho_mcp.tools",
        "sdk",
    },
    "observe": {
        "orcho_mcp.tools",
    },
    "run_control": {
        "orcho_mcp.tools",
    },
    "inspection": {
        "orcho_mcp.tools",
    },
    "authoring": {
        "orcho_mcp.tools",
    },
    "supervisor": {
        "orcho_mcp.tools",
        # Subprocess lifecycle stays below the read/projection layers: the
        # supervisor owns ``mcp_supervisor.json`` and never renders
        # observe summaries, projects the read-model, or constructs wire
        # schemas. Those flow supervisor → services/observe at read time,
        # never the reverse.
        "orcho_mcp.observe",
        "orcho_mcp.services.run_projection",
        "orcho_mcp.schemas",
    },
    "schemas": {
        "orcho_mcp.tools",
        "orcho_mcp.resources",
        "orcho_mcp.services",
        "orcho_mcp.observe",
        "orcho_mcp.run_control",
        "orcho_mcp.inspection",
        "orcho_mcp.authoring",
        "orcho_mcp.supervisor",
        "sdk",
        "core",
        "pipeline",
    },
}


def _matches_forbidden(module: str, forbidden: set[str]) -> bool:
    """``module`` equals a forbidden root OR is its submodule.

    Submodule match uses the ``f"{root}."`` prefix to catch nested
    paths (``sdk.runs``, ``orcho_mcp.services.run_reads``) without
    falling for look-alike names (``sdkish``,
    ``orcho_mcp.toolsX``) — the trailing dot is the boundary.
    """
    return any(module == root or module.startswith(f"{root}.")
               for root in forbidden)


def _iter_imports(tree: ast.AST) -> list[tuple[int, str]]:
    """Yield ``(lineno, module_path)`` for every Import / ImportFrom."""
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module:
                out.append((node.lineno, module))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                out.append((node.lineno, alias.name))
    return out


def _domain_files(domain: str) -> list[Path]:
    """Return every .py file under ``src/orcho_mcp/<domain>/`` (recursive)."""
    root = PKG_ROOT / domain
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def test_forbidden_edges_cover_every_implementation_domain() -> None:
    """Belt-and-braces: ``FORBIDDEN_EDGES`` must enumerate every
    sub-package under ``src/orcho_mcp/`` that is reachable for a
    user-facing handler chain.

    Catches a layout refactor that adds a new sub-package (e.g.
    ``orcho_mcp/new_domain/``) without giving it a forbidden-edge
    entry. Without this guard, a new domain would silently bypass
    the import-direction contract.

    Sub-packages legitimately exempt from the contract belong to
    the implicit "always allowed to import anywhere" set (``errors``,
    ``instance``, plus stdlib-shaped helpers). Add an exception below
    only with a written justification.
    """
    EXEMPT_SUBDIRS = {
        # Onboarding markdown — not a Python sub-package boundary.
        "_onboarding",
    }
    subdirs = {
        p.name for p in PKG_ROOT.iterdir()
        if p.is_dir() and not p.name.startswith("__") and p.name not in EXEMPT_SUBDIRS
    }
    mapped = set(FORBIDDEN_EDGES)
    missing = subdirs - mapped
    assert not missing, (
        "New sub-package(s) under src/orcho_mcp/ have no entry in "
        f"FORBIDDEN_EDGES: {sorted(missing)}. Add the domain to "
        "FORBIDDEN_EDGES with at least ``{\"orcho_mcp.tools\"}`` as "
        "the floor, or document why the new package is exempt."
    )


@pytest.mark.parametrize("domain", sorted(FORBIDDEN_EDGES))
def test_domain_imports_respect_forbidden_edges(domain: str) -> None:
    """Every ``.py`` under ``src/orcho_mcp/<domain>/`` is AST-walked;
    no import may target a module in ``FORBIDDEN_EDGES[domain]``
    (root or submodule). Failures point at the file:line so the
    refactor target is obvious.
    """
    forbidden = FORBIDDEN_EDGES[domain]
    files = _domain_files(domain)
    if not files:
        pytest.fail(
            f"No .py files found under src/orcho_mcp/{domain}/ — "
            "domain dir vanished or matcher misconfigured."
        )

    offenders: list[str] = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(PKG_ROOT).as_posix()
        for lineno, module in _iter_imports(tree):
            if _matches_forbidden(module, forbidden):
                offenders.append(f"{rel}:{lineno}: imports {module}")

    assert not offenders, (
        f"`{domain}` imports a forbidden module:\n  "
        + "\n  ".join(offenders)
        + f"\n\nForbidden roots for `{domain}`: {sorted(forbidden)}.\n"
        + "Push the call into the right layer (services/ for SDK reads, "
        + "the matching domain for handler logic) and update "
        + "docs/architecture/mcp_boundaries.md if the architecture has "
        + "genuinely changed."
    )
