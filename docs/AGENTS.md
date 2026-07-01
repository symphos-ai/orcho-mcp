# orcho-mcp docs Instructions

## Scope

This file applies to `docs/`.

Also obey `../AGENTS.md` and the workspace-level `../../AGENTS.md`.

## Public Documentation Style

Documentation describes the current product contract. Do not write release
history, migration notes, internal planning labels, or refactor commentary in
public docs.

Keep comments and prose factual:

- Say what the server does now.
- Name the contract a contributor must preserve.
- Link to the executable test or snapshot that enforces it.
- Prefer short trace diagrams and tables over narrative background.

If an old implementation detail no longer matters to a contributor, remove it
instead of explaining why it changed.

## Canonical Docs

- `docs/testing.md` defines the MCP testing philosophy and layer rules.
- `docs/architecture/mcp_boundaries.md` defines package boundaries.
- `docs/architecture/anatomy_of_a_request.md` traces request flow.
- `docs/run_lifecycle.md` documents run-control semantics.
- `docs/mcp_schema.json` is generated contract data; update it through the
  schema dump tool when the MCP wire surface intentionally changes.

## When Editing Docs

- Keep examples aligned with the current `tests/` layout and source packages.
- If a doc changes tool, resource, schema, or lifecycle behavior, check whether
  an architecture guard, schema snapshot, or resource catalog test also needs an
  update.
