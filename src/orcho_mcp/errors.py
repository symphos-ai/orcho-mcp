"""orcho_mcp.errors — typed exception hierarchy.

Mapped to MCP error codes (InvalidParams / InternalError / etc.) by the
dispatch layer. Domain-specific so call-sites can ``raise RunNotFoundError``
without thinking about JSON-RPC error encodings.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from orcho_mcp.schemas import InspectOnlyControlResult


class OrchoMCPError(Exception):
    """Base for all orcho-mcp domain errors."""


class RunNotFoundError(OrchoMCPError):
    """Tool referenced a run_id that doesn't exist on disk or in the supervisor."""


class InspectOnlyControlError(OrchoMCPError):
    """A mutation tool targeted a run this MCP server cannot control.

    ``orcho_run_resume`` / ``orcho_phase_handoff_decide`` raise this when the
    target run was NOT started by this MCP server — it carries no durable
    ``mcp_supervisor.json`` with a resolvable ``project_dir``
    (``control='inspect_only'``), so MCP has no supervisor metadata to respawn
    or advance it and can only inspect it. The guard fires BEFORE the supervisor
    / SDK is touched: no subprocess spawns and no decision artifact is written.

    Raised, not returned, on purpose: the success ``outputSchema`` of the two
    mutation tools must stay byte-identical to before this guard existed, so the
    refusal travels the typed-error channel (like ``RunNotFoundError``) instead
    of widening the success return union. The typed controllability verdict and
    read-only next actions are exposed on ``orcho_run_diagnose`` (its ``control``
    / ``control_reason`` fields); the carried :class:`InspectOnlyControlResult`
    (``result``) holds the same typed refusal payload — including the read-only
    ``next_actions`` and the CLI-control pointer — for callers that want it.
    """

    def __init__(self, result: InspectOnlyControlResult) -> None:
        self.result = result
        super().__init__(result.message)


class WorkspaceNotResolvedError(OrchoMCPError):
    """Workspace context was needed but couldn't be derived from env or args."""


class PipelineSpawnError(OrchoMCPError):
    """Failed to spawn a pipeline subprocess."""


class InvalidPlanError(OrchoMCPError):
    """plan_validate found structural errors in the supplied markdown."""
