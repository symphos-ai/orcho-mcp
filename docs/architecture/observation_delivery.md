# MCP Observation Delivery

This document defines how MCP clients observe running Orcho jobs without
scraping terminal output.

The contract is polling first. Notifications are optional delivery
accelerators.

## Source of truth

| Surface | Role |
|---|---|
| `events.jsonl` | Append-only event timeline with monotonic `seq`. |
| `meta.json` | Run status and run-level metadata. |
| `metrics.json` | Usage and duration data when written. |
| Evidence and phase artifacts | Parsed plans, findings, diffs, and detailed run evidence. |

Terminal stdout is not part of the MCP observation contract. `SILENT` execution
may suppress stdout while preserving events and artifacts.

## Reliable MCP path

MCP clients should treat `seq` as the reconnect cursor:

```text
last_seq = 0
call orcho_run_events_summary(run_id, since_seq=last_seq)
update last_seq from result.next_seq
repeat, or call orcho_run_watch(run_id, since_seq=last_seq)
```

For exact replay, use `orcho_run_events_tail`. For bounded progress UI, use
`orcho_run_events_summary`. For waiting until a meaningful condition, use
`orcho_run_watch`.

| Tool/resource | Delivery style | Contract |
|---|---|---|
| `orcho_run_events_tail` | Snapshot pull | Returns raw events with `seq > since_seq`. |
| `orcho_run_events_summary` | Snapshot pull | Returns bounded counts, compact tail, current phase, and `next_seq`. |
| `orcho_run_watch` | Long-poll | Holds a request until `until` triggers or timeout, then returns a summary and reconnect cursor. |
| `orcho://runs/{run_id}/summary` | Resource pull | Returns the latest bounded summary. |
| `resources/subscribe` on summary resources | Advisory invalidation | Sends `notifications/resources/updated`; client rereads the resource. |
| `progressToken` with `orcho_run_watch` | Advisory progress | Sends `notifications/progress` on observed seq advance during the active watch call. |

### `orcho_run_watch` triggers (`until`)

| `until` | Fires when |
|---|---|
| `"next_event"` | any event with `seq > since_seq` |
| `"phase_change"` | a phase transition (including end-to-`None`), or handoff/terminal |
| `"subtask"` | each `subtask_dag` boundary (a subtask starts or ends) during a long implement phase; handoff/terminal still override. `WatchTrigger.kind` is `"subtask"`, and `summary.current_subtask` carries the live index/total/goal/state. |
| `"handoff_or_terminal"` | the run pauses (`awaiting_phase_handoff` / `awaiting_gate_decision`) or reaches a terminal status |
| `"terminal"` | the run reaches a terminal status |

With a `progressToken`, an in-flight subtask makes the progress notification
read `implement: subtask 3/12 done (<goal>)` instead of `running: implement at
seq N`.

## Resilient observation loop

`orcho_run_watch` can hold a request open for up to 2h, but a client whose
transport caps tool-call duration should prefer a short bounded watch and
reconnect, rather than one long-lived call. The loop is
watch → `orcho_run_events_summary` → watch:

```text
last_seq = 0
loop:
  r = orcho_run_watch(run_id, since_seq=last_seq, timeout_s=120..240)
  if r.triggered and r.trigger.kind in {handoff, terminal}: act, stop
  # timeout OR the transport dropped the long-poll:
  s = orcho_run_events_summary(run_id, since_seq=last_seq)  # catch up
  last_seq = s.next_seq        # reconnect cursor
  # (without summary, reconnect from r.trigger.seq instead)
```

A client-side disconnect of a watch — timeout, dropped long-poll, or client
restart — is observation transport loss (observer loss), **not** a run
failure. The run keeps executing in its worktree. Lifecycle decisions
(continue / retry / halt) are read only from the typed `status`,
`pending_handoff`, terminal status, and evidence signals — never from the fact
that a watch call ended. `RunEventsSummary.next_seq` and `WatchTrigger.seq`
both carry the reconnect cursor; `orcho_run_events_summary` is the fallback
half that lets a dropped watcher catch up before resuming.

## Notification rule

Notifications do not replace replay.

When a client receives a progress notification or a resource-updated
notification, it should refresh through the same cursor/resource path:

```text
notification received
  -> call orcho_run_events_summary(..., since_seq=last_seq)
  -> or read orcho://runs/{run_id}/summary
  -> update last_seq
```

If a client misses notifications, reconnects, or restarts, it recovers by
calling `orcho_run_events_tail` or `orcho_run_events_summary` with the last
persisted `seq`.

## Boundary note

Browser-facing live transports are outside the MCP contract. If another client
layer adds a live event bridge, it should still use `events.jsonl` as the
recovery path. MCP itself stays cursor-based.

## Adding new event kinds

New Orcho core event kinds become visible to MCP raw tail readers as soon as
they are written to `events.jsonl`.

Summary readers see them in:

- `RunEventsSummary.by_kind`
- `RunEventsSummary.by_phase[].kinds`
- `RunEventsSummary.last_n[].kind`

`RunEventsSummary` also carries `current_subtask` — a live progress coordinate
(`subtask_id`, `index`, `total`, `goal`, `state`, `seq`) computed from the
latest `subtask.start` / `subtask.end` up to the summary horizon, and cleared
when the implement phase ends. It is `None` when no subtask is in flight.

If a new event requires a special compact field (like `current_subtask` for
`subtask_dag` progress), update `orcho_mcp.observe.summary` and the matching
summary tests.
Otherwise no MCP wire change is needed.

## Contributor checklist

- Do not parse terminal stdout for MCP progress.
- Preserve `since_seq` cursor semantics on every observe path.
- Treat notifications as wakeups, not data authority.
- Treat a dropped or timed-out `orcho_run_watch` as observer loss, not run
  failure; reconnect via `next_seq` / `trigger.seq` instead of re-deciding.
- Keep raw payload access on `orcho_run_events_tail`; keep summaries bounded.
- When changing tool/resource schemas, regenerate `docs/mcp_schema.json`.

## Implementation path for delivery accelerators

Delivery accelerators are allowed when the replay contract stays intact:

1. Keep `events.jsonl` and run artifacts as the source of truth.
2. Keep `orcho_run_events_tail`, `orcho_run_events_summary`, and
   `orcho_run_watch` cursor-based.
3. Improve `orcho_run_watch` internals only when needed. A file watcher may
   wake the loop instead of fixed-interval polling, but the wire contract stays
   `since_seq` based.
4. Do not add a non-replayable push-only event path for MCP.

## Validation

Observation changes should update and run the checks that cover their layer:

| Check | Covers |
|---|---|
| `tests/unit/services/test_run_reads.py` | Event-tail cursor behavior. |
| `tests/unit/observe/test_summary.py` | Bounded summary behavior. |
| `tests/integration/protocol/test_stdio_read_tools.py` | Stdio `orcho_run_events_summary` and `orcho_run_watch`. |
| `tests/integration/protocol/test_resource_subscriptions.py` | Resource-updated notification behavior. |
| `tests/unit/observe/test_silent_terminal_parity.py` | `SILENT` event/artifact parity when the test is present. |
