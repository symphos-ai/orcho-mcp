# Anatomy of a request

This document traces two representative MCP requests through every
layer of `orcho-mcp` — from the JSON-RPC frame arriving on stdio
down to the disk artefacts the request leaves behind. It is the
flow-level counterpart to `mcp_boundaries.md` (which describes the
static shape) and to `tests/README.md` (which describes the test
layout).

Read this once before your first non-trivial change. After that, the
architecture guards under `tests/unit/architecture/` are the source of
truth — they enforce what this document describes.

## How clients reach the server

MCP clients — Claude Code, Cursor, Zed, and any process speaking the
MCP stdio protocol via the `mcp` SDK — launch the server as:

```bash
python -m orcho_mcp        # the canonical entry point for tests and tooling
orcho-mcp                   # the installed console script, equivalent
```

Both routes resolve to `src/orcho_mcp/server.py:main()`, which:

1. Parses `--version` and bare invocation args.
2. Calls `_register_handlers()`. `instance.py` itself only owns the
   shared `FastMCP("orcho")` object — registration is deliberately
   driven from `server.py` (after the parser runs) so importing
   `orcho_mcp.server` stays cheap for tests that only need the
   instance handle. `_register_handlers()`:
   - Side-effect imports `orcho_mcp.resources` and `orcho_mcp.tools`,
     wiring the `@mcp.resource` / `@mcp.tool` decorators against the
     shared instance.
   - Calls `orcho_mcp.prompts.register_all_prompts()` — the
     `_prompts/*.md` catalogue is dynamic (one prompt per file), so
     registration is an explicit function call.
   - Side-effect imports `orcho_mcp.onboarding` and
     `orcho_mcp.workflows` for the static `@mcp.prompt` decorators
     (onboarding prompt + five workflow templates).
3. Calls `mcp.run()`, which hands control to FastMCP's stdio loop —
   JSON-RPC frames from stdin, responses to stdout.

stdin and stdout are reserved for the protocol. **No `print()` is
allowed in handler code paths** — a single rogue write to stdout
corrupts the next frame. This is one of the recorded anti-patterns
in `CLAUDE.md`.

## Trace 1 — a read tool: `orcho_run_status`

The simplest non-trivial trace. No subprocess, no locking, no
supervisor in-memory state mutated. Pure read.

```
                ┌─────────────────────────────────┐
client/stdin →  │ FastMCP stdio loop              │
                │ src/orcho_mcp/instance.py       │
                └────────────────┬────────────────┘
                                 │  @mcp.tool dispatch
                                 ▼
                ┌─────────────────────────────────┐
                │ tools.py                        │
                │ orcho_run_status(run_id)        │ thin adapter — one-line
                └────────────────┬────────────────┘ delegation
                                 │
                                 ▼
                ┌─────────────────────────────────┐
                │ services/run_reads.py           │ SDK-backed reads
                │ get_run_status(run_id)          │
                └─────┬───────────────────┬───────┘
                      │                   │
                      ▼                   ▼
            ┌──────────────────┐  ┌─────────────────┐
            │ sdk.load_status  │  │ services/       │
            │ resolves run_id, │  │ status_merge.py │
            │ reads meta +     │  │ meta + super-   │
            │ metrics, computes│  │ visor merge     │
            │ next_actions     │  │ (terminal +     │
            │                  │  │ halt_reason)    │
            └────────┬─────────┘  └────────┬────────┘
                     │                     │
                     └──────────┬──────────┘
                                ▼
            ┌──────────────────────────────────────┐
            │ schemas/read.RunStatus               │
            │ returned directly from the service   │
            │ — tool handler is pure pass-through  │
            └────────────────┬─────────────────────┘
                             │  JSON-RPC response
                             ▼
                       client/stdout
```

Step by step:

1. **Frame arrives.** FastMCP unpacks the JSON-RPC `tools/call`
   request and looks up the registered handler.
2. **Adapter dispatch.** The `@mcp.tool` handler in
   `src/orcho_mcp/tools.py` is a one-line shim that imports and
   delegates to `services/run_reads.py`. The thin-body contract is
   guarded by `test_tool_body_thinness.py`.
