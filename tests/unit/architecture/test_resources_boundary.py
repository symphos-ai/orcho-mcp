"""Resource-layer boundary gate.

``orcho_mcp/resources/`` is the URI-to-Python-callable adapter layer for
the MCP ``resources/`` capability. To stay symmetric with ``tools.py``
(thin wire adapter; business logic in ``services/`` + ``observe/`` +
``inspection/``), every resource handler must delegate to a service.
This file enforces that contract structurally:

1. **No SDK imports** in ``resources/`` — read paths go through
   ``services.run_artifacts`` / ``services.read_queries``.
2. **No ``orcho_mcp.tools`` imports** in ``resources/`` — resources
   speak to services, not to peer adapters.
3. **No direct file reads** (``open(``, ``Path.read_text(``,
   ``Path.read_bytes(``, ``glob.glob(``) — file I/O belongs in
   ``services/run_artifacts``.
4. **Soft size tripwire** at ``MAX_LINES`` per file. Warning only —
   if a resource module needs to grow past the threshold for a
   defensible reason, raise the threshold here in the same change so
   the discussion happens.
"""
from __future__ import annotations

import ast
import warnings
from pathlib import Path

import pytest

PKG_ROOT = Path(__file__).resolve().parents[3] / "src" / "orcho_mcp"
RESOURCES_ROOT = PKG_ROOT / "resources"

MAX_LINES = 140

# Attribute-call names that constitute direct file I/O on whatever the
# receiver is. ``Path(...).open()`` and ``some_path.read_text()`` both
# match — we do not narrow on the receiver because the boundary rule
# is "no direct file I/O from resources/", regardless of how the path
# is constructed. ``open`` covers ``Path.open(...)``; bare ``open(...)``
# is handled separately as an ``ast.Name`` call below.
FORBIDDEN_DIRECT_IO_ATTRS = {
    "read_text",
    "read_bytes",
    "open",
    "glob",
    "rglob",
}

# Module-level call patterns. ``glob.glob(...)`` / ``glob.iglob(...)``
# are caught by attribute matching; ``from glob import glob`` followed
# by a bare ``glob(...)`` lands as an ``ast.Call`` on an ``ast.Name``,
# so we keep a separate name-set for that shape.
FORBIDDEN_BARE_CALL_NAMES = {
    "open",
    "glob",
    "iglob",
}


def _resource_files() -> list[Path]:
    return [
        p for p in RESOURCES_ROOT.rglob("*.py")
        if "__pycache__" not in p.parts
    ]


@pytest.mark.parametrize("path", _resource_files(), ids=lambda p: p.name)
def test_resources_do_not_import_sdk(path: Path) -> None:
    """Resources must not ``from sdk import ...`` or ``import sdk``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "sdk" or module.startswith("sdk."):
                pytest.fail(
                    f"{path.relative_to(PKG_ROOT).as_posix()}: forbidden "
                    f"`from {module} import …`. Resources go through "
                    "``orcho_mcp.services.run_artifacts`` (or another "
                    "service) — add the helper there if it does not "
                    "exist yet."
                )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "sdk" or alias.name.startswith("sdk."):
                    pytest.fail(
                        f"{path.relative_to(PKG_ROOT).as_posix()}: "
                        f"forbidden `import {alias.name}`. Route reads "
                        "through ``orcho_mcp.services``."
                    )


@pytest.mark.parametrize("path", _resource_files(), ids=lambda p: p.name)
def test_resources_do_not_import_tools(path: Path) -> None:
    """Resources must not import the sibling ``orcho_mcp.tools`` adapter.

    Catches both shapes:
        from orcho_mcp.tools import something
        import orcho_mcp.tools  # (and ``import orcho_mcp.tools as foo``)
    """
    rel = path.relative_to(PKG_ROOT).as_posix()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "orcho_mcp.tools" or module.startswith(
                "orcho_mcp.tools."
            ):
                pytest.fail(
                    f"{rel}: forbidden `from {module} import …`. "
                    "Resources and tools are peer adapters — share "
                    "through ``orcho_mcp.services``."
                )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "orcho_mcp.tools" or alias.name.startswith(
                    "orcho_mcp.tools."
                ):
                    pytest.fail(
                        f"{rel}: forbidden `import {alias.name}`. "
                        "Resources and tools are peer adapters — "
                        "route through ``orcho_mcp.services``."
                    )


@pytest.mark.parametrize("path", _resource_files(), ids=lambda p: p.name)
def test_resources_do_not_read_files_directly(path: Path) -> None:
    """Resources must not perform direct file I/O of any common shape.

    Catches every routine path-to-bytes / discovery-walk call pattern
    that could let a resource handler quietly bypass
    ``services/run_artifacts``:

    * Bare calls: ``open(...)``, ``glob(...)``, ``iglob(...)``
      (the last two appear after ``from glob import glob``).
    * Attribute calls on any receiver:
      ``.read_text(...)``, ``.read_bytes(...)``, ``.open(...)``
      (covers both ``Path(...).open(...)`` and ``path.open()``),
      ``.glob(...)``, ``.rglob(...)`` (covers ``Path.glob`` /
      ``Path.rglob`` walks plus ``glob.glob(...)`` / ``glob.iglob(...)``
      since those land as attribute calls on the ``glob`` module).
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    rel = path.relative_to(PKG_ROOT).as_posix()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Name)
            and func.id in FORBIDDEN_BARE_CALL_NAMES
        ):
            pytest.fail(
                f"{rel}:{node.lineno} calls bare ``{func.id}(...)`` — "
                "move the read to ``orcho_mcp.services.run_artifacts`` "
                "(or ``run_lookup`` for discovery walks)."
            )
        if (
            isinstance(func, ast.Attribute)
            and func.attr in FORBIDDEN_DIRECT_IO_ATTRS
        ):
            pytest.fail(
                f"{rel}:{node.lineno} calls ``.{func.attr}(...)`` — "
                "move the read to ``orcho_mcp.services.run_artifacts`` "
                "(or ``run_lookup`` for discovery walks)."
            )


@pytest.mark.parametrize("path", _resource_files(), ids=lambda p: p.name)
def test_resources_stay_thin(path: Path) -> None:
    """Soft tripwire — warn if a resource file grows beyond ``MAX_LINES``.

    Not a hard failure: file-size limits drift over time and a hard
    cap encourages superficial splits. The warning surfaces the growth
    so a real review can ask whether the file is still a thin adapter
    or has started absorbing service logic.
    """
    line_count = len(path.read_text(encoding="utf-8").splitlines())
    if line_count > MAX_LINES:
        warnings.warn(
            f"{path.relative_to(PKG_ROOT).as_posix()}: {line_count} lines "
            f"exceeds the {MAX_LINES}-line resource soft cap. If the "
            "extra lines are still adapter glue, raise MAX_LINES in "
            "test_resources_boundary.py in the same change; otherwise "
            "extract the heavy logic into ``orcho_mcp/services/``.",
            stacklevel=2,
        )
