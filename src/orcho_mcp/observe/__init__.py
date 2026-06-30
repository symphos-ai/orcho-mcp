"""orcho_mcp.observe — observe-domain implementation behind MCP tool adapters.

Hosts the bounded event-summary builder, long-poll watch loop, paused-run
handoff hint synthesizer, and advisory workspace-state observer. The
matching ``@mcp.tool`` handlers (``orcho_run_events_summary``,
``orcho_run_watch``) live in ``orcho_mcp.tools`` and delegate to the
public service entries here (``build_run_events_summary``,
``watch_run``, ``build_handoff_hint``, ``record_workspace_observation``).

Sibling submodules import each other in one direction:
``summary`` ← ``handoff_hints`` ← ``watch``. ``observation`` is a leaf.
Keep this ``__init__`` empty — callers write full paths so each symbol
has exactly one canonical home and grep stays useful.
"""
