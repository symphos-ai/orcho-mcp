# Changelog

## Unreleased

### Added

- Criterion-to-evidence traceability is readable from a client (ADR 0188). A
  captain no longer has to join plan JSON, subtask receipts, findings, and gate
  receipts by hand to answer "what proves this, and is it releasable?":
  - `orcho_run_evidence` gains a `criterion_matrix` slice — one row per plan
    acceptance criterion with its verification class, the executors that own
    it, a discriminated proof method (official gates / agent inspection /
    operator instructions), its proof references, state, and whether it blocks;
  - the `plan` slice now carries typed criteria (stable ids, verification
    class, complete `(command, hook, phase)` gate identities) plus per-task
    `acceptance_refs`, where it previously carried prose strings;
  - a `criterion_decisions` slice returns the run's append-only human-decision
    log, so a client that reconnects after a resume can resolve a
    `human_decision` proof reference into the decision behind it — who decided,
    when, with what note, and which earlier decision it superseded;
  - `orcho_run_status`, `orcho_run_diagnose`, and `orcho_delivery_gate` carry
    the same `criterion_readiness` summary, read through one projection path,
    so they cannot disagree about blockers.
- `orcho_criterion_decide` records an operator's `accept` / `reject` on a
  `human` criterion. Called without a verdict it asks a capable client through
  native MCP form elicitation, and otherwise returns
  `operator_input_required` with the exact missing input and a ready-call —
  writing nothing in either case. No verdict is ever inferred from
  conversation, and every admission rule (unknown criterion, non-human
  criterion, wrong run, conflicting decision) is enforced by the engine before
  anything is written.

### Changed

- Requires an `orcho-core` that exposes the ADR 0188 criterion SDK. MCP is a
  pure consumer here: it never recomputes a criterion state, readiness,
  receipt freshness, gate selection, executors, or blocking consequences, and
  an architecture guard now fails the build if a second SDK call site, a local
  state table, string criteria, or a `null` for an absent criterion payload
  appears.
- **Wire change:** `PlanSliceRecord.acceptance_criteria` is a list of typed
  criterion objects rather than a list of strings.
- An open blocking criterion now shapes the suggested next action, not just
  the readiness number. `orcho_run_status`, `orcho_run_diagnose`, and
  `orcho_delivery_gate` lead their `next_actions` with the
  `orcho_criterion_decide` call that can clear it, and a shipping
  `orcho_delivery_decide` call is demoted from `ready_call` to
  `operator_input_required` (naming the blocker count/state and any pending
  human criteria in `context`)
  instead of sitting beside a `ready: false` summary as if it were safe to
  forward. This applies to every blocker, including failed/missing executable
  proof and rejected human criteria; resume and read-only actions are untouched.
- A recorded decision is never retracted by a failed readback. The response
  carries the matrix as it stands after the write; if that read fails, the
  outcome stays `decision_recorded` and `matrix_error` says why, so an
  operator re-reads instead of retrying into "already decided".
- A run with no criterion contract OMITS `criterion_matrix` /
  `criterion_readiness` rather than sending `null`, keeping "this run predates
  the contract" distinguishable from "this plan declares no criteria" (which
  is an explicit empty matrix). A missing criterion SDK capability or malformed
  current matrix fails closed instead of masquerading as that absent case.

### Fixed

- `orcho_run_diagnose` / `orcho_run_resume` no longer point a terminal recovery
  run at a source resume that core's launch preflight refuses. When the source
  had a finalized `scheduled_gate_ledger.json` (closed at every runner-side
  `run.end`), the `recover_via_source_run` response carried a `ready_call`
  `orcho_run_resume(source)` that then failed with "same-run resume is
  blocked: parent has a finalized scheduled-gate ledger". Core now derives
  source resumability from that same preflight; when the source cannot be
  resumed in place but preflight accepts a `from_run_plan` launch off its
  persisted plan, the condition stays `recover_via_source_run` with
  `recommended_next_action='plan_artifact_continuation'` and the `ready_call`
  becomes `orcho_run_start(from_run_plan=<source>)` on both the diagnose and
  resume surfaces. Requires the matching `orcho-core` change.

- The plan slice's `allowed_modifications` read the durable plan artifact's
  top level, but the artifact is an `{"artifact_version", "plan"}` envelope, so
  the globs were always empty against a real run. It now reads the inner plan
  body; per-task `acceptance_refs` come from the public core SDK plan summary.

## 0.8.2 - 2026-08-29

Two defects that made a paused or abandoned run unreadable from the client
side, both found in a field report against 0.8.5.

### Changed

- Requires `orcho-core` 0.9.0. That release makes the dead-run detection this
  server relays actually fire: the liveness predicate behind `stalled` could
  never become true on a real run, so `orcho_run_diagnose` answered `active`
  for runs whose processes had been gone for hours.

### Fixed

- A paused verification gate now tells the operator what was rejected. The
  engine writes its findings — severity, evidence, and the required fix —
  under `meta.phase_handoff.artifacts.findings`; this server read only the
  payload's top level, so every gate pause arrived as `verdict: REJECTED` with
  `findings_summary: null` and nothing to act on. The reader now consults both,
  and a contract test drives it from the engine's own payload builder rather
  than a hand-written fixture, which is how the mismatch survived.
- A run abandoned by a dead server process is settled instead of being
  reported as live work forever. Runs are detached children reaped by a task
  inside this process; when the process itself goes away — a client restart, or
  a kill that takes the tree with it — nothing is left to record that the run
  ended. The recovery probe that exists for exactly this was never called
  outside the test suite. It now runs once per server start, before the first
  client request, and marks orphaned only those `running` entries whose
  recorded pid is proven dead. A probe failure is reported on stderr and never
  blocks startup.


