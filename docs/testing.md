# Testing philosophy

`orcho-mcp` exposes Orcho through the Model Context Protocol. A change can pass
ordinary unit tests and still be unusable if the MCP catalog does not register,
the stdio stream is polluted, a resource URI drifts, or a tool handler starts
owning implementation logic.

The test suite is therefore not only a coverage net. It is an executable
description of the architecture.

## Principles

1. **Tests describe contracts, not just examples.**
   A test should say what must stay true: tool handlers are adapters, resources
   do not read files directly, event cursors are monotonic, and resource URIs are
   intentional public surface.

2. **Wrong-layer code should fail before review.**
   If implementation logic appears in `tools.py`, or a domain imports
   `orcho_mcp.tools`, architecture tests fail. The reviewer should not have to
   notice those rules by eye.

3. **Prefer small deterministic fixtures.**
   Unit tests should use synthetic workspaces and run builders. A new run-state
   test should be a few readable lines, not a page of hand-written JSON.

4. **Wire surfaces are public contracts.**
   Tool schemas, resource URIs, resource templates, prompt registration, and
   stdio behavior must be tested at the protocol level when they change.

5. **Do not chase coverage for its own sake.**
   Add a test when it locks behavior, prevents architectural drift, or makes a
   future change easier to make safely. Avoid tests that only repeat an
   implementation detail.

## Test Layers

Use the smallest layer that can catch the bug, then add higher layers when the
change touches registration, transport, or subprocess lifecycle.

| Layer | Location | Use for | What it catches |
|---|---|---|---|
| L1 unit | `tests/unit/<domain>/` | Domain behavior, error mapping, Pydantic round trips, cursor semantics | Fast feedback without subprocesses |
| Architecture | `tests/unit/architecture/` | Import direction, adapter shape, catalog hygiene, test-layout contracts | Wrong-layer implementation and drift |
| L2 registration | `tests/integration/protocol/` | `mcp.list_tools()`, resources, prompts, schema generation | Missed side-effect imports and catalog drift |
| L3 stdio | `tests/integration/protocol/` | `python -m orcho_mcp` through `mcp.client.stdio` | stdout pollution, JSON-RPC framing, capability negotiation |
| L4 mock pipeline | `tests/acceptance/mock_pipeline/` | Real Orcho subprocess with `--mock` | run lifecycle, resume/cancel races, handoff flows |

The default `pytest` suite runs L1, architecture, L2, and L3. L4 is gated behind
`pytest -m mcp_integration`.

## Self-Discovery Dogfood

MCP clients should be able to drive the main Orcho lifecycle from payloads alone:
workflow recipes tell the client which calls compose a scenario,
`orcho_run_watch` exposes handoff choices with ready-to-send decision args,
and `orcho_run_status.artefacts` advertises readable resource URIs. A client
should not need to open docs or parse logs to decide what to do next.

`orcho_run_live_status` answers "where is this run right now, and what should I
do?" in a single bounded call. It returns a `RunLiveStatusCard` with one closed
`state_class` (`running_phase` / `running_subtask` / `awaiting_handoff` /
`terminal_success` / `terminal_halted` / `terminal_inconsistent`) plus the live
phase/subtask position, the last activity (truncated preview), any pending
handoff, and a terminal slice. It composes existing projections — merged status
(meta + supervisor fallback), the handoff read-model, and a narrow
`meta.phases.final_acceptance` read — and never spills full phase bodies,
critiques, or raw logs, so it is safe for high-frequency polling. A contradiction
between a terminal-success status and a rejected `final_acceptance` is surfaced
explicitly in `consistency_flags`, never hidden. The L1 contract for all six
states lives in `tests/unit/observe/test_live_status.py`.

The executable smoke for that contract is
`tests/acceptance/mock_pipeline/test_self_discovery_flow.py`. It starts a real
`python -m orcho_mcp` stdio session, reads the workflow catalogue through both
the tool and resource channels, creates a mock run that pauses on
`validate_plan`, validates the handoff `choices`, and reads every advertised
artefact URI through the MCP resource channel. The same smoke advertises client
form elicitation support and proves that a `retry_feedback` decision can collect
its missing feedback through native MCP elicitation.

When validating the same behavior in an interactive MCP client, use a clean copy
of `orcho-core/examples/golden-api` and cover this matrix:

| Case | Required calls | Contract being checked |
|---|---|---|
| Discovery | `orcho_workspace_info`, `orcho_workflows_list`, `orcho://workflows` | The client can find workspace state and the five recipe names, including `diagnose_halted_run`. |
| Clean terminal run | `orcho_run_start(mock=True)`, `orcho_run_watch(until="terminal")`, status/evidence/diff/resource reads | Terminal runs advertise artefacts, and every advertised URI resolves through MCP resources. |
| Handoff inspection | `orcho_run_start(mock=True, profile="advanced", mock_validate_plan_reject=3)`, `orcho_run_watch(until="handoff_or_terminal")` | Handoff `choices` contain only known actions and do not put feedback placeholders inside `args`. |
| Continue path | `orcho_phase_handoff_decide` using the advertised `continue` args, then the advertised followup | Non-halt choices lead to `orcho_run_resume` without reconstructing arguments from memory. |
| Retry-feedback path | `retry_feedback` choice args plus operator text in `feedback_field`, then the advertised followup | Feedback is collected explicitly and never forwarded as a placeholder string. |
| Native feedback elicitation | `retry_feedback` choice args without `feedback`, from a client that advertises form elicitation | Orcho requests the missing feedback through `elicitation/create`; clients without that capability use the retry-feedback path above. `continue_with_waiver` gates feedback the same way. |
| Continue-with-waiver path | `continue_with_waiver` choice args plus operator waiver text in `feedback`, then the advertised followup | The rejected verdict is accepted, the waiver rationale is recorded, and resume advances past the handoff without reopening the waived findings. |
| Halt path | `halt` choice args, then status inspection | Halt has no advertised followup and flips status synchronously. |
| Halt diagnosis | `diagnose_halted_run` recipe: status, `orcho_run_evidence(slice="errors")`, `orcho_run_events_summary` | A halted run can be classified without log scraping. |
| Subtask progress | `orcho_run_watch(until="subtask")` reads `summary.current_subtask`; `orcho_run_evidence(slice="receipts")` | Per-subtask state and done-criteria attestation surface for `subtask_dag` runs without parsing the event stream. |
| Live status snapshot | `orcho_run_live_status` at any point in the run | One typed `state_class` card with bounded previews classifies the run (running / awaiting handoff / terminal); a `done`-plus-rejected `final_acceptance` contradiction appears in `consistency_flags` rather than reading as a clean ship. |

Manual dogfood reports should record the `run_id`, final status, tools/resources
used, handoff choice table, artefact table, and any step where the client had to
rely on prior knowledge instead of MCP payloads.

## Inspector Smoke

MCP Inspector is the interactive protocol workbench for `orcho-mcp`. Use it as
an L5 manual smoke when a change needs visual client behavior that unit, stdio,
and mock-pipeline tests cannot show directly: native elicitation forms, resource
subscription refreshes, tool/resource catalog browsing, and client-facing payload
ergonomics.

Start it from the repo root:

```bash
make inspector
```

The target runs `scripts/mcp_inspector.sh`, which starts Inspector against the
current checkout with:

```bash
./.venv/bin/python -m orcho_mcp
```

By default the script uses the sibling `workspace-orchestrator` directory. Set
`ORCHO_WORKSPACE`, `ORCHO_WORKTREE`, or `ORCHO_MCP_PYTHON` when testing another
workspace or interpreter:

```bash
ORCHO_WORKSPACE=/path/to/workspace-orchestrator make inspector
```

Inspector is not a CI gate. It depends on `npx`, opens a browser UI, and may
need operator input. Treat it as a required manual smoke for MCP UX changes that
involve:

- native elicitation or chat fallback behavior;
- resource reads, resource subscriptions, or resource update notifications;
- tool, prompt, or resource registration UX;
- self-discovery payloads such as `handoff.choices`, `next_actions`, workflow
  recipes, and advertised artefact URIs;
- debugging a disagreement between an interactive client and the stdio tests.

For the handoff feedback flow, use Inspector to run this protocol trace:

1. Start or find a mock run paused at `awaiting_phase_handoff`.
2. Call `orcho_run_watch(until="handoff_or_terminal")` and inspect
   `handoff.choices`.
3. Call `orcho_run_status(run_id)` and confirm `next_actions` does not contain
   placeholder feedback inside callable args.
4. Call `orcho_phase_handoff_decide` with exactly the `retry_feedback` choice
   args and no `feedback`.
5. In the Inspector Elicitations tab, submit real feedback through the generated
   form.
6. Call the advertised `orcho_run_resume` followup and watch the run to a
   terminal status.

Record the Inspector smoke in the change notes when it catches behavior that
automated tests cannot display, especially whether elicitation showed a form,
was declined by the client, or fell back to chat.

## Where New Tests Go

Production code and unit tests mirror each other:

| Production surface | Unit test home |
|---|---|
| `src/orcho_mcp/services/` | `tests/unit/services/` |
| `src/orcho_mcp/observe/` | `tests/unit/observe/` |
| `src/orcho_mcp/run_control/` | `tests/unit/run_control/` |
| `src/orcho_mcp/inspection/` | `tests/unit/inspection/` |
| `src/orcho_mcp/authoring/` | `tests/unit/authoring/` |
| `src/orcho_mcp/resources/` | `tests/unit/resources/` |
| `src/orcho_mcp/supervisor/` | `tests/unit/supervisor/` |
| `src/orcho_mcp/schemas/` | covered by schema snapshot and tool/resource tests |
| `src/orcho_mcp/tools.py` | architecture tests plus per-domain tests |

