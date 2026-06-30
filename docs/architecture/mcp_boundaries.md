# orcho-mcp architecture boundaries

This document describes the current architecture of `orcho-mcp` as an
executable contract. It is the human-readable counterpart to the
guards under `tests/unit/architecture/`.

The point is to keep the package shaped so the MCP wire surface stays
thin, the implementation domains stay focused, and the test layout
mirrors production. When a change crosses a boundary, the matching
guard test fails — read the failure together with this document.

## Adapter layers

These modules are MCP-facing adapters. They register tools / resources
/ prompts with FastMCP and delegate to implementation domains. They do
not contain business logic.

| Surface | Role |
|---|---|
| `src/orcho_mcp/tools.py` | `@mcp.tool` registration and one-line delegation to domain modules |
| `src/orcho_mcp/resources/` | MCP resource URI handlers; one-line delegation, no SDK or file IO |
| `src/orcho_mcp/prompts.py` | MCP prompt registration |
| `src/orcho_mcp/onboarding.py` | Onboarding prompt registration |
| `src/orcho_mcp/workflows.py` | Workflow prompt registration |
| `src/orcho_mcp/schemas/` | Pydantic models that define the MCP wire shape (sub-package: `shared`, `workspace`, `read`, `observe`, `authoring`, `run_control`, `inspection`) |
| `src/orcho_mcp/instance.py` | Shared `FastMCP` instance — imported by all other adapters |

## Implementation domains

Each domain owns a coherent slice of MCP behavior. Cross-domain
imports are allowed only when the importer is using the other
domain's public surface — not reaching into its internals.

| Domain | Owns |
|---|---|
| `src/orcho_mcp/services/` | SDK-backed reads, raw run artifact reads, the read-model projection owner (`run_projection.py`), and the SDK→MCP / command error-mapping owner (`errors.py`) |
| `src/orcho_mcp/observe/` | Event summaries, watch, handoff hints, advisory observation hints |
| `src/orcho_mcp/run_control/` | Implementation for run start / resume / cancel / handoff-decision tools |
| `src/orcho_mcp/inspection/` | Evidence and diff reads |
| `src/orcho_mcp/authoring/` | Plan validation and prompt resolution |
| `src/orcho_mcp/supervisor/` | Subprocess lifecycle: spawn, state IO, recovery, cancel, resume, reap |

## Import rules

- `tools.py` must not import `sdk`, `core.observability.events`, or
  `pipeline.plan_parser` (or any of their submodules). Tool handlers
  delegate to the matching domain module.
- `resources/` must not import `sdk`, `orcho_mcp.tools`, or perform
  direct file IO (`open`, `path.read_text`, `path.glob`, etc.). Reads
  go through `services/`.
- `services/` is the canonical home for SDK read access in MCP.
- The SDK exception types (`RunNotFound`, `NoWorkspace`,
  `InvalidPhaseHandoffState`) are imported — and therefore caught — only
  in `services/errors.py` plus the direct SDK-call sources
  `services/run_lookup.py`, `services/run_events.py`, and
  `services/read_queries.py`. Every other domain wraps its SDK call in
  `services.errors.map_sdk_errors(...)`.
- The `meta.phase_handoff` read-model (available actions / id / phase /
  trigger / artifacts / findings) is parsed only in
  `services/run_projection.py`. Other modules consume the returned
  `HandoffReadModel`.
- `supervisor/` owns subprocess lifecycle and its own state file
  (`mcp_supervisor.json`); `meta.json` stays the pipeline's contract.
  It does not import `observe/`, the projection (`run_projection`), or
  `schemas/` — read-model and wire shaping happen above it.
- Domain modules do not import from peer adapter layers (`tools.py`,
  `resources/`).

## Error-mapping owner

`services/errors.py` is the single place that converts exceptions into
the typed `orcho_mcp.errors` hierarchy the dispatch layer maps to
JSON-RPC. It exposes two context managers:

- `map_sdk_errors(run_id)` — wraps an SDK read/command call:
  `RunNotFound → RunNotFoundError`, `NoWorkspace →
  WorkspaceNotResolvedError`, `InvalidPhaseHandoffState → InvalidPlanError`,
  `ValueError → InvalidPlanError`.
- `map_command_errors()` — wraps a run-control delegation into the
  supervisor: already-typed `OrchoMCPError` pass through unchanged, a
  bare `ValueError` becomes `InvalidPlanError`, any other
  non-`OrchoMCPError` leak becomes `PipelineSpawnError`.