3. **One-shot load.** The service calls `sdk.load_status(run_id)`,
   which resolves the run id (honouring `$ORCHO_WORKSPACE` /
   `$ORCHO_WORKTREE` and the recent-runs index), reads `meta.json`
   and `metrics.json`, and returns a `RunStatus` SDK object
   carrying the raw meta, raw metrics, sub-projects, and
   pre-computed `next_actions`. SDK errors (`RunNotFound`,
   `NoWorkspace`) are caught and re-raised as the matching MCP
   error types.
4. **Status / halt-reason merge.** `services/status_merge.py`
   reconciles `meta.status` and `meta.halt_reason` against
   `mcp_supervisor.json` via `merged_status_from_meta` and
   `merged_halt_reason_from_meta`. When the pipeline crashed before
   flushing meta and the supervisor knows the process is dead, the
   merge surfaces the supervisor's terminal status; meta is
   preferred whenever it already carries a terminal value.
5. **Summary projection.** `services/meta_summary.py` projects
   `meta` to the default summary shape for polling: status, halt,
   lineage, gate verdicts, and scalar audit fields stay inline while
   heavy phase bodies are replaced with size/count markers. The tool's
   `include` argument can opt specific body families back in.
6. **Wire model.** The service constructs the `schemas.read.RunStatus`
   Pydantic model directly — `next_actions` are passed through
   verbatim from the SDK's `RunStatus.next_actions` (state-derived,
   no transformation). The `@mcp.tool` handler in `tools.py` is a
   pass-through that returns the model unchanged.
7. **Serialise + send.** FastMCP serialises against the JSON Schema
   in `docs/mcp_schema.json` and writes the response frame to stdout.

**What this trace does NOT touch:**

- The supervisor singleton (the merge reads the on-disk state file,
  not the in-memory `_runs` dict).
- Any subprocess.
- Any lock — read paths are lock-free.
- `events.jsonl` — `orcho_run_status` returns status summary + metrics only.
  Event reads go through the parallel trace below.

## Trace 1b — a read tool that reads events: `orcho_run_events_tail`

Identical shape to `orcho_run_status` for the first four steps, but
the SDK call differs. Worth its own trace because it is the path
that closed the only outstanding architectural exception in the
read-state guard.

```
tools.py
  └─→ services/run_reads.py
        └─→ services/run_events.py       ← MCP-side wrapper
              └─→ sdk.list_events         ← public SDK surface
                    └─→ core.observability.events.read_all
                        (engine internal — only the SDK reaches here)
```

Two boundaries matter here:

- **`services/run_events.py`** maps SDK errors to MCP error types:
  `sdk.RunNotFound → errors.RunNotFoundError`,
  `sdk.NoWorkspace → errors.WorkspaceNotResolvedError`. Every read
  path that touches events crosses this boundary; no MCP module
  reaches into `core.observability.events` directly.
- **`sdk.list_events`** owns the public surface. The returned
  `RunEvent` dataclass is the embedder-facing wire shape; the
  internal `core.observability.events.Event` stays free to evolve.

The architectural guard `test_no_raw_event_read_imports` in
`tests/unit/architecture/test_no_direct_run_state.py` enforces this:
any `from core.observability.events import read_all` anywhere in
`src/orcho_mcp/` fails CI.

Write-side participation (`append_event`, used by the supervisor to
emit `run.orphaned` / `run.supervisor_reaped` markers) is a
deliberately separate contract and is **not** routed through SDK.
The guard ignores write-side imports.

## Trace 2 — a spawn: `orcho_run_start`

The full lifecycle path. Touches every architectural layer.

