# orcho-mcp Instructions

## Scope

This file applies to `orcho-mcp/`.

`orcho-mcp` is the Apache-2.0 public Model Context Protocol server. It exposes
`orcho-core` to Claude Code, Cursor, Zed, and other MCP-speaking
clients.

Also obey the workspace-level `../AGENTS.md`.

## Local Instruction Files

More specific instructions live near the code they govern:

- `docs/AGENTS.md` — public documentation style and canonical doc ownership.
- `tests/AGENTS.md` — test layer rules, fixtures, and architecture contracts.
- `src/orcho_mcp/AGENTS.md` — source package boundaries and adapter rules.
- `src/orcho_mcp/supervisor/AGENTS.md` — subprocess lifecycle invariants.

## Workspace Development Pipeline

When working on this repo inside the Orcho workspace, follow
`../DEVELOPMENT_PIPELINE.md`. That manual pipeline governs direct source
development only; it is separate from Orcho-managed worktree runs.

## Stable Install Is Read-Only

Stable Orcho is a pipx install (venv at `$HOME/.local/pipx/venvs/orcho`,
shims in `$HOME/.local/bin`). Do not edit files inside that venv directly.
Change the canonical workspace repos and promote with `orcho-promote`; touch
the stable install only when the user explicitly asks to debug or repair it.

## Build, Run, And Test

```bash
pip install -e ".[dev]"
pytest
pytest -m mcp_integration
orcho-mcp
python -m orcho_mcp
```

The default `pytest` suite covers L1 unit, L2 registration, and L3 stdio E2E.
The `mcp_integration` marker runs slower L4 subprocess and `--mock` pipeline
coverage.

Use `python -m orcho_mcp` in tests and smoke checks when reproducibility across
pip-install state matters.

## Test Methodology

Use the four-layer test pyramid. Each layer catches a different class of bug:

| Layer | What | Catches | Misses |
| --- | --- | --- | --- |
| L1 pure unit | Call `@mcp.tool` handlers as plain Python functions with synthetic fixtures | Business logic, edge cases, error mapping, Pydantic round-trip | stdio, registration, capability negotiation, dual-import |
| L2 server registration | In-process `await mcp.list_tools()` against the FastMCP instance | Dual-import bugs, missed imports, JSON Schema generation failures | Wire format, stdio purity |
| L3 stdio E2E | Subprocess plus `mcp.client.stdio.stdio_client`, `ClientSession.initialize()`, `call_tool`, `list_resources`, `list_prompts` | stdout pollution, capability negotiation, JSON-RPC framing | Pipeline lifecycle |
| L4 mock integration | Real subprocess pipeline through Orcho's `--mock` provider | Subprocess lifecycle, watch/progress notifications, race conditions | — |

The detailed testing guide is `docs/testing.md`. Do not ship a change with only
L1 green when L2 or L3 applies. If the LLM client cannot see the MCP surface,
the change is not complete.

## MCP Anti-Patterns

- `FastMCP` must be instantiated in `instance.py`; everything imports the shared
  instance from there. Otherwise one import path can register tools on a
  different instance than the server process exposes.
- Do not use `print()` in handler code paths. stdout is reserved for protocol
  frames. Configure logging to stderr.
- Keep `__main__.py` present. Tests use `python -m orcho_mcp`.
- Preserve `progressToken` lifecycle behavior for progress notifications.
- Keep JSON Schema in `tools/list` aligned with Pydantic models. Snapshot-test
  against `docs/mcp_schema.json` and regenerate the snapshot when contracts
  intentionally change.

## Architecture Boundary Contract

The current architecture is described in `docs/architecture/mcp_boundaries.md`
and enforced by the guards under `tests/unit/architecture/`.

When adding or changing MCP behaviour:

- Tool handlers stay in `tools.py`; implementation lives in the matching domain
  module (`services/`, `observe/`, `run_control/`, `inspection/`, `authoring/`).
- Resource handlers stay in `resources/`; SDK and file reads go through `services/`.
- New production domains add or update `tests/unit/<domain>/` in the same commit.
- Public MCP catalog changes keep `docs/mcp_schema.json` in sync.
- Run `pytest -q tests/unit/architecture` before claiming a change commit-ready.

## Cross-Repo Validation Rule

Every `orcho-core` change that can affect MCP-visible behaviour must ship with
an `orcho-mcp` E2E mock smoke in the same commit, even when the wire format
appears unchanged.

Completion requires:

- Code and tests green.
- Architecture note written when needed.
- Schema/API documented.
- An `orcho-mcp` E2E mock smoke passes for at least one touched mode:

```text
mcp__orcho__orcho_run_start(project_dir=<orcho-core>, mode=<full|task|review>, mock=true, max_rounds=1)
```

Then call `orcho_run_status(run_id)` and verify the session shape.

If types or wire format change, update `orcho-mcp` in the same commit. Use a
bundle-style commit message such as:
`feat(redesign,mcp): <core changes> + matching MCP surface`.

Wire-format-changing work needs the matching schema snapshot and MCP smoke.
Most behavioural-only work only needs the smoke.

## Current API Status

For v0.1.0, read tools, resources, prompts, supervisor-backed run control,
progress over `progressToken`, phase-handoff decisions, inspection slices, and
authoring helpers are part of the active MCP surface. Keep the API additive
until the release line is cut.

## Pointers

- MCP wire schema snapshot: `docs/mcp_schema.json`
- Core cross-project execution flow:
  `../orcho-core/docs/architecture/cross_project_pipeline.md`
