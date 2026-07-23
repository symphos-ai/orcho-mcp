# Changelog

## Unreleased

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