```
                ┌─────────────────────────────────┐
client/stdin →  │ FastMCP stdio loop              │
                └────────────────┬────────────────┘
                                 │
                                 ▼
                ┌─────────────────────────────────┐
                │ tools.py                        │
                │ orcho_run_start(...)            │ thin shim
                └────────────────┬────────────────┘
                                 │
                                 ▼
                ┌─────────────────────────────────┐
                │ run_control/lifecycle.py        │ implementation home
                │ start_run(...)                  │ for the run-start tool
                └────────────────┬────────────────┘
                                 │  lazy import:
                                 │  ``from orcho_mcp.supervisor
                                 │     import get_supervisor``
                                 ▼
                ┌─────────────────────────────────┐
                │ supervisor/__init__.py          │
                │ get_supervisor() → singleton    │
                └────────────────┬────────────────┘
                                 │  await sup.spawn(...)
                                 ▼
                ┌─────────────────────────────────┐
                │ supervisor/manager.py           │
                │ RunsSupervisor.spawn (mixin)    │
                └────────────────┬────────────────┘
                                 │
                                 ▼
        ┌────────────────────────┴─────────────────────────┐
        │                                                  │
        ▼                                                  ▼
┌─────────────────────────┐                  ┌─────────────────────────────┐
│ supervisor/paths.py     │                  │ sdk.build_orch_argv         │
│ resolve_project_dir(..) │                  │ build argv + --mock /       │
│ — absolute path, raises │                  │ --profile / --run-id /      │
│ PipelineSpawnError if   │                  │ --output-dir / --workspace  │
│ missing                 │                  └──────────────┬──────────────┘
└────────┬────────────────┘                                 │
         │                                                  │
         └───────────┬──────────────────────────────────────┘
                     │
                     ▼
              ┌────────────────────────────────┐
              │ subprocess.Popen(              │
              │   argv,                        │
              │   cwd=abs_project,             │
              │   start_new_session=True,      │ — own process group
              │   env=env_passthrough,         │   so ``os.killpg`` reaches
              │   stdout=runner.log fd,        │   the whole tree
              │   stderr=subprocess.STDOUT,    │ — merged transcript on disk
              │ )                              │   at <run_dir>/runner.log
              └──────────────┬─────────────────┘
                             │
                             ▼
              ┌────────────────────────────────┐
              │ allocate RunHandle             │
              │ (dataclass — pid, pgid,        │
              │ run_id, run_dir, command,      │
              │ started_at, popen)             │
              └──────────────┬─────────────────┘
                             │
                             ▼
              ┌────────────────────────────────┐
              │ supervisor/state.py            │
              │ write_state(handle) →          │
              │ <run_dir>/mcp_supervisor.json  │
              └──────────────┬─────────────────┘
                             │
                             ▼
              ┌────────────────────────────────┐
              │ sup._runs[run_id] = handle     │ in-memory registry
              │ asyncio.create_task(_reap(...)) │ background lifecycle
              └──────────────┬─────────────────┘
                             │
                             ▼
              ┌────────────────────────────────┐
              │ schemas/run_control.            │
              │ RunStartedResult               │ wire response
              └──────────────┬─────────────────┘
                             │
                             ▼
                       client/stdout
                       (pipeline subprocess continues
                        running independently)
```

Step by step:

1. **Frame arrives**, same as Trace 1.
2. **Tool shim.** The `@mcp.tool` handler in `tools.py` delegates to
   `run_control/lifecycle.py`. The delegation is one statement —
   the thin-body guard enforces this.
3. **Lazy supervisor import.** Inside the handler body,
   `from orcho_mcp.supervisor import get_supervisor` runs every
   call. The lazy import is load-bearing: tests monkeypatch
   `orcho_mcp.supervisor.get_supervisor` to inject a fake supervisor;
   a module-top import would capture the original reference before
   the patch landed.
4. **Singleton resolve.** `get_supervisor()` returns the singleton
   from `supervisor/__init__.py`. The singleton itself lives on the
   package `__init__` (not on `manager.py`) so the acceptance
   fixture's `_sup._singleton = None` reset writes to the right
   namespace.
5. **Spawn entry.** `await sup.spawn(...)` calls the thin delegation
   method on `RunsSupervisor`, which forwards to the module-level
   function `orcho_mcp.supervisor.spawn.execute(sup, ...)`. The
   operation function reads state directly off `sup._runs`,
   `sup._project_locks`, `sup._max_runs`.
6. **Project-dir resolution.** `supervisor/paths.resolve_project_dir`
   resolves the path argument to absolute form. A relative path
   becomes absolute against the server's cwd; a missing directory
   raises `PipelineSpawnError`. The absolute path is used for both
   `Popen(cwd=...)` and the `--project` argv flag, so the
   orchestrator does not re-resolve against the changed subprocess
   cwd.
7. **Run-id mint.** `RunsSupervisor.mint_run_id` returns
   `YYYYMMDD_HHMMSS_xxxxxx` (timestamp + 6 hex). Uniqueness is
   tested for 50-id collisions; format is contract-tested.
