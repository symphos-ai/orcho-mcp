"""Unit tests for _prompts/*.md exposed as MCP prompts.

L1: handler closures, project-aware chain resolution, registration count.
The stdio E2E for prompts is in test_initialize_handshake.py.
"""
from __future__ import annotations

from core.io.prompt_loader import list_core_prompts

from orcho_mcp.prompts import _make_handler, register_all_prompts


def test_register_all_prompts_returns_at_least_one():
    """orcho-core ships at least one prompt; registration must surface them all."""
    names = register_all_prompts()
    core_names = list_core_prompts()
    assert set(core_names) <= set(names)


def test_register_all_prompts_is_idempotent():
    """Calling twice must not raise; FastMCP overwrites by name."""
    first = register_all_prompts()
    second = register_all_prompts()
    assert first == second


def test_handler_resolves_core_when_no_project():
    """With project_dir=None, the handler reads the core ``_prompts/<n>.md``."""
    name = list_core_prompts()[0]
    handler = _make_handler(name)
    text = handler()
    assert text  # non-empty
    # Sanity: the placeholder for missing prompts wouldn't show up here.
    assert "[orcho] prompt" not in text


def test_handler_falls_back_to_placeholder_for_unknown():
    """Unknown prompt names return a recognisable error string, not empty."""
    handler = _make_handler("definitely_not_a_prompt_xyz")
    text = handler()
    assert "[orcho]" in text
    assert "definitely_not_a_prompt_xyz" in text


def test_handler_uses_project_override(tmp_path):
    """A project-level override at .orcho/multiagent/prompts/<n>.md beats core."""
    name = "tasks/build"
    override_dir = tmp_path / ".orcho" / "multiagent" / "prompts"
    override_dir.mkdir(parents=True)
    sentinel = "PROJECT_OVERRIDE_SENTINEL_TEXT"
    override_file = override_dir / f"{name}.md"
    override_file.parent.mkdir(parents=True, exist_ok=True)
    override_file.write_text(sentinel, encoding="utf-8")

    handler = _make_handler(name)
    text = handler(project_dir=str(tmp_path))
    assert text == sentinel


def test_handler_closure_captures_name():
    """Late-binding of the loop variable would make every handler resolve to
    the last iteration's name. The default-arg trick in _make_handler must
    keep each closure pinned to its own name."""
    name_a = list_core_prompts()[0]
    name_b = list_core_prompts()[-1] if len(list_core_prompts()) > 1 else name_a

    handler_a = _make_handler(name_a)
    handler_b = _make_handler(name_b)
    assert handler_a.__name__ == name_a
    assert handler_b.__name__ == name_b
