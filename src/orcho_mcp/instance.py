"""orcho_mcp.instance — the single FastMCP server instance.

Lives in its own module so every other file (server, tools, resources, …)
imports the same object regardless of how the server was launched. Without
this split, ``python -m orcho_mcp.server`` and ``orcho_mcp.tools`` end up
holding two distinct FastMCP instances (``__main__`` vs ``orcho_mcp.server``)
and decorator registrations go to the wrong one.
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

# Server-level intent→tool map. Clients read this to pick the right tool by
# what they want to do, instead of scanning every tool description. Keep it
# compact and neutral; the per-tool docstrings carry the detail.
INSTRUCTIONS = """\
Orcho drives multi-phase project runs and exposes them as MCP tools. Pick a \
tool by intent:

- start a run → orcho_run_start (real runs, mock or live)
- progress / where is the run now / subtask position → orcho_run_live_status; \
for a blocking long-poll that returns on the next change → orcho_run_watch
- durable status snapshot + pause/delivery checks → orcho_run_status
- decide a paused phase-handoff → orcho_phase_handoff_decide (advice first via \
orcho_handoff_advice), then orcho_run_resume to continue
- evidence / findings / proof → orcho_run_evidence
- what changed (patch) → orcho_run_diff
- metrics / token cost → orcho_run_metrics
- diagnose a stuck or failed run → orcho_run_diagnose

For live progress prefer orcho_run_live_status over orcho_run_status."""

mcp = FastMCP("orcho", instructions=INSTRUCTIONS)
