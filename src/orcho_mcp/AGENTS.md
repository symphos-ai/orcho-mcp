# orcho_mcp source Instructions

## Scope

This file applies to `src/orcho_mcp/`.

Also obey `../../AGENTS.md` and the workspace-level `../../../AGENTS.md`.

## Package Boundaries

`tools.py` is the MCP tool adapter. Every `@mcp.tool` handler stays thin:
docstring plus one `return` or `return await` into a domain function.

Implementation belongs in the matching package:

| Package | Owns |
| --- | --- |
| `services/` | SDK-backed reads, artifact reads, and shared read helpers |
| `observe/` | event summaries, watch behavior, handoff hints, advisory observations |
| `run_control/` | start, resume, cancel, and phase-handoff decisions |
| `inspection/` | evidence and diff inspection |
| `authoring/` | plan validation and prompt resolution |
| `resources/` | MCP resource adapters |
| `schemas/` | Pydantic wire models |
| `supervisor/` | subprocess lifecycle and supervisor state |

## Source Rules

- `resources/` does not import `sdk` and does not read run files directly; use
  `services/`.
- `schemas/` contains wire models only. It must not import SDK, pipeline, core
  runtime code, or implementation domains.
- Implementation domains do not import `orcho_mcp.tools`.
- `instance.py` owns the shared `FastMCP` instance. Registration happens through
  the server and package registration modules.
- Do not use `print()` in server or handler code paths; stdout is protocol data.

## Verification

Run `pytest -q tests/unit/architecture` after changing package boundaries,
imports, schemas, tools, resources, registration, or supervisor composition.
Run the relevant domain tests alongside the architecture tests.
