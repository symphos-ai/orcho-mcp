"""orcho_mcp.prompts — orcho ``_prompts/*.md`` exposed as MCP prompts.

Each core prompt becomes a named MCP prompt the client can offer in its UI
(for example slash menus or prompt pickers). When the
user picks one, the handler resolves it through the standard 3-level chain
(project → workspace → core) and returns the winning template content.

Why surface prompts AT ALL given orcho already uses them inside the
pipeline: the prompts encode our methodology (architect / decomposer / QA
gates / build / fix / review). Exposing them through MCP lets a Claude
Code user invoke the same patterns directly in chat — "use the orcho
architect prompt for this task" — without running a full pipeline. It's
the cheapest distribution of orcho's positioning angle (workflow + QA
gates) into Claude Code's day-to-day surface.

Registration is dynamic (one MCP prompt per file in ``_prompts/``) — done
once at server startup via ``register_all_prompts()``. The ``project_dir``
argument is optional and only affects chain resolution; without it, only
the core layer is consulted.
"""
from __future__ import annotations

from core.io.prompt_loader import list_core_prompts, resolution_chain

from orcho_mcp.instance import mcp


def _resolve_prompt(name: str, project_dir: str | None) -> str:
    """Walk the 3-level chain and return the first existing template's content."""
    chain = resolution_chain(name, project_dir=project_dir)
    for _level, path, exists in chain:
        if exists:
            return path.read_text(encoding="utf-8")
    # No level produced a file. Surface a clear message rather than an
    # empty prompt — the client UI shows whatever we return verbatim.
    return f"[orcho] prompt '{name}' not found in project / workspace / core."


def _make_handler(prompt_name: str):
    """Return a closure that resolves ``prompt_name`` through the chain.

    Each call to this factory opens a fresh local scope, so the inner
    handler's free variable ``prompt_name`` resolves to the argument of
    *this specific call* — no late-binding pitfall, no default-arg hack.
    (FastMCP rejects parameter names starting with ``_``, so the
    default-arg trick wouldn't fly anyway.)
    """
    def handler(project_dir: str | None = None) -> str:
        return _resolve_prompt(prompt_name, project_dir)

    handler.__name__ = prompt_name
    handler.__doc__ = (
        f"Orcho '{prompt_name}' prompt. Resolves through project → workspace → core; "
        "pass ``project_dir`` to include project/workspace overrides."
    )
    return handler


def register_all_prompts() -> list[str]:
    """Register one MCP prompt per ``_prompts/*.md`` file. Idempotent.

    Returns the list of registered prompt names so callers (smoke tests,
    docs generation) can introspect what was wired up.
    """
    registered: list[str] = []
    for name in list_core_prompts():
        handler = _make_handler(name)
        # FastMCP's ``mcp.prompt(name=...)`` returns a decorator; calling it
        # immediately on the handler is the programmatic-registration path.
        mcp.prompt(
            name=name,
            description=(
                f"Orcho '{name}' prompt template. "
                "Resolves project → workspace → core."
            ),
        )(handler)
        registered.append(name)
    return registered


__all__ = ["register_all_prompts"]