8. **Per-project lock + cap.** The spawn enters a `asyncio.Lock`
   scoped to the project directory and checks the running-run count
   against `sup._max_runs` (default 4, env-overridable via
   `ORCHO_MCP_MAX_RUNS`).
9. **Argv build.** `sdk.build_orch_argv` constructs the orchestrator
   command line — `--task`, `--project`, `--workspace`, `--run-id`,
   `--output-dir`, `--mock`, `--profile`, `--max-rounds`,
   `--output`. The SDK owns argv construction so the MCP server and
   the CLI use the same flags.
10. **Popen.** `subprocess.Popen` launches the pipeline with
    `start_new_session=True` — the child gets its own process
    group, so `os.killpg(pgid, SIGTERM)` reaches the whole tree
    during cancel.
11. **Handle allocation.** A `RunHandle` dataclass captures `pid`,
    `pgid`, `run_id`, `run_dir`, `command`, `started_at`,
    `popen`. The handle is the in-memory record of the run.
12. **State flush to disk.** `supervisor/state.write_state(handle)`
    writes `<run_dir>/mcp_supervisor.json`. This file is what
    `recover()` reads at server restart to detect orphan runs
    (running status, dead pid) and is what `status_merge` consults
    when the pipeline died before flushing meta.
13. **Registration.** `sup._runs[run_id] = handle` makes the run
    discoverable by `cancel` / `_reap` / `list_active`.
14. **Background reap.** `asyncio.create_task(sup._reap(handle))`
    schedules a lifecycle watcher in the event loop. The reap task
    awaits `popen`; when the subprocess exits, it branches on the
    return code (0 = done, 4 = `awaiting_phase_handoff`, anything
    else = failed), emits a synthetic `run.supervisor_reaped`
    event for diagnostics, updates `handle.status`, and flushes
    via `write_state`.
15. **Response.** The spawn returns a `RunStartedResult`
    (`schemas/run_control.py`) — `run_id`, `pid`, `started_at`,
    `command`, `run_dir`, `project_dir`. The pipeline subprocess
    is now running independently of the MCP server's request/
    response cycle. The client polls via `orcho_run_status` or
    holds a connection open via `orcho_run_watch`.

### Recovery: what happens on server restart

`server.main()` calls `sup.recover()` once before entering the stdio
loop. The recover routine:

1. Scans the runs directory for any `<run_dir>/mcp_supervisor.json`.
2. For each file with `status == "running"`, checks
   `os.kill(pid, 0)` — alive or dead?
