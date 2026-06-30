# Typed silent boundary pilot

> Two tools drive a single-project pipeline as an in-process library
> call through orcho-core's typed silent boundary — a blocking
> reference variant and a non-blocking async sibling. The rest of
> the run-control surface continues to spawn subprocesses.

## Scope

| Surface | Shape | When to use |
|---|---|---|
| `orcho_run_start` + `orcho_run_watch` / `orcho_run_status` | Background subprocess; supervisor lifecycle; progress notifications | Long-running real-provider runs; cancellable; interactive watch |
| `orcho_run_project_typed` | Foreground blocking library call; mock provider; structured result in one round-trip | Short mock smokes, fixture generation, integration scaffolding |
| `orcho_run_project_typed_async` | Non-blocking library call; mock provider; returns `run_id` immediately; status polled via existing read tools | Same use cases as the blocking variant when the caller can't hold the MCP request open for the duration |

Pilot constraints:

* **Mock provider only** — `mock=True` is required. Real-provider
  runs continue through `orcho_run_start` so the MCP client can
  stream progress and cancel via signal.
* **Single project** — cross-project is not in scope for the pilot.
* **Blocking** — the handler returns only after the pipeline
  completes; the MCP server's event loop is held for the duration.
  Mock runs are sub-second; that constraint is acceptable for the
  pilot scope.

## Contracts

### Blocking variant — `orcho_run_project_typed`

```text
orcho_run_project_typed(
    task: str,
    project_dir: str,
    output_dir: str,
    profile: str = "task",
    mock: bool = True,
    max_rounds: int = 1,
) -> TypedRunResult
```

Response shape (`TypedRunResult` — see
`src/orcho_mcp/schemas/run_control.py`):

```json
{
  "run_id": "20260101_123456",
  "output_dir": "/abs/path/to/run_dir",
  "status": "done",
  "halt_reason": null,
  "event_kinds": ["run.start", "phase.start", "phase.end", "run.end"]
}
```

* `status` is read directly from the persisted session, never from
  any transcript.
* `halt_reason` is `None` on the done path; populated on every
  non-`done` terminal status.
* `event_kinds` is the ordered list of `kind` values from
  `events.jsonl`. Presence of the canonical
  `run.start` → `phase.start` → `phase.end` → `run.end` spine
  confirms the file + event sinks stayed wired under SILENT.

For the persisted surface (status summary / metrics / event payloads)
the caller follows up with the existing read tools using the returned
`run_id`:

* `orcho_run_status(run_id)` — summary meta + metrics snapshot; pass `include=["all"]` for full persisted meta
* `orcho_run_metrics(run_id)` — phase / cost breakdown
* `orcho_run_events_tail(run_id)` — full event stream

### Non-blocking variant — `orcho_run_project_typed_async`

```text
orcho_run_project_typed_async(
    task: str,
    project_dir: str,
    profile: str = "task",
    mock: bool = True,
    max_rounds: int = 1,
) -> TypedRunStartedResult
```

```json
{
  "run_id": "20260527_141500",
  "output_dir": "<workspace>/runspace/runs/20260527_141500",
  "status": "running",
  "started_at": "2026-05-27T14:15:00.123456+00:00"
}
```

Differences vs the blocking variant:

* **Returns immediately.** The pipeline runs in
  `asyncio.to_thread` so the MCP event loop stays responsive.
* **Workspace-aware.** The tool derives `output_dir` from
  `$ORCHO_WORKSPACE` (or walk-up resolution) and places the run at
  `<workspace>/runspace/runs/<run_id>/`. The directory name **is**
  the `run_id`, so the standard read tools resolve the run by id:
  ```text
  orcho_run_project_typed_async(...)  →  run_id="20260527_141500"
  orcho_run_status("20260527_141500")  →  status="running" → "done"
  orcho_run_events_tail("20260527_141500")  →  run.start → ... → run.end
  ```
* **No new polling endpoint.** The async tool integrates with the
  existing observation surface; `run_id` is a first-class identifier
  consumed by every read tool that takes one.

Pilot constraint: one async run in flight per MCP server process.
The `ORCHO_RUN_ID` env var is serialized at start so concurrent
async starts can't race on the env-thread, but the env var itself
is a process-wide singleton. Real concurrency widens the seam from
env to per-invocation context.

## Anti-patterns the pilot avoids

* **No stdout parsing.** The pilot's adapter consults
  `ProjectRunResult.session` and `events.jsonl` directly. There is
  no `subprocess.Popen`, no `capture_output=True`, no string match
  against transcript markers like `DONE` / `Session:` / `Run dir:` /
  `[PLAN]`. This is enforced by a source-level grep guard in
  `tests/unit/run_control/test_typed_pilot.py`.
* **No CLI dependency.** The pilot does not require an installed
  `orcho-run` console script; orcho-core is imported as a library.