`run_control/lifecycle.py` (`start_run` / `resume_run` / `cancel_run`)
and `run_control/handoff.py` (`decide_phase_handoff`) are the consumers:
they own the command, the owner owns the translation. The dispatch layer
owns the final `OrchoMCPError → JSON-RPC` mapping.

Run-control command results follow one taxonomy:

| State | Wire result |
|---|---|
| Successful command | `RunStartedResult` / `CancelResult` (e.g. `signal_sent(graceful)`) |
| Pending operator action | `CancelResult` status (`already_dead` / `already_done`) or an `awaiting_*` run status |
| Validation error | `InvalidPlanError` (incl. an invalid cancel `mode`) |
| Run not found | `RunNotFoundError` |
| Environment / import issue | `WorkspaceNotResolvedError` |
| Supervisor / subprocess issue | `PipelineSpawnError` |

Enforced by `tests/unit/architecture/test_no_direct_run_state.py`
(`test_sdk_error_types_caught_only_in_owner`,
`test_run_control_wraps_supervisor_delegations`) and the behavioural
taxonomy cases in `tests/unit/run_control/test_lifecycle_tools.py`.

## Read-model projection owner

`services/run_projection.py` is the single projection surface. It turns
on-disk run state into normalised read-models that presentation layers
render without re-parsing:

- `project_handoff_read_model(run_id, *, current_phase=None)` parses
  `meta.phase_handoff` into a `HandoffReadModel` (actions, handoff id,
  phase, trigger, incomplete-subtask count, and the resolved findings
  source incl. the SDK `list_findings` fallback). `observe/handoff_hints.py`
  renders that read-model (prompt / choices / client-hints / bounded
  findings compaction) and never reads the `phase_handoff` key itself.
- `merged_status_from_meta` / `merged_halt_reason_from_meta` reconcile
  `meta` status/halt-reason with `mcp_supervisor.json`; the
  implementation stays in the pure `services/status_merge.py` and is
  re-exported here so the projection surface has one import home.

`project_run_diagnosis` (`services/run_projection.py`) and
`project_recovery_lineage` (`services/run_lineage.py`) delegate the
`condition` / continuation-subject / recovery-lineage classification to
the core read-model (`sdk.run_control.run_diagnosis` / `recovery_lineage`);
the MCP side stays a thin projection onto the `RunDiagnosis` /
`RecoveryLineage` wire models. Supervisor-merged `meta` / `status` is fed
into core so a stale on-disk status cannot drive an incorrect recovery
recommendation.

Enforced by `tests/unit/architecture/test_no_direct_run_state.py`
(`test_phase_handoff_parsed_only_in_projection_owner`) and the
`supervisor` forbidden edges in
`tests/unit/architecture/test_import_graph.py`.

## Provider-pressure projection (single source, four surfaces)

A run is under *provider pressure* when core types its failure as a
provider runtime/access fault — a rate-limit, a transient provider
runtime fault, or a loss of provider access — rather than a generic
code/test/review failure. MCP surfaces this as a distinct typed
condition with conservative resume-later / inspect guidance, never as a
rejected review, a failed final acceptance, or an operator halt.

**Single source of truth.** `services/run_projection.py`
`project_provider_pressure(run_id)` reads ONLY the core-typed errors/halt
slice — `sdk.get_errors_halt(run_id)` → `ErrorsAndHalt.provider_runtime`
(`ProviderRuntimeFailure`) and `ErrorsAndHalt.recovery`
(`ProviderAccessRecovery`), both keyed off `meta.failure.failure_kind`.
The two are mutually exclusive; `provider_runtime` takes priority. MCP
never derives the condition by parsing raw provider output, event logs,
or stdout. `project_provider_pressure_from_errors_halt(run_id, eh)` is
the pure mapping half, so a caller that already holds the slice (the
evidence `errors` path) reuses the same mapping without a second SDK
read.

**Single next_actions helper.** `build_provider_pressure_next_actions`
is the ONE place provider-pressure follow-ups are built; the
`build_provider_pressure(projection)` factory assembles the
`ProviderPressure` wire model (`schemas/shared.py`) from the projection +
that helper. The actions are conservative and feedback-free: they inspect
the errors slice and resume/retry the interrupted phase (or wait for a
reset window) — they never emit `retry_feedback` / operator-feedback and
never imply a passed review/delivery.

**Four surfaces, one condition.** Every surface fills its typed
`provider_pressure` field from that single source + helper, so they
cannot drift:

| Surface | Builder | Field |
|---|---|---|
| status | `services/run_reads.py` `get_run_status` (the real `orcho_run_status` builder) | `RunStatus.provider_pressure` |
| evidence | `inspection/evidence.py` `errors` slice | `ErrorsHaltSliceRecord.provider_pressure` |
| diagnose | `inspection/diagnosis.py` `inspect_run_diagnosis` | `RunDiagnosis.provider_pressure` |
| summary | `observe/summary.py` `build_run_events_summary` (terminal status) | `RunEventsSummary.provider_pressure` |

The live-status card (`observe/live_status.py`) additively carries it too
for terminal cards. The legacy pass-throughs are untouched:
`RunStatus.next_actions` (SDK-derived) and `RunEventsSummary.next_actions`
(`list[str]`) keep their old shape — the typed provider-pressure actions
live only in `provider_pressure.next_actions`.

Cross-surface equality (condition / failure_kind / phase /
`provider_pressure.next_actions`) is enforced by
`tests/unit/observe/test_provider_pressure_status.py`; the evidence-slice
shape by `tests/unit/inspection/test_evidence_provider_pressure.py`; the
projection mapping + helper by
`tests/unit/services/test_provider_pressure_projection.py`; and the
diagnose residual-reconciliation by
`tests/unit/run_control/test_diagnose_provider_pressure.py`.

**Diagnose entrypoint.** The public `orcho_run_diagnose` tool delegates
to `inspect_run_diagnosis` in `inspection/diagnosis.py`
(`project_run_diagnosis` is the projection). There is no
`run_control/diagnose.py` — diagnose lives in `inspection/`. The
provider-pressure upgrade is a *post-core reconciliation*: only a
residual resumable condition (`halted` / `failed` / `interrupted`) with a
present provider-pressure source is upgraded to
`condition='provider_pressure'`; `needs_decision` /
`needs_delivery_decision` / `superseded_by_child` / `closed_by_followup` /
`blocked_worktree` / `resume_inert_terminal` / `active` are never
overridden.

### Core blocker (Stage-0 T2) and additive-passthrough

Today's shipped core source carries exactly the
`ProviderRuntimeFailure` / `ProviderAccessRecovery` fields
(`failure_kind`, `recoverable`, `recommended_action`, `failed_phase`,
`runtime`, `model`, `provider_message`). It does NOT yet carry a durable
*parked* status or the finer `pressure_kind` / `retry_state` / `reset_at`
/ `wait_hint` fields. Surfacing those as durable, captain-actionable
state is a **core blocker (Stage-0 T2)**: core must first persist a
parked provider source (`pressure_kind` / `retry_state` / `reset_at` /
`wait_hint` on `meta.failure` and the SDK slice).

Until then MCP does **additive-passthrough only**: the projection reads
those future fields defensively via `getattr` (they are `None` on today's
slice) and passes them through unchanged when present. MCP never
fabricates a `reset_at` / wait time, never invents `retry_state`, and
never parses logs to synthesise them. `build_provider_pressure_next_actions`
already branches on them — a `retry_state='parked_until_reset'` /
`reset_at` projection yields `wait_until_reset` → `resume_after_reset` →
`inspect` actions, an exhausted projection yields conservative
`inspect` + `resume` with no reset time — so the future shape is exercised
by fixture tests (stand-in objects carrying the future attributes) with
**no change to the installed core**.

**Stop condition.** The full parked-captain-state — `wait_until_reset` /
`resume_after_reset` presented as a *normal* lifecycle state rather than a
terminal failure — is enabled ONLY once core ships the parked source. The
fixture tests document the target shape and stand as the executable
stop-condition marker; do not promote parked to a first-class lifecycle
state on the MCP side while the source is absent.

## Run-control adapter surface matrix (Stage 5)

orcho-core exposes a headless run-control slice (`sdk.run_control`:
`RunService`, `read_run_events` / `tail_run_events`, `load_run_snapshot`,
`PhaseHandoffDecisionCommand`). This matrix records, per MCP surface,
whether that slice is **adopted** (MCP routes through it), **kept** (MCP
keeps its own path because the SDK contract diverges), or is **no-touch**
(out of the slice entirely). Every decision is verified against the
actual code on both sides; the file/line evidence is in the *Reason*
column. The executable guards for the adopted/keep boundaries live under
`tests/unit/run_control/` and `tests/unit/services/`.