3. Live pid: leave the file alone (someone else's supervisor owns it).
4. Dead pid: flip the file to `status = "orphaned"` and append a
   `run.orphaned` event for forensics.

`awaiting_phase_handoff` runs are intentionally excluded from the
dead-pid orphan path. The pipeline exits `rc=4` when a phase pauses
on a declared handoff policy — the dead pid is the expected
post-pause signature, not an orphan condition. The run waits for
`orcho_phase_handoff_decide` followed by `orcho_run_resume`.

### Progress notifications (the `progressToken` branch)

Progress notifications are emitted from `orcho_run_watch`, not from
`orcho_run_start`. The spawn call returns immediately with the
`RunStartedResult` — no notifications, no streaming. To observe a
running run, the client opens a long-poll watch:

1. Client calls `orcho_run_watch(run_id, …)` with a `progressToken`
   in the JSON-RPC request meta.
2. FastMCP injects a `Context` into the handler when the call carries
   a token. `orcho_mcp/observe/watch.py` plumbs that context through
   the watch loop.
3. On each iteration that sees the run's `next_seq` advance,
   `_maybe_report_watch_progress(ctx, snap, last_reported_seq)` calls
   `ctx.report_progress(progress=float(snap.next_seq), …)`. `next_seq`
   is the monotonic cursor; the call is the single choke-point so the
   "progress on event advance" invariant cannot drift across fast-path /
   loop / timeout return paths.
4. `Context.report_progress` is a no-op when the request carries no
   `progressToken`, so the same code path serves both modes — clients
   without a token get the final `RunWatchResult` and no
   notifications; clients with a token get an ordered notification
   stream plus the final result.

The notifications surface event-advance cursors, not full payloads —
the message field carries a bounded status / phase / kind summary.
For full events the client follows up with `orcho_run_events_tail`.

Implementation note: `src/orcho_mcp/event_tail.py` defines a
`JsonlTailer` class and a `TailedEvent` dataclass. They are not used
by any production code path today — they predate the watch-based
progress design and remain in the tree as a self-contained
building block. Treat the file as latent until a feature actually
imports it.

## Cross-cutting concerns

### Errors

Every error visible on the MCP wire is one of the typed errors in
`src/orcho_mcp/errors.py`:

- `RunNotFoundError` — `run_id` is unknown.
- `WorkspaceNotResolvedError` — `$ORCHO_WORKSPACE` /
  `$ORCHO_WORKTREE` could not be resolved.
- `PipelineSpawnError` — argv build, project resolution, or
  Popen failed.
- `InvalidPlanError` — argument out of range or shape.

The service layer maps SDK errors (`sdk.RunNotFound`,
`sdk.NoWorkspace`) to these MCP error types. FastMCP serialises
typed exceptions to JSON-RPC errors with stable codes.

### Architecture guards along each trace

The guards under `tests/unit/architecture/` enforce the invariants
the traces above rely on. A failing guard means a regression in
the architecture, not a flaky test:

| Layer | Guard |
|---|---|
| Tool handler body shape | `test_tool_body_thinness.py` — handler body is at most 6 statements, warns at 2+ |
| `tools.py` imports | `test_no_direct_run_state.py::test_tools_py_stays_wire_adapter` — no `sdk`, no `core.observability.events`, no `pipeline.plan_parser` |
| Read paths use SDK | `test_no_direct_run_state.py::test_no_raw_event_read_imports` — no `read_all` from `core.observability.events` anywhere in `src/orcho_mcp/` |
| `resources/` purity | `test_resources_boundary.py` — no SDK, no `orcho_mcp.tools`, no direct file IO |
| Supervisor package shape | `test_supervisor_boundary.py` — expected modules present, `_singleton` on the package `__init__`, soft size cap per module |
| Test layout mirrors source | `test_test_layout_contract.py` — every production sub-package has a populated unit-test home OR an explicit exemption |
| Stale paths / banned terms | `test_public_text_hygiene.py` — no retired paths, no internal process markers, no banned vocabulary |

When a guard fails, the test docstring explains the invariant. The
fix is almost always a refactor — pushing the call into the right
layer — rather than a guard-list edit. See `mcp_boundaries.md` for
the rules on relaxing a guard.

### The schema snapshot

The MCP wire shape is the union of every Pydantic model under
`schemas/`. The published contract lives at `docs/mcp_schema.json`
and is asserted by `tests/integration/protocol/test_schema_snapshot.py`.

When you change a tool's signature, return field, or schema model,
regenerate the snapshot in the same commit:

```bash
python tools/dump_mcp_schema.py
```

CI compares the live schema against the committed snapshot and
fails on drift. This is the single chokepoint that catches "wire
change shipped without doc update".

### State files

Two state files live under `<run_dir>/`:

- `meta.json` — the pipeline's contract. Owned by orcho-core,
  never written by orcho-mcp. Read for `orcho_run_status`.
- `mcp_supervisor.json` — orcho-mcp's own state. Owned exclusively
  by the supervisor; tracks pid, pgid, status, output_mode,
  halt_reason, exit_code. Read by `recover()` at restart and by
  `status_merge` for the merged view.

The two files have separate writers and never overlap. When the
pipeline crashes between writing meta and exiting, the supervisor's
state file is what makes the run discoverable as terminal.

## Reading order if you're new to the package

Follow this order on a fresh checkout. Each step builds on the
previous:

1. **This document.** Lays out the request flow.
2. **`docs/architecture/mcp_boundaries.md`.** The static shape — what
   lives where, what may import what.
3. **`tests/README.md`.** How tests mirror production layout.
4. **`src/orcho_mcp/instance.py`.** The FastMCP instance every
   adapter imports from.
5. **`src/orcho_mcp/tools.py`.** See the `@mcp.tool` decoration shape
   and a handful of one-line delegations.
6. **`src/orcho_mcp/supervisor/__init__.py`.** The package
   docstring is the supervisor's design rationale.
7. **`tests/unit/architecture/`.** The contracts that defend
   everything above. Each file's docstring states the invariant.

After step 7 you have enough context to read any source module in
the package and place it correctly.
