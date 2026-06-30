"""orcho_mcp.authoring — developer-facing utilities for preparing work.

Backs two MCP tool handlers (``orcho_plan_validate``,
``orcho_prompts_resolve``) that live in ``orcho_mcp.tools`` as thin
shims. Both surfaces help author/prepare work before or around a run:
validate a plan document and resolve prompt templates through the
project / workspace / core chain. Neither is run lifecycle, observe,
inspection, or resource registration — they share the "authoring"
namespace because both wrap one-shot library calls into MCP-shaped
results without touching run state.

Sibling modules:

- ``plan_validation`` — ``validate_plan_document``.
- ``prompt_resolution`` — ``resolve_prompt``.

Keep this ``__init__`` empty — callers import full paths so each
symbol has exactly one canonical home and grep stays useful.
"""
