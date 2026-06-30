"""Unit tests for the ``orcho_prompts_resolve`` MCP tool.

Walks the 3-level prompt chain (project → workspace → core). Surfaces
the chain (with ``exists`` flags) plus content of the first existing
entry.
"""
from __future__ import annotations

import pytest

from orcho_mcp.tools import orcho_prompts_resolve


def test_prompts_resolve_falls_back_to_core():
    """A core prompt always exists; chain ends with core/exists=True."""
    # Use a known core prompt name. List available ones to pick the first.
    from core.io.prompt_loader import list_core_prompts
    names = list_core_prompts()
    if not names:
        pytest.skip("no core prompts shipped — nothing to resolve")

    r = orcho_prompts_resolve(name=names[0])
    assert r.name == names[0]
    assert any(c.level == "core" and c.exists for c in r.chain)
    assert r.resolved_text  # non-empty winner content


def test_prompts_resolve_unknown_prompt_returns_chain_no_winner(tmp_path):
    r = orcho_prompts_resolve(name="this_prompt_definitely_does_not_exist_xyz",
                              project_dir=str(tmp_path))
    assert r.resolved_path is None
    assert r.resolved_text is None
    assert all(not c.exists for c in r.chain)
