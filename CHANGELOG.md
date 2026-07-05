# Changelog

## Unreleased

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