| # | Surface | Current implementation | `sdk.run_control` coverage | Decision |
|---|---|---|---|---|
| a | Event-stream reads (`services/run_events.read_run_events`) | `sdk.run_control.read_run_events(run_id, cwd=None)` | Full — same reader | **ADOPTED** (done) |
| b | Typed pilot start (`run_control/typed_pilot.run_project_typed_silent`) | `RunService().start(request)` | `RunService.start` dispatches `ProjectRunRequest` → same `run_project_pipeline` | **ADOPTED** (done) |
| c | Phase-handoff decide (`run_control/handoff.decide_phase_handoff`) | `sdk.phase_handoff_decide(..., cwd=None)` | `RunService.decide_handoff` → `phase_handoff_decide` (no `cwd`) | **KEEP** |
| d | Status / halt-reason merge (`services/status_merge`) | reads `mcp_supervisor.json` | none | **KEEP** |
| e | Handoff read-model (`services/run_projection.project_handoff_read_model` / `project_pending_handoff`) | payload-first parse of `meta.phase_handoff` | `load_run_snapshot` → `PendingOperatorAction` | **KEEP** |
| f | `get_run_status` (`services/run_reads.get_run_status`) | `sdk.load_status(run_id, cwd=None)` | `RunService.snapshot` → `RunSnapshot` | **KEEP** |
| g | `event_tail.JsonlTailer` | `run_dir`-based JSONL tail thread | `tail_run_events` is `run_id`/workspace-based | **KEEP** |
| h | Supervisor spawn / resume / cancel (`run_control/lifecycle`) | MCP `supervisor` subprocess | `RunService.resume`/`cancel` return typed unsupported | **NO-TOUCH** |
| i | Delivery decisions (`run_control/delivery.decide_delivery`) | `sdk.decide_delivery(..., cwd=None)` | Full for post-release delivery / correction gates | **ADOPTED** (done) |
| j | `resources/` | thin URI adapters, delegate to `services/` | n/a | **NO CHANGE** |
| k | Generic gate decisions | no generic MCP gate-decide tool | none (generic gate ≠ phase-handoff or delivery) | **NO-TOUCH** |

### Why each decision (code-verified)

- **(a) Event reads — ADOPTED.** `services/run_events.py:24` calls
  `sdk.run_control.read_run_events(run_id, cwd=None)`; `cwd=None` pins
  resolution to the ambient workspace / runs_dir with no walk-up, and the
  `find_run` error sources are identical to the prior direct
  `sdk.list_events` path. Consumers already ride this single reader:
  `observe/summary.py:278,401`, `observe/watch.py:70,262`, and
  `services/run_reads.py:162` (events tail). No change owed here; listed so
  the matrix is complete.
- **(b) Typed pilot start — ADOPTED.** `run_control/typed_pilot.py:156`
  calls `RunService().start(request)` (via a lazy
  `from sdk.run_control import RunService`). `RunService.start`
  (`sdk/run_control/service.py:161-180`) is a pure `isinstance` dispatcher
  that routes a `ProjectRunRequest` to the *same* `run_project_pipeline`
  (`service.py:55-58`) and returns the same `ProjectRunResult`, so the run
  is field-for-field wire-neutral while the `PresentationPolicy.SILENT` +
  `no_interactive=True` + `MockAgentProvider` request shape
  (`typed_pilot.py:135-144`) is preserved verbatim. This is the one surface
  where the SDK gives full parity, so it is the single adopted start path.
  The routing is locked by `test_pilot_routes_run_through_run_service_start`
  (`tests/unit/run_control/test_typed_pilot.py`), which patches
  `sdk.run_control.RunService` and fails if the adapter ever reverts to a
  direct `run_project_pipeline` call; the wire fields stay pinned by
  `test_typed_run_result_wire_fields_are_pinned`.
- **(c) Phase-handoff decide — KEEP.** MCP calls
  `sdk.phase_handoff_decide(..., cwd=None)` at `run_control/handoff.py:118-125`.
  `RunService.decide_handoff` (`service.py:267-271`) invokes
  `phase_handoff_decide(**command.to_decide_kwargs())`, and
  `PhaseHandoffDecisionCommand.to_decide_kwargs()`
  (`sdk/run_control/types.py:121-129`) emits **only**
  `run_id`/`handoff_id`/`action`/`feedback`/`note` — it carries no `cwd` or
  `workspace`. Because neither the command nor `decide_handoff` passes a
  `cwd`, `phase_handoff_decide` would fall back to its `_CWD_DEFAULT`
  walk-up sentinel, whereas the MCP path pins `cwd=None` (no ambient
  walk-up). Extending the command with `cwd` is rejected on the core side
  (`cwd` is a resolution input, not a durable command field). So MCP keeps
  the direct `cwd=None` call. T4 adds the executable guard asserting
  `to_decide_kwargs()` contains no `cwd`/`workspace` key.
