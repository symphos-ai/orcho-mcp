# orcho_mcp supervisor Instructions

## Scope

This file applies to `src/orcho_mcp/supervisor/`.

Also obey `../AGENTS.md`, `../../../AGENTS.md`, and the workspace-level
`../../../../AGENTS.md`.

## Responsibilities

The supervisor owns MCP-side subprocess lifecycle and `mcp_supervisor.json`.
Pipeline-owned `meta.json` remains the source for pipeline status and halt
reason.

`RunsSupervisor` owns shared state:

- `_runs`
- `_project_locks`
- `_max_runs`

Operation modules expose module-level functions that accept the concrete
supervisor as their first argument. Keep operation logic in the operation module;
keep `manager.py` as explicit delegation and state ownership.

## Lifecycle Invariants

- `_singleton` stays in `supervisor/__init__.py`.
- `RunsSupervisor.spawn`, `resume`, `cancel`, `_reap`, and `recover` signatures
  are stable and guarded by architecture tests.
- `_reap` remains a method on `RunsSupervisor` and delegates to
  `lifecycle.reap`.
- `spawn` and `resume` schedule `sup._reap(handle)` so tests and lifecycle
  behavior share one path.
- Cancel checks terminal pipeline status before checking `popen.poll()` or
  sending signals.
- Subprocesses use a separate process group; cancellation targets the group.

## Verification

For changes to spawn, resume, cancel, reap, recovery, process groups, or
supervisor state, run:

```bash
pytest -q tests/unit/supervisor
pytest -q tests/unit/architecture/test_supervisor_boundary.py
pytest -q -m mcp_integration
```

Also run the repo-level lint and whitespace checks before calling the change
ready.