When adding a new production domain, add or update `tests/unit/<domain>/` in the
same change. The layout contract is enforced by
`tests/unit/architecture/test_test_layout_contract.py`.

## Architecture Contracts

The guards under `tests/unit/architecture/` are part of the design, not lint
preferences.

| Guard | Contract |
|---|---|
| `test_tool_body_thinness.py` | Every `@mcp.tool` handler is docstring plus one `return` or `return await` |
| `test_import_graph.py` | Domains cannot import forbidden peer layers such as `orcho_mcp.tools` |
| `test_resources_boundary.py` | Resources do not import SDK, `tools.py`, or perform direct file reads |
| `test_supervisor_boundary.py` | Supervisor package shape, operation exports, signatures, and singleton location stay stable |
| `test_no_direct_run_state.py` | MCP read paths use SDK surfaces instead of private run-state parsers |
| `test_resource_catalog.py` | Static resource URIs and resource templates are explicit expected sets |

If a guard fails, start by moving code to the correct layer. Relaxing a guard is
allowed only when the architecture itself changes. That same change should
update the guard, the human-readable docs, and the tests that prove the new
shape.

## Fixture Style

Most unit tests should use the synthetic workspace helpers in
`tests/fixtures/mcp_workspace.py`:

```python
from tests.fixtures.mcp_workspace import event, meta, metrics, write_run


def test_status_for_done_run(fake_workspace):
    write_run(
        fake_workspace,
        "20260101_000001",
        meta=meta(status="done", task="ship it"),
        metrics=metrics(total_tokens=500),
        events=[event(1, "run.start"), event(2, "run.end")],
    )

    ...
```

Builder rules:

- Builders return plain dicts.
- Builders do not perform file IO.
- Builders do not hide assertions.
- `write_run` owns filesystem setup.
- Use `**extra` when a test needs a field the builder does not name yet.

If several tests start passing the same `**extra` field, promote that field to a
named builder argument.

## Event Cursor Tests

Event tools are cursor protocols. Tests should name the cursor contract they
lock:

- `next_seq` equals the last returned event when events are returned.
- Empty streams keep `next_seq == since_seq`.
- Exact fits report `eof=True`.
- Truncated windows report `eof=False`.
- `orcho_run_events_summary` computes current phase from the full stream up to
  `next_seq`, not just the returned window.
- `orcho_run_watch(..., summary=False)` still returns the reconnect cursor on
  `trigger.seq`.

Use table-driven cases when the behavior is a matrix of cursor, limit, stream,
and expected output.

## When To Add Higher-Layer Tests

Add L2/L3/L4 tests when the change touches the surface those layers own:

- Tool, resource, prompt, or schema registration: add or update L2 tests and
  `docs/mcp_schema.json` when the wire shape changes.
- stdio startup, logging, `__main__.py`, or server initialization: add L3 stdio
  coverage.
- run start/resume/cancel, handoff, progress, subprocess state, or recovery:
  add or update L4 mock-pipeline coverage.
- resource URI changes: update `test_resource_catalog.py`.
- event cursor behavior: update unit cursor tests before relying on L4.

Do not use L4 as the first place to discover a simple unit bug. L4 should prove
that the layers work together after lower layers have pinned the local behavior.

## Required Checks

Before calling a change ready:

```bash
pytest -q tests/unit/architecture
pytest -q tests/integration/protocol
pytest -q --import-mode=importlib
pytest -q
ruff check .
python tools/dump_mcp_schema.py --check
git diff --check
```

When subprocess lifecycle or run-control behavior changes, also run:

```bash
pytest -q -m mcp_integration
```

## How To Read Failures

- **Architecture failure:** the code is probably in the wrong layer. Refactor
  before editing the guard.
- **Schema snapshot failure:** the MCP wire contract changed. If intentional,
  regenerate the snapshot and explain the contract change. If not, restore the
  previous handler signature or schema model.
- **Resource catalog failure:** a URI was added, removed, or renamed. Update the
  expected set only when the public catalog change is deliberate.
- **L3 stdio failure:** suspect stdout writes, startup imports, missing
  registration, or `python -m orcho_mcp` behavior.
- **L4 failure:** treat it as lifecycle evidence, not weather. Check
  supervisor state, `meta.json`, events, and the run transcript.

The goal is not to make tests green by any means. The goal is to keep the code
shape and the public MCP behavior understandable enough that the next change is
safe to make.
