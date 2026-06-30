"""orcho_mcp.run_control — run lifecycle + handoff decision implementations.

Backs MCP tool handlers (``orcho_run_start``, ``orcho_run_resume``,
``orcho_run_cancel``, ``orcho_phase_handoff_decide``, ``orcho_handoff_advice``,
``orcho_delivery_decide``) that live in
``orcho_mcp.tools`` as thin shims. Lifecycle entries spawn / continue /
stop pipeline subprocesses through ``orcho_mcp.supervisor``; the
handoff, advice, and delivery decision entries are pure state transitions /
read-only advisories over SDK helpers.

Sibling modules:

- ``lifecycle`` — ``start_run`` / ``resume_run`` / ``cancel_run``
  (async; talk to the supervisor singleton).
- ``handoff`` — ``decide_phase_handoff`` (sync; SDK state transition,
  no process management).
- ``advice`` — ``request_advice`` (sync; read-only SDK advisory pass that
  writes only an advice artifact, records no decision, applies nothing).
- ``delivery`` — ``decide_delivery`` (sync; SDK state transition,
  no process management).

Keep this ``__init__`` empty — callers import full paths so each
symbol has exactly one canonical home and grep stays useful.
"""
