"""orcho_mcp.run_control.delivery — delivery decision service entry.

Backs the ``orcho_delivery_decide`` MCP tool. This module is a pure adapter
over the orcho-core SDK delivery decision entrypoint: it never applies a patch,
touches git directly, or re-implements delivery policy. The SDK owns the
durable gate checks and state transition; MCP maps SDK errors into its wire
error taxonomy and mirrors the typed result.
"""
from __future__ import annotations

from sdk import decide_delivery as _sdk_decide_delivery

from orcho_mcp.schemas import DeliveryDecideResult
from orcho_mcp.services.errors import map_sdk_errors


def decide_delivery(
    run_id: str,
    action: str,
    note: str | None = None,
) -> DeliveryDecideResult:
    """Resolve a parked delivery / correction gate through the SDK.

    Error mapping is owned by ``orcho_mcp.services.errors.map_sdk_errors``:
    missing workspace / run state stays consistent with read tools, and SDK
    input validation becomes ``InvalidPlanError``.
    """
    with map_sdk_errors(run_id):
        result = _sdk_decide_delivery(
            run_id,
            action,  # type: ignore[arg-type]
            note=note,
            cwd=None,
        )

    # ``scope_disclosure`` is read defensively: a core that predates the
    # delivery-scope axis simply yields an empty tuple, never an AttributeError.
    scope_disclosure = getattr(result, "scope_disclosure", ()) or ()

    return DeliveryDecideResult(
        run_id=result.run_id,
        action=result.action,
        accepted=result.accepted,
        status=result.status,
        terminal_outcome=result.terminal_outcome,
        halt_reason=result.halt_reason,
        artifact_paths=list(result.artifact_paths),
        commit_sha=result.commit_sha,
        blocker=result.blocker,
        followup_run_id=result.followup_run_id,
        scope_disclosure=[str(p) for p in scope_disclosure if isinstance(p, str)],
    )


__all__ = ["decide_delivery"]