## 0.8.1 - 2026-08-23

### Changed

- Requires `orcho-core` 0.8.3, which closes the Windows startup-hang family
  (a run could block forever before its first phase, could not be cancelled,
  and reported itself healthy the whole time).
- The live status card no longer calls a stopped run healthy. `state_class`
  gains `starting` (launched, no phase yet) and `stalled` (core's startup
  stall verdict), and `running_phase` now requires a real open phase instead
  of being the fall-through for any phase-empty card. A stalled card carries
  an inspect-or-cancel next action rather than "keep polling".
- `orcho_run_diagnose` surfaces core's `stalled` condition instead of
  flattening it into `active`, and does not advertise such a run as resumable.

## 0.8.0 - 2026-08-20

### Changed

- Requires `orcho-core` 0.8.0, which makes Orcho usable on native Windows
  (UTF-8 git output, concurrent stderr drain, sandbox auth passthrough) and
  moves `claude-glm` setup into the runtime adapter. Stalled-command evidence
  surfaced through the MCP tools now carries per-stream byte counts.

## 0.7.0 - 2026-08-11

### Added

- `orcho_workspace_cleanup_report` previews what a workspace cleanup would
  reclaim — separating reclaimable checkouts from work the engine protects as
  still-at-risk and from inert references with nothing left to remove — and
  changes nothing on disk.
- `orcho_workspace_cleanup_reclaim` performs the removal, but only against a
  selection an operator confirmed: it requires the `confirm_token` minted by a
  preceding report and re-derives that token from the live workspace, so a
  token that was never issued or whose selection has since changed is refused
  with nothing removed. The two halves are separate tools so allowlisting the
  preview does not allowlist the removal.

### Changed

- A stopped delivery or correction gate is published as context rather than a
  decision surface: it keeps its explanation but offers no actions, no default,
  and no ready `orcho_delivery_decide` call until the run is resumed.
- Requires `orcho-core>=0.7.0,<0.8` and reads the core 0.7 workspace-cleanup
  and delivery-decidability contracts.

## 0.6.0 - 2026-07-28

### Changed

- Requires `orcho-core>=0.6.0,<0.7` and reads the core 0.6 verification-cost,
  cross-plan, and execution-state contracts.
- Project verification configuration uses the typed granular cost vocabulary.
- Release-path GitHub Actions use immutable pins and CodeQL covers protected
  release branches.

### Fixed

- The MCP SDK is constrained to the supported 1.x line; clean installations
  cannot silently resolve the incompatible 2.x API.

## 0.5.0 - 2026-07-23

### Added

- Typed live status exposes engine-owned scheduled-gate execution and
  cross-project execution-graph state.
- Evidence inspection exposes the managed lifecycle and durable receipts of
  provider-owned commands.
- Run status projects the canonical scheduled-gate ledger, including repair
  and rerun history.

### Changed

- Run diagnosis, resume, correction follow-up, and handoff settlement delegate
  continuation semantics to the public `orcho-core` SDK.
- Workflow recipes use typed live status for progress instead of relying on a
  long-lived watch call.
- Requires `orcho-core>=0.5.0,<0.6`.

### Fixed

- A recorded retry or continue decision survives resume and is not
  misclassified as missing or as an implicit waiver.
- Interrupted runs no longer advertise a same-run resume call when the
  canonical core preflight requires a fresh implementation from the persisted
  plan.
- Optional evidence failures no longer break authoritative status reads.
- Delivery status exposes the published commit identity and reports the actual
  branch disposition.
- Project verification configuration requires provenance and lint gates.

### Documentation

- Documented the complete MCP control state machine and its decision graph.

## 0.4.0 - 2026-07-08

### Changed

- Profile reads go through the public `orcho-core` SDK profile catalogue
  surface instead of internal profile modules.
- Detached-launch mechanics for supervised runs are delegated to the SDK
  run-control launch surface.
- Requires `orcho-core>=0.4.0,<0.5`.

### Documentation

- Run inspection tool roles are clarified so clients pick the right tool for
  status, diff, evidence, and metrics reads.

## 0.3.0 - 2026-07-06

### Changed

- Requires `orcho-core>=0.3.0,<0.4`.

### Documentation

- Position Orcho as a production harness in the server-facing docs.

## 0.2.0 - 2026-07-05

### Added

- Typed run-readiness evidence slices for inspecting run state.
- Branch-policy delivery data surfaced in MCP projections.
- Recognition of the canonical `.orcho/.task-files/` task directory.
- `orcho-mcp --help` now explains what the server is and how to wire it into a
  client.

### Changed

- Requires `orcho-core>=0.2.0,<0.3`.

### Documentation

- Client setup covers install paths, Cursor, and the full tool catalog.
- Run-lifecycle and control-loop docs match the current MCP surface.
- Added Docker setup instructions for the MCP server.

## 0.1.0 - 2026-07-01

Initial release baseline for `orcho-mcp`.

### Added

- MCP server package exposing Orcho workflows to MCP-compatible clients.
- Stdio server entry point with tools, resources, prompts, and run-control helpers.
- Run observation, supervisor, inspection, authoring, and workflow service modules.
- Public dependency line on `orcho-core>=0.1.0,<0.2`.

### Known Notes

- This release establishes the first public package baseline and API line.
- The package is in alpha; public contracts should still be treated as early and evolving within the `0.1.x` line.