* **No async polling for short runs.** Callers that want a one-shot
  structured result get it in one round-trip — no `run_id` →
  `status` → `events_tail` dance.

## Architecture fit

The pilot follows the standard MCP boundary contract:

* The `@mcp.tool` handler in `tools.py` is a single-line
  delegation, enforced by `test_tool_body_thinness.py`.
* The adapter lives in
  `src/orcho_mcp/run_control/typed_pilot.py` and owns request
  construction, `MockAgentProvider` wiring, post-run reads, and
  response packing.
* The wire model lives in `src/orcho_mcp/schemas/run_control.py`
  and is captured in the committed `docs/mcp_schema.json` snapshot.

## Cross-reference

The orcho-core consumer reference walks the same pattern from the
library-caller side:

> `orcho-core/docs/examples/typed_boundary_consumer.md`

and the executable companion smokes (which double as the regression
net for the boundary itself):

> `orcho-core/tests/integration/project/test_typed_boundary_consumer.py`
> `orcho-core/tests/integration/cross/test_typed_boundary_consumer.py`

## Migration path

This is the smallest viable in-process consumer of the typed silent
boundary. Future widening (longer-running runs, real providers,
cross-project) is gated on:

1. An offload story for blocking pipeline work that does not stall
   the MCP server's event loop. Likely `asyncio.to_thread` or a
   worker pool, but a real benchmark drives the choice.
2. A cancellation path for in-process runs. Today's
   `orcho_run_cancel` works on subprocess signal delivery, which
   doesn't apply to in-process work.
3. A progress-notification path for in-process runs. Today's
   `progressToken` flow rides on the supervisor's reap loop.

Until those land, the pilot stays a focused two-tool slice
(blocking + non-blocking, both mock-only, both single-project)
and `orcho_run_start` remains the long-form interface for
real-provider runs.

## SILENT migration status for the MCP read surface

The MCP read tools (`orcho_run_status`, `orcho_run_metrics`,
`orcho_run_events_tail`, `orcho_run_events_summary`,
`orcho_run_watch`, `orcho_run_history`, `orcho_run_evidence`,
`orcho_run_diff`) are already structurally `SILENT`-ready: zero
stdout parsing in source. Event-stream reads go through
`sdk.list_events`; status, metrics, evidence, and diff reads go
through the SDK and persisted artifacts (`meta.json`, `metrics.json`,
`evidence.json`, `diff.patch`). The only `kind ==` switch in MCP
code is on `phase.start` / `phase.end` for phase tracking; every
other event kind flows as opaque bucketed records, so the orcho-core
[event registry](../../orcho-core/docs/reference/event_registry.md)
additions (`agent.contract_ready`, `agent.tool_use`,
`agent.mcp_tool_call`, verdict events) already surface to
downstream MCP clients via the existing summary / tail APIs.

The
[stdout-to-event gap register](../../orcho-core/docs/plans/2026-05-27-stdout-event-gap-register.md)
proposes follow-up events (`agent.notice`, `run.notice`,
`phase.notice`, `phase.parse_failed`, `phase.output_ready`,
`usage.snapshot`). None of them are currently required by an MCP
consumer use case — the existing tools complete the consumer-
visible state contract without them. The witness test at
`tests/unit/observe/test_silent_terminal_parity.py` pins this:
if a future orcho-core change starts emitting a candidate, the
witness trips and the matching gap-register row should be flipped
from "proposed" to "shipped" in the same diff.

`tests/unit/observe/test_silent_terminal_parity.py` is the
load-bearing migration smoke. It drives the same mock task under
`PresentationPolicy.TERMINAL` and `PresentationPolicy.SILENT`,
then asserts that the MCP read tools return equivalent consumer-
visible state across modes:

* `orcho_run_status` — final status, halt reason, failure block,
  consumer-key surface.
* `orcho_run_metrics` — metrics.json key set.
* `orcho_run_events_tail` — full event-kind set + within-25% event
  count parity.
* `orcho_run_events_summary` — per-phase coverage + per-phase kind
  set.

If a future change leaks an event or artifact only under one
policy, the parity test catches it before MCP consumers see drift.

## Adjacent context — test floor

The pilot rides on a stable test floor. `tests/fixtures/mcp_workspace.py`
builds workspaces at `<tmp>/ws/runspace/runs/`, matching how the
orcho-core SDK resolves `runspace/runs/` via the workspace
walk-up. Project-level skills live under `.agents/skills/`.
Project-level prompt overrides live under `.orcho/multiagent/prompts/`. Production
docstrings in `src/orcho_mcp/supervisor/paths.py` document the
same layout; the resolution code itself is layout-agnostic
(`runs_dir.parent.parent`).

If a future migrator widens the pilot beyond `mock=True` /
single-project / blocking, the same workspace layout applies to
the new surface — there is no separate test-fixture path.
