---
name: orcho-mcp
description: "Use when editing orcho-mcp MCP server behavior: tools/resources/prompts registration, schemas, tools.py, resources, prompts.py, supervisor, run control, event tail, docs/mcp_schema.json, stdio transport, progressToken notifications, or MCP client-facing payloads. Pair with orcho-core-sdk-wire when core shape changes."
---

# Orcho MCP

Own the MCP exposure layer for Orcho.

## First Reads

- `orcho-mcp/AGENTS.md`
- more specific `orcho-mcp/**/AGENTS.md` near touched code
- `orcho-mcp/tests/AGENTS.md` before changing tests
- `orcho-mcp/src/orcho_mcp/instance.py`
- `orcho-mcp/src/orcho_mcp/tools.py`
- `orcho-mcp/src/orcho_mcp/schemas/`
- changed module

## Owns

- MCP tools/resources/prompts
- Pydantic wire schemas
- `docs/mcp_schema.json`
- supervisor and run-control exposure
- stdio framing and registration
- progress notifications

## Does Not Own

- core SDK dataclass source -> `orcho-core-sdk-wire`
- core gate/evidence/runtime meaning -> relevant core specialist
- final integrity gate -> `orcho-integrity-pipeline`

## Invariants

- Shared `FastMCP` instance lives in `instance.py`.
- Do not `print()` in handler code paths; stdout is protocol frames.
- Keep `python -m orcho_mcp` working.
- Tool/resource/prompt catalog changes need registration/schema smoke.

## Verification

- From `orcho-mcp`: `python -m pytest -q tests/unit`
- From `orcho-mcp`: `python -m pytest -q tests/integration/protocol/test_schema_snapshot.py` when schema/catalog changes
- From `orcho-mcp`: run registration/list-tools smoke when tools/resources/prompts change
- From `orcho-mcp`: run stdio E2E when protocol framing/logging can break
- From `orcho-mcp`: `python -m pytest -q -m mcp_integration` when subprocess/mock lifecycle changes

## Neighbor Skills

- `orcho-core-sdk-wire` when core-side public shape changes
- `orcho-core-evidence-observability` for evidence meaning exposed through MCP
- `orcho-core-quality-gates` for gate meaning exposed through MCP
