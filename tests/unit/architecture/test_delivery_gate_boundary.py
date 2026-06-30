"""Delivery-gate boundary contract — wire adapter vs. domain logic.

The ``orcho_delivery_gate`` MCP surface must keep the same shape as every
other read tool: the handler in ``tools.py`` is a thin wire adapter that
delegates to the domain implementation in ``services/delivery_gate.py``, and
the SDK-state / artifact-read logic lives in the service — never in the
handler.

These guards are structural (AST-level) so they survive refactors and
reviewer fatigue. The generic thin-adapter guard
(``test_tool_body_thinness``) and the no-SDK-in-tools guard
(``test_no_direct_run_state.test_tools_py_stays_wire_adapter``) cover every
handler; this file pins the delivery-gate-specific wiring so a regression
that moves logic into the handler, or bypasses the service, fails loudly.
"""
from __future__ import annotations

import ast
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parents[3] / "src" / "orcho_mcp"
TOOLS_PY = PKG_ROOT / "tools.py"
SERVICE_PY = PKG_ROOT / "services" / "delivery_gate.py"

_HANDLER = "orcho_delivery_gate"
_DOMAIN_FN = "project_delivery_gate"


def _tools_tree() -> ast.AST:
    return ast.parse(TOOLS_PY.read_text(encoding="utf-8"))


def _find_handler(tree: ast.AST) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            node.name == _HANDLER
        ):
            return node
    return None


def test_service_module_owns_the_domain_function() -> None:
    """``project_delivery_gate`` is defined in ``services/delivery_gate.py``."""
    assert SERVICE_PY.is_file(), f"{SERVICE_PY} missing"
    tree = ast.parse(SERVICE_PY.read_text(encoding="utf-8"))
    defined = {
        n.name for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert _DOMAIN_FN in defined, (
        f"{_DOMAIN_FN} must be defined in services/delivery_gate.py — the "
        "delivery-gate domain implementation home."
    )


def test_handler_exists_and_delegates_to_service() -> None:
    """The ``orcho_delivery_gate`` handler is a single delegating return.

    Body is exactly ``[docstring?, return project_delivery_gate(...)]`` — no
    classification, artifact reads, or multi-statement plumbing in the wire
    adapter.
    """
    handler = _find_handler(_tools_tree())
    assert handler is not None, (
        f"{_HANDLER} handler not found in tools.py — the read-tool must be "
        "registered as an @mcp.tool there."
    )
    body = handler.body
    if body and isinstance(body[0], ast.Expr) and isinstance(
        body[0].value, ast.Constant,
    ):
        body = body[1:]
    assert len(body) == 1 and isinstance(body[0], ast.Return), (
        f"{_HANDLER} must be a single delegating ``return`` — push any logic "
        "into services/delivery_gate.py."
    )
    ret = body[0]
    assert isinstance(ret.value, ast.Call), (
        f"{_HANDLER} must return a direct call to the domain function."
    )
    func = ret.value.func
    called = func.attr if isinstance(func, ast.Attribute) else (
        func.id if isinstance(func, ast.Name) else ""
    )
    assert called == _DOMAIN_FN, (
        f"{_HANDLER} must delegate to {_DOMAIN_FN}; got {called!r}."
    )


def test_handler_imports_domain_fn_from_service() -> None:
    """tools.py imports ``project_delivery_gate`` from the service module."""
    tree = _tools_tree()
    ok = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "orcho_mcp.services.delivery_gate"
        and any(a.name == _DOMAIN_FN for a in node.names)
        for node in ast.walk(tree)
    )
    assert ok, (
        f"tools.py must import {_DOMAIN_FN} from "
        "orcho_mcp.services.delivery_gate."
    )


def test_service_imports_only_sdk_and_artifact_boundary() -> None:
    """The service may call SDK state, but not engine internals or tools.

    Gate kind and action availability come from
    ``sdk.delivery_decision_state``. Artifact reads still route through
    ``services.run_artifacts``. The service must not import ``pipeline`` /
    ``core`` directly, nor the ``tools`` wire adapter.
    """
    tree = ast.parse(SERVICE_PY.read_text(encoding="utf-8"))
    forbidden_roots = ("pipeline", "core", "orcho_mcp.tools")
    offenders: list[str] = []
    for node in ast.walk(tree):
        modules: list[str] = []
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
        elif isinstance(node, ast.Import):
            modules.extend(a.name for a in node.names)
        for module in modules:
            if any(
                module == root or module.startswith(f"{root}.")
                for root in forbidden_roots
            ):
                offenders.append(f"line {node.lineno}: {module}")
    assert not offenders, (
        "services/delivery_gate.py must route policy through sdk and durable "
        "artifact reads through services.run_artifacts, not import "
        "pipeline/core/tools directly. "
        f"Offenders: {offenders}"
    )
