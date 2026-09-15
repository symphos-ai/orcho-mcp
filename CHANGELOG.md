# Changelog

## Unreleased

## 1.0.0 - 2026-09-15

The MCP server exposes criterion evidence and recoverable delivery from
`orcho-core` 1.0.0 through a consistent operator surface.

### Added

- Criterion evidence matrices, human-decision history, and shared readiness
  summaries in evidence, status, diagnosis, and delivery inspection.
- `orcho_criterion_decide` records explicit acceptance or rejection of a human
  criterion; missing decisions are requested through supported elicitation or
  returned as required operator input without writing a decision.
- Live verification progress and diagnosis of unrecorded delivery commits.
- `orcho_reconcile_delivery` records an existing delivery through the core SDK.
  SDK refusals remain structured results, and duplicate recording is handled
  by the engine's existing contract.

### Changed

- Requires `orcho-core>=1.0.0,<2.0`. Upgrade both packages together and restart
  the server before using the new contract.
- Plan acceptance criteria are typed objects rather than strings.
- `delivery_committed` distinguishes `null` (unknown) from `false` (recorded
  as not committed). Clients must preserve this distinction.
- Blocking criteria shape the suggested next action. Pending human criteria
  are named before resume, and a blocked delivery is not offered as a ready call.

### Fixed

- Unknown tool argument names are rejected before dispatch rather than silently
  ignored; the error names accepted parameters so callers can correct the call.
- Core exit code `3` is recorded as an intentional halt with its original cause.
- Producer-parked delivery gates expose their available decisions instead of
  suggesting a resume that would only park the run again.
- Evidence preserves engine-bound criteria. A failed decision readback does
  not retract a decision that was already durably recorded.
- Resume guidance follows the accepted source-run recovery operation; watch
  deadlines use a sleep-aware clock.

### Known Notes

- Reconciliation has the same legacy-discovery limitations as the core SDK:
  without a delivery ledger, a commit found only on a retained worktree branch
  may be undiscoverable. Passing a commit does not bypass discovery.
- General handoff waivers and review-context limitations in core final
  acceptance also apply to runs controlled through MCP.

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
