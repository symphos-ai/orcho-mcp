"""orcho_mcp.inspection — post-run evidence + diff implementations.

Backs two MCP tool handlers (``orcho_run_evidence``, ``orcho_run_diff``)
that live in ``orcho_mcp.tools`` as thin shims. Both pass through to
typed SDK helpers, map SDK exceptions into ``orcho_mcp.errors``
subclasses, and convert SDK records into ``orcho_mcp.schemas``
response models.

Sibling modules:

- ``evidence`` — ``inspect_run_evidence`` (typed slice projections
  over the run's evidence bundle).
- ``diff`` — ``inspect_run_diff`` + ``_RUN_DIFF_MAX_BYTES_CAP``
  (read side of the captured ``diff.patch`` artifact).
- ``diagnosis`` — ``inspect_run_diagnosis`` (typed resume-situation
  verdict with unambiguously typed ``next_actions``, packed on top of
  ``services.run_projection.project_run_diagnosis``).

Keep this ``__init__`` empty — callers import full paths so each
symbol has exactly one canonical home and grep stays useful.
"""