- **(d) Status / halt-reason merge — KEEP.** `services/status_merge.py:16-17,79-82`
  reads `mcp_supervisor.json`, an MCP-private contract the SDK
  intentionally does not surface. `RunSnapshot.status` comes straight from
  `meta.json` (`sdk/run_control/snapshots.py:74`) with no supervisor
  reconciliation, so adopting the snapshot would drop the SIGKILL / orphan /
  SIGTERM-before-atexit terminal-status fill-in that the merge provides.
- **(e) Handoff read-model — KEEP.** `project_handoff_read_model`
  (`services/run_projection.py:240-280`) is payload-first: it parses
  `meta.get("phase_handoff")` directly (`run_projection.py:243`) and is **not**
  gated by `meta.status`. `load_run_snapshot` instead gates on
  `meta_status == "awaiting_phase_handoff"` (`snapshots.py:147`) and resolves
  the cross checkpoint first (`snapshots.py:123`), so a direct swap changes
  the edge cases. The MCP read-model also carries richer fields —
  `trigger`, `incomplete_count`, `raw_findings` (with the SDK `list_findings`
  fallback), `verdict`, `round_n`/`loop_max_rounds`,
  `decision_artifact_exists` (`run_projection.py:256-266`) — none of which
  exist on `PendingOperatorAction` (`sdk/run_control/types.py:68-75`).
- **(f) `get_run_status` — KEEP.** `services/run_reads.py:53` reads
  `sdk.load_status(run_id, cwd=None)`. The resulting `RunStatus` is richer
  than `RunSnapshot`: it carries `artefacts` (`run_reads.py:123-131`),
  state-derived `next_actions` (`run_reads.py:117`) and `metrics`
  (`run_reads.py:57`). `RunSnapshot` (`sdk/run_control/types.py:78-100`) is a
  deliberately focal control projection with no `artefacts` / `next_actions`
  / `metrics` field, so it does not cover the `orcho_run_status` wire
  contract. `meta` is *not* a verbatim pass-through: it is the highest-
  frequency poll, so `services/meta_summary.summarize_run_meta` projects it
  to a summary-only shape (phase bodies → `*_chars` / `*_count` markers,
  task text truncated, receipts collapsed to `subtask_id` + `state`) before
  it crosses the wire. Full bodies stay on disk and are reached through
  `orcho_run_evidence` / `orcho_run_metrics`; the `include` arg (with
  `["all"]` as the verbatim escape hatch) opts specific families back in.
- **(g) `event_tail.JsonlTailer` — KEEP.** `event_tail.py:69` is a
  `run_dir`-based JSONL tail *thread*, a non-production utility (it is not
  wired into any production tool — only referenced by the test layering in
  `tests/conftest.py:52,79`). Production observation notifications flow
  through `observe/watch.py`, which reads via
  `services.run_events.read_run_events` (`watch.py:28,70,262`). The SDK
  `tail_run_events` (`service.py:101-121`) resolves by `run_id` /
  `workspace` / `runs_dir`, not a raw `run_dir`, so it is not a drop-in for
  the `run_dir`-keyed utility.
- **(h) Supervisor spawn / resume / cancel — NO-TOUCH.**
  `run_control/lifecycle.py` drives the MCP `supervisor` subprocess
  (`lifecycle.py:65-89,184-194,213-216`). Core has no run supervisor:
  `RunService.cancel` always returns
  `RunControlUnsupported(reason="no-core-supervisor")`
  (`service.py:410-414`) and `RunService.resume` returns
  `cross-resume-not-in-slice` for any non single-project run
  (`service.py:337-340`). Routing resume/cancel through `RunService` would
  surface those unsupported results instead of acting, so the supervisor
  path stays untouched.
- **(i) Delivery decisions — ADOPTED.** `run_control/delivery.py` calls
  `sdk.decide_delivery(run_id, action, note=note, cwd=None)` and mirrors the
  SDK `DeliveryDecisionResult` into `DeliveryDecideResult`. The companion
  projection `services/delivery_gate.py` calls
  `sdk.delivery_decision_state(run_id, cwd=None)` for gate kind and action
  availability, then enriches the wire projection from durable artifacts.
  MCP never applies a patch or writes git directly; core owns the decision
  state transition.
