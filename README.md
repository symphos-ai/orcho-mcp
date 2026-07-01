# orcho-mcp

[![PyPI](https://img.shields.io/pypi/v/orcho-mcp.svg)](https://pypi.org/project/orcho-mcp/)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://pypi.org/project/orcho-mcp/)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)
[![CI](https://github.com/symphos-ai/orcho-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/symphos-ai/orcho-mcp/actions/workflows/ci.yml)
[![DCO](https://github.com/symphos-ai/orcho-mcp/actions/workflows/dco.yml/badge.svg)](https://github.com/symphos-ai/orcho-mcp/actions/workflows/dco.yml)
[![Release](https://github.com/symphos-ai/orcho-mcp/actions/workflows/release.yml/badge.svg)](https://github.com/symphos-ai/orcho-mcp/actions/workflows/release.yml)
[![codecov](https://codecov.io/gh/symphos-ai/orcho-mcp/branch/main/graph/badge.svg)](https://codecov.io/gh/symphos-ai/orcho-mcp)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/symphos-ai/orcho-mcp/badge)](https://scorecard.dev/viewer/?uri=github.com/symphos-ai/orcho-mcp)

[Model Context Protocol](https://modelcontextprotocol.io) server for Orcho.

Exposes orcho's runtime to MCP-aware clients (Claude Code, Cursor, Zed, and other MCP-speaking tools) over stdio. Full async control loop — act, observe, decide, inspect — without raw log scraping.

> **Status:** ``v0.1.0`` public release line. Core control loop surfaces are available:
>
> - **Act**: ``orcho_run_start`` / ``orcho_run_resume`` / ``orcho_run_cancel`` with L4-test-pinned semantics (process-group signal handling, supervisor-owned restart-recovery, race-aware cancel).
> - **Observe**: ``orcho_run_status`` / ``orcho_run_history`` / ``orcho_run_metrics`` / ``orcho_run_events_tail`` — read-only, polling-friendly.
> - **Decide**: ``orcho_phase_handoff_decide`` — generic phase-handoff state transition for paused runs. The pipeline pauses with ``status=awaiting_phase_handoff`` when a phase's declared ``handoff`` policy fires; ``continue`` / ``retry_feedback`` / ``halt`` write a decision artifact (``halt`` flips ``meta.status`` to ``halted`` synchronously). Pure state transition; never spawns.
> - **Inspect**: ``orcho_run_evidence`` — typed inspection slices (``plan`` / ``findings`` / ``commands`` / ``artifacts`` / ``errors`` / ``sub_runs`` / ``all``) with severity filter (P0..P3).
>
> Live progress: ``orcho_run_watch`` emits ordered ``notifications/progress`` when the MCP request carries a ``progressToken``. Clients that don't carry one poll ``orcho_run_status`` / ``orcho_run_events_tail`` against the same run state.

## Install

If `pipx` is missing, install it first. On macOS with Homebrew:

```bash
brew install pipx
pipx ensurepath
exec zsh -l
```

For Linux or Windows, use the
[official pipx installation guide](https://pipx.pypa.io/stable/installation/).

### Recommended CLI install

Use the `orcho` distribution with the `mcp` extra when you want both the Orcho
commands and the MCP server available from your shell. `pipx` keeps the command
set isolated from the current project or Python environment.

```bash
pipx install "orcho[mcp]"
orcho-mcp --help
```

### Direct MCP package install

Use `pip` when you intentionally want `orcho-mcp` in the active virtual
environment, CI image, devcontainer, or Docker image.

```bash
python -m pip install orcho-mcp
```

This pulls `orcho-core` (the engine), the official `mcp` Python SDK, and the runtime pieces orcho-mcp depends on.

## Create a workspace

Orcho writes run state into an Orcho workspace. Start by pointing it at
the folder that groups your project repos:

```bash
orcho workspace init ~/www/my-workspace
```

The command creates `~/www/my-workspace/workspace-orchestrator/`,
including `.orcho/` settings and extension-point guides, and prints the
MCP config snippet for that workspace. To write the snippet directly
into a project-local MCP config:

```bash
ORCHO_MCP_COMMAND="$(command -v orcho-mcp)"

orcho workspace init ~/www/my-workspace \
  --mcp-config ~/www/my-workspace/.mcp.json \
  --mcp-server-name orcho-my-workspace \
  --orcho-mcp-command "$ORCHO_MCP_COMMAND"
```

`ORCHO_MCP_COMMAND` must point to the command your MCP client can run.
For packaged installs this is normally `orcho-mcp`. For source installs,
use the absolute path inside the Orcho environment, for example
`/Users/me/orcho-preview/orcho-core/.venv/bin/orcho-mcp`.

Each MCP server process owns one workspace through `ORCHO_WORKSPACE`.
For multiple workspaces, add multiple MCP server entries.

## Register with an MCP-aware client

Each client has its own MCP registry/config format. Use
[`docs/mcp_client_setup.md`](docs/mcp_client_setup.md) for copy-paste
instructions for Codex CLI/app, Claude Code, Gemini CLI, the Claude app,
and Antigravity.

## What's inside

```bash
npx @modelcontextprotocol/inspector orcho-mcp
```

Opens a web UI on localhost showing every registered tool, resource, and prompt with full JSON schemas — Anthropic's official Inspector ≈ Swagger UI for MCP.

A static catalogue is also committed at [`docs/mcp_schema.json`](docs/mcp_schema.json) — the same shape, snapshotted in CI.

## Control loop

The full contract — **starting**, **observing**, **resuming**, **cancelling**, **deciding** (QA gate), and **inspecting** runs through the MCP wire — lives in [`docs/run_lifecycle.md`](docs/run_lifecycle.md). Tool docstrings stay terse; that file is the long-form reference.

Tool naming is consistent: every run-lifecycle tool is `orcho_run_<verb>`. State-transition and inspection tools sit beside that group with their own names:

| Group | Tools |
|---|---|
| **Act** | `orcho_run_start`, `orcho_run_resume`, `orcho_run_cancel` |
| **Observe** | `orcho_run_status`, `orcho_run_history`, `orcho_run_metrics`, `orcho_run_events_tail` |
| **Decide** | `orcho_phase_handoff_decide` |
| **Inspect** | `orcho_run_evidence`, `orcho_run_diff` |

For an end-to-end walkthrough of the full control loop with code, see [`docs/control_loop_walkthrough.md`](docs/control_loop_walkthrough.md).

## Architecture

orcho-mcp is one of the public Orcho runtime packages:

- `orcho-core` — pipeline runtime + CLI (Apache-2.0).
- **`orcho-mcp`** — MCP server, this repo (Apache-2.0).

The post-v1 cross-MCP consumer roadmap (orcho-as-MCP-client — pipeline agents calling external GitHub / Linear / Slack MCP servers) is documented in `orcho-core/docs/plans/2026-05-06-cross-mcp-orchestration.md`.

For contributor-facing architecture and test guidance:

- [`docs/architecture/mcp_boundaries.md`](docs/architecture/mcp_boundaries.md)
  describes the package boundaries enforced by the architecture tests.
- [`docs/architecture/observation_delivery.md`](docs/architecture/observation_delivery.md)
  defines the durable replay contract for MCP observation and notification use.
- [`docs/testing.md`](docs/testing.md) explains the test philosophy, layer model,
  fixture style, and verification commands.

## License

Apache-2.0. See [LICENSE](LICENSE).
