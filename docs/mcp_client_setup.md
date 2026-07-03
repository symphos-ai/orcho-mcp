# MCP client setup

Orcho MCP is a stdio server. A client must know two things:

1. Which command starts the server.
2. Which workspace that server owns.

Start by creating or choosing a workspace:

```bash
orcho workspace init ~/www/my-workspace
```

The command creates `workspace-orchestrator/.orcho/` with settings and
safe extension-point guides. Re-running it does not overwrite existing
scaffold files.

Then resolve the server command:

```bash
ORCHO_MCP_COMMAND="$(command -v orcho-mcp)"
echo "$ORCHO_MCP_COMMAND"
```

Use an absolute command path for developer setups. A bare `orcho-mcp` is
portable, but it is only safe when the client and your shell resolve the same
installation. If multiple Orcho installs exist on the machine, choose the
`orcho-mcp` binary from the same environment as the Orcho version you want the
client to run.

If `command -v orcho-mcp` prints an empty line, or points at a different
environment than the one under test, set the command explicitly:

```bash
ORCHO_MCP_COMMAND="$HOME/orcho-preview/orcho-core/.venv/bin/orcho-mcp"
test -x "$ORCHO_MCP_COMMAND" && echo ok
```

Before registering the server, verify both commands if you have more than one
Orcho checkout or install:

```bash
command -v orcho
command -v orcho-mcp
"$ORCHO_MCP_COMMAND" --version
```

## Docker server command

If you use the container image instead of a native install, the MCP command is
`docker` and the server command becomes its argument list. Mount the workspace
parent and bind the MCP server to the workspace inside the container:

```bash
docker run --rm -i \
  -v "$HOME/www/my-workspace:/workspace" \
  -v "$HOME/.orcho-auth:/agent-auth:ro" \
  -e ORCHO_WORKSPACE=/workspace/workspace-orchestrator \
  ghcr.io/symphos-ai/orcho \
  orcho-mcp
```

For JSON-based clients, use the same shape:

```json
{
  "command": "docker",
  "args": [
    "run", "--rm", "-i",
    "-v", "/Users/me/www/my-workspace:/workspace",
    "-v", "/Users/me/.orcho-auth:/agent-auth:ro",
    "-e", "ORCHO_WORKSPACE=/workspace/workspace-orchestrator",
    "ghcr.io/symphos-ai/orcho",
    "orcho-mcp"
  ]
}
```

For terminal registration commands, put the `docker run ... orcho-mcp` command
after the final `--` instead of `"$ORCHO_MCP_COMMAND"`.

The workspace path used below is:

```text
~/www/my-workspace/workspace-orchestrator
```

For multiple workspaces, register one Orcho MCP server per workspace with a
distinct name, for example `orcho-demo-mcp`, `orcho-atas-mcp`, and
`orcho-qcg-mcp`. After switching clients or sessions, call
`orcho_workspace_info` first and verify the reported `workspace_dir`.

## Codex

Codex does not read project `.mcp.json` files automatically. Register the
server with the Codex MCP registry from your terminal. This applies to both
Codex CLI and the Codex app:

```bash
codex mcp add orcho-my-workspace \
  --env ORCHO_WORKSPACE="$HOME/www/my-workspace/workspace-orchestrator" \
  -- "$ORCHO_MCP_COMMAND"
```

Verify:

```bash
codex mcp list
codex mcp get orcho-my-workspace
```

Restart the Codex session/app after adding or changing the server.
Already-open sessions usually keep the MCP catalogue they loaded at startup.

## Claude Code

Claude Code can register the server from the shell:

```bash
claude mcp add orcho-my-workspace \
  --env ORCHO_WORKSPACE="$HOME/www/my-workspace/workspace-orchestrator" \
  -- "$ORCHO_MCP_COMMAND"
```

Restart the Claude Code session after changing the server.

## Gemini CLI

Gemini CLI can register the server from the shell:

```bash
gemini mcp add --env ORCHO_WORKSPACE="$HOME/www/my-workspace/workspace-orchestrator" \
  orcho-my-workspace "$ORCHO_MCP_COMMAND"
```

Restart the Gemini session after changing the server.

## Claude app

If you use the Claude app's MCP JSON config, copy a server entry shaped like:

```json
{
  "mcpServers": {
    "orcho-my-workspace": {
      "command": "/absolute/path/to/orcho-mcp",
      "args": [],
      "env": {
        "ORCHO_WORKSPACE": "/Users/me/www/my-workspace/workspace-orchestrator"
      }
    }
  }
}
```

Use the real value of `ORCHO_MCP_COMMAND` for `command`. This JSON is copied
into the app config; it is not a shell command. Restart the app after saving
the config.

## Antigravity

Antigravity stores MCP servers in:

```text
~/Library/Application Support/Antigravity/User/mcp.json
```

Copy an Orcho entry under `servers`:

```json
{
  "servers": {
    "orcho-my-workspace": {
      "type": "stdio",
      "command": "/absolute/path/to/orcho-mcp",
      "args": [],
      "env": {
        "ORCHO_WORKSPACE": "/Users/me/www/my-workspace/workspace-orchestrator"
      }
    }
  },
  "inputs": []
}
```

If your Antigravity build does not pass `env` from `mcp.json`, create a tiny
wrapper script that exports `ORCHO_WORKSPACE` and then execs `orcho-mcp`, and
use that wrapper as `command`.

Restart Antigravity after changing `mcp.json`.

## Verify from the client

After restart, the client should expose these Orcho tools:

```text
orcho_workspace_info
orcho_run_start
orcho_run_status
orcho_run_watch            # long-poll; until=next_event|phase_change|subtask|handoff_or_terminal|terminal
orcho_run_events_tail
orcho_run_events_summary
orcho_run_evidence         # slices incl. "receipts" (per-subtask delivery + attestation)
orcho_run_diff
orcho_phase_handoff_decide
orcho_run_resume
orcho_run_metrics
orcho_run_history
```

Call `orcho_workspace_info` first. It should report the workspace you
configured. If it reports another workspace, the client is launching a
different server entry or has not restarted.