- **(j) `resources/` — NO CHANGE.** The resource adapters import no `sdk` /
  `pipeline` and perform no direct file IO (verified: a grep for
  `import sdk` / `from sdk` / `import pipeline` / `open(` / `read_text` /
  `.glob(` across `src/orcho_mcp/resources/` returns nothing); they delegate
  to `services/`. There is nothing for the SDK slice to adopt here.
- **(k) Generic gate decisions — NO-TOUCH.** Generic gate pauses still resolve
  through `core.resolve_gate_decision` (run / skip), which does not reduce to
  `phase_handoff_decide` or post-release delivery. `build_decision_command`
  explicitly rejects a `kind="gate"` pending action, and core ships no generic
  gate command. Phase-handoff, delivery, and generic gate decisions remain
  separate mechanisms.

The adopted run-control surfaces are now event reads (a), typed pilot start
(b), and post-release delivery decisions (i). The remaining keep / no-touch
rows diverge from SDK coverage as detailed above.

## Test layout

The unit-test tree mirrors the production tree. Every production
domain has a matching `tests/unit/<domain>/` directory with at least
one test file. The full mapping is the contract — moving a production
module without moving its tests breaks it.

| Production | Unit tests |
|---|---|
| `src/orcho_mcp/authoring/` | `tests/unit/authoring/` |
| `src/orcho_mcp/inspection/` | `tests/unit/inspection/` |
| `src/orcho_mcp/observe/` | `tests/unit/observe/` |
| `src/orcho_mcp/resources/` | `tests/unit/resources/` |
| `src/orcho_mcp/run_control/` | `tests/unit/run_control/` |
| `src/orcho_mcp/services/` | `tests/unit/services/` |
| `src/orcho_mcp/supervisor/` | `tests/unit/supervisor/` |
| `src/orcho_mcp/client_interactions.py` | `tests/unit/client/` |
| `src/orcho_mcp/prompts.py` + `src/orcho_mcp/onboarding.py` | `tests/unit/prompts/` |
| `src/orcho_mcp/workflows.py` | `tests/unit/workflows/` |
| `src/orcho_mcp/workspace_state.py` | `tests/unit/workspace_state/` |
| (all of the above) | `tests/unit/architecture/` — boundary guards |

Protocol-layer tests (L2 registration / L3 stdio) live in
`tests/integration/protocol/`. End-to-end mock-pipeline tests (L4)
live in `tests/acceptance/mock_pipeline/` and are gated behind
`pytest -m mcp_integration`. Shared fixtures live in `tests/fixtures/`.

`tests/README.md` carries the same mapping in operator-facing form;
keep both in sync when adding a domain.

For testing philosophy, fixture style, and the layer-by-layer verification
model, see `docs/testing.md`.

For the observation-delivery contract, including why MCP keeps
cursor-based replay as the reliable path and treats notifications as
delivery accelerators, see
[`docs/architecture/observation_delivery.md`](observation_delivery.md).

## Public catalog

The MCP wire surface is snapshotted in `docs/mcp_schema.json`. Any
intentional tool / resource / prompt / schema change updates the
snapshot in the same commit. CI compares the live schema against the
snapshot.

## When a guard fails

The guards in `tests/unit/architecture/` are deliberately strict.
When one fails:

1. Read the test's docstring — it explains the invariant.
2. Decide whether the guard is the source of truth or the code is.
   For boundary rules above, the guard is the source of truth.
3. If the code legitimately needs to cross the boundary, the fix is a
   refactor (push the call into the correct domain) — not a
   guard-list edit. Guard-list edits are reserved for genuine
   architectural changes that this document is updated to reflect.

## When a guard has to relax

Sometimes the right move is to weaken a guard — a new exemption, a
raised soft cap, a removed forbidden symbol. That is allowed, but it
is a load-bearing change, not a CI cleanup. A PR that relaxes any
guard under `tests/unit/architecture/` must satisfy three things in
the same change:

1. **Explain the new architectural shape**, not just the new
   exception. Describe what now lives where, why the previous shape
   no longer fits, and what readers should expect going forward.
2. **Update this document** to match. The English description of the
   contract has to follow the executable contract — otherwise the
   guard stops being a contract and becomes a hurdle.
3. **Demonstrate the refactor alternative was considered and
   rejected.** A guard that gets relaxed because the diff is easier
   than the refactor is how architecture dies. The reviewer should
   see the rejected alternative in the PR description.

Guards are not sacred tablets. They are also not obstacles to be
filed down for a green CI run. They are the part of the codebase
that documents the architectural form we have chosen to defend.
