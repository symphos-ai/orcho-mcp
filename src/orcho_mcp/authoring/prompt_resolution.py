"""orcho_mcp.authoring.prompt_resolution — prompt-chain resolution entry.

Sync public function ``resolve_prompt`` backs the
``orcho_prompts_resolve`` MCP tool. Walks the 3-level prompt
resolution chain (project → workspace → core) via
``core.io.prompt_loader.resolution_chain``, returns every chain entry
with its ``exists`` flag, and surfaces the content of the first
existing file.

``OSError`` while reading the winning file maps to ``resolved_text =
None`` (path is still surfaced) — preserves the original tools.py
fidelity. No match in any level leaves both resolved fields ``None``.

This module is distinct from ``orcho_mcp.prompts``, which handles
dynamic FastMCP prompt registration.
"""
from __future__ import annotations

from core.io.prompt_loader import resolution_chain

from orcho_mcp.schemas import PromptChainEntry, PromptResolveResult


def resolve_prompt(
    name: str,
    project_dir: str | None = None,
) -> PromptResolveResult:
    """Resolve a prompt template through the 3-level chain.

    See ``orcho_prompts_resolve`` docstring in ``orcho_mcp.tools`` for
    the wire contract.
    """
    chain = resolution_chain(name, project_dir=project_dir)
    chain_records = [
        PromptChainEntry(level=lvl, path=str(p), exists=ex)
        for (lvl, p, ex) in chain
    ]

    resolved_path: str | None = None
    resolved_text: str | None = None
    for _lvl, p, ex in chain:
        if ex:
            resolved_path = str(p)
            try:
                resolved_text = p.read_text(encoding="utf-8")
            except OSError:
                resolved_text = None
            break

    return PromptResolveResult(
        name=name,
        chain=chain_records,
        resolved_path=resolved_path,
        resolved_text=resolved_text,
    )


__all__ = ["resolve_prompt"]
