# orcho-mcp tests Instructions

## Scope

This file applies to `tests/`.

Also obey `../AGENTS.md` and the workspace-level `../../AGENTS.md`.

## Testing Style

The canonical guide is `../docs/testing.md`. The layout map is `README.md`.

Tests should make the architecture executable. When a production boundary is
important, prefer a small structural test over a convention that depends on
review memory.

## Layout

- `unit/` mirrors production domains under `src/orcho_mcp/`.
- `integration/protocol/` covers MCP protocol and stdio behavior.
- `acceptance/mock_pipeline/` covers subprocess-backed mock pipeline flows.
- `fixtures/` contains shared builders and transport helpers.

Do not add `__init__.py` under `tests/`.

## Fixtures

Use `tests/fixtures/mcp_workspace.py` for synthetic run state:

- `write_run(...)`
- `meta(...)`
- `metrics(...)`
- `event(...)`
- `supervisor_state(...)`

Use `tests/fixtures/stdio.py::initialized_stdio_session` for L3 stdio tests.
Keep stdio setup explicit in the test body.

## Architecture Guards

Architecture tests under `unit/architecture/` are contracts. If a change needs
to relax a guard, update the guard and the relevant architecture doc in the same
diff, with the new intended boundary stated plainly.

Run `pytest -q tests/unit/architecture` after touching adapters, resources,
schemas, package imports, or test layout.

## Behavioral Tests

- Use table-driven tests for cursor, pagination, and status-transition behavior.
- Keep L4 tests small and operator-readable; they should cover lifecycle traces
  that would be costly to diagnose after release.
- Mark subprocess mock-pipeline tests with `pytest.mark.mcp_integration`.
