"""orcho_mcp.inspection.diff — read side of the captured ``diff.patch`` artifact.

Sync public function ``inspect_run_diff`` backs the ``orcho_run_diff``
MCP tool. The pipeline writes ``diff.patch`` at run lifecycle time;
this module returns it under typed projections (``preview``, ``stat``,
``full``) with a byte cap. Missing artifact is a typed ``found=False``
result — not a JSON-RPC error — because clean runs and runs predating
the artifact are both valid.

SDK alias ``_sdk_get_run_diff`` lives here so the MCP adapter layer
does not call the SDK directly.
``_RUN_DIFF_MAX_BYTES_CAP`` is exported so tests can reference the same
hard ceiling the validator enforces.
"""
from __future__ import annotations

from typing import Literal

from sdk import get_run_diff as _sdk_get_run_diff

from orcho_mcp.errors import InvalidPlanError
from orcho_mcp.schemas import RunDiffFile, RunDiffResult
from orcho_mcp.services.errors import map_sdk_errors

_RUN_DIFF_MAX_BYTES_CAP = 2_000_000


def inspect_run_diff(
    run_id: str,
    mode: Literal["preview", "stat", "full"] = "preview",
    path: str | None = None,
    phase: str | None = None,
    max_bytes: int = 200_000,
) -> RunDiffResult:
    """Read a captured ``diff.patch`` artifact.

    See ``orcho_run_diff`` docstring in ``orcho_mcp.tools`` for the
    wire contract. This module owns the implementation; the tool is a
    thin sync shim. ``phase`` selects between the run-level cumulative
    diff (``phase=None``) and a per-phase artifact
    (``phase=<name>``); validation lives in the SDK and surfaces here
    as :class:`InvalidPlanError` via the existing ``ValueError`` wrap.
    """
    if max_bytes <= 0 or max_bytes > _RUN_DIFF_MAX_BYTES_CAP:
        raise InvalidPlanError(
            f"orcho_run_diff: max_bytes must be in "
            f"(0, {_RUN_DIFF_MAX_BYTES_CAP}], got {max_bytes!r}",
        )
    with map_sdk_errors(run_id):
        r = _sdk_get_run_diff(
            run_id,
            cwd=None,
            mode=mode,
            path=path,
            phase=phase,
            max_bytes=max_bytes,
            color=False,
        )

    return RunDiffResult(
        run_id=r.run_id,
        found=r.found,
        mode=r.mode,  # type: ignore[arg-type]
        diff_path=r.diff_path,
        files=[
            RunDiffFile(path=f.path, added=f.added, removed=f.removed)
            for f in r.files
        ],
        content=r.content,
        truncated=r.truncated,
        max_bytes=r.max_bytes,
        message=r.message,
        scope=r.scope,
        phase=r.phase,
    )


__all__ = ["inspect_run_diff"]
