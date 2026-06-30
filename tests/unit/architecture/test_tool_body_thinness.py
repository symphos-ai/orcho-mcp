"""Tool-handler body thinness contract — AST shape.

``orcho_mcp/tools.py`` is a thin MCP wire adapter. Every
``@mcp.tool``-decorated function must look exactly like:

    @mcp.tool()
    def orcho_some_tool(...) -> ReturnSchema:
        \"\"\"User-visible MCP tool description.\"\"\"
        return _delegate.some_tool(...)

or, for async handlers:

    @mcp.tool()
    async def orcho_some_tool(...) -> ReturnSchema:
        \"\"\"User-visible MCP tool description.\"\"\"
        return await _delegate.some_tool(...)

Implementation goes in the matching domain module (``services/``,
``observe/``, ``run_control/``, ``inspection/``, ``authoring/``).

The companion guard ``test_no_direct_run_state.test_tools_py_stays_
wire_adapter`` already catches reintroduced SDK / raw-event / parser
imports. This guard catches the orthogonal smell of logic creeping
back into handler bodies via allowed imports — guards, loops,
multi-statement plumbing.

What is enforced (AST shape, structural — not a size cap):

1. **Exactly one statement after the docstring.** The body is
   ``[Expr(Constant(str))]?, Return``. No assignments, loops,
   ``if``, ``try``, ``with``, nested defs, or local imports.
2. **The statement is a ``Return``.**
3. **For async handlers, the return value is ``Await``** wrapping
   a call.
4. **The called target's name does not start with ``orcho_``.**
   ``orcho_*`` is the @mcp.tool naming convention; a handler that
   calls another handler is composing wire adapters and almost
   certainly belongs in a service.

Scanner discipline: only functions whose ``decorator_list`` contains
``mcp.tool`` (bare ``Attribute``) or ``mcp.tool(...)`` (a ``Call``
whose ``func`` is the attribute) are in scope. Neighbouring
helpers and compatibility shims in ``tools.py`` (``_find_run_dir``,
``_runs_dir_or_raise``, …) are NOT swept up — they're not
decorated, and future helpers must not silently inherit this strict
contract.

Update protocol: if a handler legitimately needs more shape (a
guard, a local import for monkeypatch surface), push the new logic
into a domain module first. The handler stays the delegation; the
new behaviour lives behind the call. If the new shape genuinely
belongs in ``tools.py`` (which is rare — the guard is
intentionally hostile to that idea), relax this test in the same
diff with a written justification.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

PKG_ROOT = Path(__file__).resolve().parents[3] / "src" / "orcho_mcp"
TOOLS_PY = PKG_ROOT / "tools.py"


def _is_mcp_tool_decorator(node: ast.expr) -> bool:
    """Match ``@mcp.tool`` or ``@mcp.tool(...)``.

    Decorator AST shapes:
      ``@mcp.tool``      → ``Attribute(Name('mcp'), 'tool')``
      ``@mcp.tool()``    → ``Call(Attribute(Name('mcp'), 'tool'))``
      ``@mcp.tool(...)`` → same as above, with args
    """
    if isinstance(node, ast.Call):
        node = node.func
    if not isinstance(node, ast.Attribute):
        return False
    if node.attr != "tool":
        return False
    return isinstance(node.value, ast.Name) and node.value.id == "mcp"


def _is_docstring(stmt: ast.stmt) -> bool:
    """The conventional position-0 docstring on a function body."""
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and isinstance(stmt.value.value, str)
    )


def _tool_handlers() -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """Return ``(handler_name, ast_node)`` for every @mcp.tool function."""
    tree = ast.parse(TOOLS_PY.read_text(encoding="utf-8"))
    out: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(_is_mcp_tool_decorator(d) for d in node.decorator_list):
            out.append((node.name, node))
    return out


def _non_docstring_body(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.stmt]:
    """Body minus the docstring (when present)."""
    body = node.body
    if body and _is_docstring(body[0]):
        return body[1:]
    return body


def _called_name(call: ast.Call) -> str:
    """Extract the rightmost callable name from a Call expression.

    ``foo()``         → ``"foo"``
    ``mod.foo()``     → ``"foo"``
    ``a.b.foo()``     → ``"foo"``
    Anything else     → ``""`` (catches dynamic dispatch shapes we
    don't expect in a thin-adapter body).
    """
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def test_tools_py_has_handlers_to_scan() -> None:
    """Belt-and-braces: an empty handler list would make the per-handler
    parametrize trivially pass. Catch a tools.py emptied by a refactor.
    """
    handlers = _tool_handlers()
    assert handlers, (
        f"No @mcp.tool-decorated functions found in {TOOLS_PY}. "
        "Either tools.py was gutted or the decorator pattern changed."
    )


@pytest.mark.parametrize(
    "name,node",
    _tool_handlers(),
    ids=lambda x: x if isinstance(x, str) else "node",
)
def test_tool_handler_is_single_return(
    name: str,
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> None:
    """Body is exactly ``[Return]`` after the optional docstring.

    Catches: assignments, ``if`` guards, ``try`` blocks, ``with``
    blocks, loops, local imports, nested defs, multi-statement
    plumbing. Each of those belongs in a domain module behind the
    delegation, not in the wire adapter.
    """
    body = _non_docstring_body(node)
    assert len(body) == 1, (
        f"{name} at tools.py:{node.lineno} has {len(body)} non-"
        f"docstring statement(s); the thin-adapter contract is "
        "exactly one ``return``. Push the additional logic into a "
        "domain module (services / observe / run_control / "
        "inspection / authoring) and shrink the handler back to a "
        "delegation."
    )
    assert isinstance(body[0], ast.Return), (
        f"{name} at tools.py:{node.lineno}: single body statement "
        f"must be ``Return``; got {type(body[0]).__name__}."
    )


@pytest.mark.parametrize(
    "name,node",
    _tool_handlers(),
    ids=lambda x: x if isinstance(x, str) else "node",
)
def test_tool_handler_return_shape(
    name: str,
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> None:
    """The return value is a direct call to a domain function.

    - Sync handler: ``return foo(...)`` — ``Return(Call(...))``.
    - Async handler: ``return await foo(...)`` —
      ``Return(Await(Call(...)))``.
    - The called name must NOT start with ``orcho_`` — that prefix
      is the @mcp.tool naming convention, and a handler calling
      another handler is composing wire adapters (the logic belongs
      in a service that both handlers can call).
    """
    body = _non_docstring_body(node)
    assert body, f"{name}: empty body — earlier test should have caught this"
    ret = body[0]
    assert isinstance(ret, ast.Return), "earlier test should have caught this"
    assert ret.value is not None, (
        f"{name} at tools.py:{node.lineno}: ``return`` without a value "
        "is not a delegation; the handler must return a wire model."
    )

    value = ret.value
    if isinstance(node, ast.AsyncFunctionDef):
        assert isinstance(value, ast.Await), (
            f"{name} at tools.py:{node.lineno}: async handler must "
            "return ``await <call>``; got "
            f"{type(value).__name__}."
        )
        call = value.value
    else:
        call = value

    assert isinstance(call, ast.Call), (
        f"{name} at tools.py:{node.lineno}: return value must be a "
        f"direct call; got {type(call).__name__}."
    )

    target = _called_name(call)
    assert target, (
        f"{name} at tools.py:{node.lineno}: could not extract called "
        "name (dynamic dispatch?). Thin adapters delegate to named "
        "functions; refactor the call shape."
    )
    assert not target.startswith("orcho_"), (
        f"{name} at tools.py:{node.lineno} delegates to ``{target}`` — "
        "names starting with ``orcho_`` are the @mcp.tool naming "
        "convention. A handler calling another handler is composing "
        "wire adapters; push the shared logic into a service that "
        "both handlers can call."
    )
