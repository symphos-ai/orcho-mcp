# orcho-mcp test layout

Tests mirror the package layout: a unit-test directory under `tests/unit/<domain>/` exists for every meaningful source area, whether that area is a sub-package (`src/orcho_mcp/<domain>/`) or a flat module (`src/orcho_mcp/<domain>.py`). The mapping is the contract — moving production code without moving its tests breaks it.

See [docs/testing.md](../docs/testing.md) for the 4-layer test methodology,
architecture-contract philosophy, fixture style, and verification commands.
This file is the **layout map**, not the methodology guide.

## Production ↔ test mapping

| Production surface | Unit test home | Notes |
|---|---|---|
| `src/orcho_mcp/authoring/` | `tests/unit/authoring/` | sub-package |
| `src/orcho_mcp/inspection/` | `tests/unit/inspection/` | sub-package |
| `src/orcho_mcp/observe/` | `tests/unit/observe/` | sub-package |
| `src/orcho_mcp/resources/` | `tests/unit/resources/` | sub-package |
| `src/orcho_mcp/run_control/` | `tests/unit/run_control/` | sub-package |
| `src/orcho_mcp/services/` | `tests/unit/services/` | sub-package |
| `src/orcho_mcp/supervisor/` | `tests/unit/supervisor/` | package split by lifecycle concern: spawn, recovery, cancel, resume, lifecycle |
| `src/orcho_mcp/workflows.py` | `tests/unit/workflows/` | flat module |
| `src/orcho_mcp/workspace_state.py` | `tests/unit/workspace_state/` | flat module |
| `src/orcho_mcp/prompts.py` + `src/orcho_mcp/onboarding.py` | `tests/unit/prompts/` | grouped — prompts/onboarding registration tested together |
| `src/orcho_mcp/client_interactions.py` | `tests/unit/client/` | flat module |
| `src/orcho_mcp/tools.py` | covered indirectly via per-domain test dirs above plus `tests/unit/architecture/` boundary tests | `tools.py` is a thin adapter; its business logic lives in the per-domain services |

## Cross-cutting suites

These do not map to a single production module — they enforce invariants across the package.

| Directory | What it covers |
|---|---|
| `tests/unit/architecture/` | Boundary guards: `tools.py` must not import from `resources/` etc. Add new guard tests here when a new boundary needs locking. |
| `tests/integration/` | L2 (server registration) + L3 (stdio E2E). In-process FastMCP for L2, subprocess stdio for L3. |
| `tests/integration/protocol/` | Protocol-specific (capability negotiation, progressToken lifecycle, JSON-RPC framing). |
| `tests/acceptance/` | L4 — full pipeline behind the MCP boundary. Gated with `@pytest.mark.mcp_integration`. |
| `tests/acceptance/mock_pipeline/` | L4 tests that drive a real `--mock` orcho-core subprocess to validate end-to-end behavior. |
| `tests/fixtures/` | Shared fixtures (`fake_workspace`, `stdio` client) — **not** a test directory itself. |

## Run commands

```bash
pytest                          # default: L1 + L2 + L3 (fast, no subprocess pipelines)
pytest -m mcp_integration       # L4 only (subprocess + --mock pipeline)
pytest tests/unit/<domain>/     # focused: one production area
pytest tests/unit/architecture/ # boundary guards only — catches refactor regressions
```

## Adding a new test

1. Production code lands under `src/orcho_mcp/<domain>/` (or `<domain>.py` for a flat module).
2. Its unit tests land under `tests/unit/<domain>/`. If `<domain>` does not exist as a test directory, create it.
3. If a unit test crosses a domain boundary (e.g. `services` importing from `run_control`), prefer mocking the other domain at the import surface; cross-domain integration belongs in `tests/integration/`.
4. If you add or rename a domain, update this README's table in the same commit. The table is the contract.
