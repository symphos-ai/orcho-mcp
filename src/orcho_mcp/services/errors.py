"""orcho_mcp.services.errors — the single SDK→MCP / command error owner.

This module is the ONE place that catches SDK exception *types* and the
bare Python exceptions leaking out of run-control delegation, and
translates them into the canonical :mod:`orcho_mcp.errors` hierarchy.
Centralising the translation keeps message texts and error types
consistent across every tool, and lets the architecture guard assert
that the SDK error types are caught nowhere else.

Two surfaces:

- :func:`map_sdk_errors` — wrap an SDK read / command call so SDK
  exceptions surface as the matching MCP error:

  * ``RunNotFound``            → ``RunNotFoundError("run not found: <run_id>")``
  * ``NoWorkspace``            → ``WorkspaceNotResolvedError(str(e))``
  * ``InvalidPhaseHandoffState`` → ``InvalidPlanError(str(e))``
  * ``ValueError``             → ``InvalidPlanError(str(e))``

  Wrap only the SDK call itself, never the post-success domain checks
  (``if not m.raw: raise RunNotFoundError(...)``) — those raise typed
  MCP errors already and must pass through unchanged.

- :func:`map_command_errors` — wrap a run-control delegation into the
  supervisor. Already-typed ``OrchoMCPError`` (``PipelineSpawnError`` /
  ``WorkspaceNotResolvedError`` / ``RunNotFoundError`` /
  ``InvalidPlanError``) pass through unchanged — never re-wrapped into a
  different type. A bare ``ValueError`` (e.g. the invalid cancel-mode
  ``ValueError`` raised by ``supervisor.cancel``) becomes
  ``InvalidPlanError``; any other non-``OrchoMCPError`` leak becomes
  ``PipelineSpawnError`` — the supervisor/subprocess slot of the
  command error taxonomy.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager

from sdk import (
    EvidenceInvalid as _SDKEvidenceInvalid,
    InvalidPhaseHandoffState as _SDKInvalidPhaseHandoffState,
    NoWorkspace as _SDKNoWorkspace,
    RunNotFound as _SDKRunNotFound,
)

from orcho_mcp.errors import (
    InvalidPlanError,
    OrchoMCPError,
    PipelineSpawnError,
    RunNotFoundError,
    WorkspaceNotResolvedError,
)


@contextmanager
def map_sdk_errors(run_id: str | None = None) -> Iterator[None]:
    """Translate SDK exceptions into the canonical MCP error hierarchy.

    Wrap the SDK call (and, where the original site did, the wire-model
    construction that can raise a Pydantic ``ValueError``). The
    ``run_id`` flavours the ``RunNotFoundError`` message so it matches
    the historical ``"run not found: <run_id>"`` text exactly.
    """
    try:
        yield
    except _SDKRunNotFound as e:
        raise RunNotFoundError(f"run not found: {run_id}") from e
    except _SDKNoWorkspace as e:
        raise WorkspaceNotResolvedError(str(e)) from e
    except _SDKInvalidPhaseHandoffState as e:
        # State / contract mismatch (wrong status, mismatched handoff
        # id, action not in available_actions, payload-divergence
        # conflict). Surfaced as InvalidPlanError so clients distinguish
        # missing-run from bad-request.
        raise InvalidPlanError(str(e)) from e
    except ValueError as e:
        # SDK-side input validation (bad action / severity / phase,
        # malformed id, feedback-required-without-feedback, …) — same
        # bad-request bucket as the contract-mismatch case above.
        raise InvalidPlanError(str(e)) from e


def read_optional_evidence[T](reader: Callable[[], T]) -> T | None:
    """Return one evidence-derived enrichment, or ``None`` when unavailable.

    High-frequency status reads must not fail because an optional evidence
    projection cannot be composed. Explicit evidence tools still surface the
    original typed SDK error; only callers that deliberately use this helper
    opt into absence semantics.
    """
    try:
        return reader()
    except _SDKEvidenceInvalid:
        return None


@contextmanager
def map_command_errors() -> Iterator[None]:
    """Translate run-control delegation leaks into the canonical hierarchy.

    Already-typed ``OrchoMCPError`` (the supervisor's
    ``PipelineSpawnError`` / ``WorkspaceNotResolvedError`` /
    ``RunNotFoundError`` and the run-control ``InvalidPlanError``) pass
    through verbatim — never re-wrapped into another type. A bare
    ``ValueError`` (e.g. ``supervisor.cancel`` rejecting an invalid
    mode) maps to ``InvalidPlanError``; any other non-``OrchoMCPError``
    leak maps to ``PipelineSpawnError``.
    """
    try:
        yield
    except OrchoMCPError:
        raise
    except ValueError as e:
        raise InvalidPlanError(str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise PipelineSpawnError(str(e)) from e


__all__ = ["map_command_errors", "map_sdk_errors", "read_optional_evidence"]
