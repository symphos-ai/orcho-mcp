"""orcho_mcp.services.workspace_cleanup — SDK adapter for workspace cleanup.

Projects the public ``sdk.report_workspace_cleanup`` /
``sdk.reclaim_workspace_cleanup`` pair onto the MCP wire and adds the one
thing a chat client needs that a CLI operator gets for free: a *confirmed*
destructive step.

On the CLI the operator types the reclaim flags themselves, having just read
the report on their own screen. Over MCP the caller is a model, and nothing
in the protocol distinguishes "the operator saw the selection and agreed"
from "the model decided to tidy up". So reclaim here is gated on a
``confirm_token`` that only a preceding report can produce: the token
fingerprints the exact selection, and the reclaim recomputes it against the
live workspace before touching anything. A stale or invented token is a
refusal, not a sweep.

The engine still owns every selection and retention decision — this module
adds no policy of its own. Workspace resolution follows the same
walk-up-disabled invariant as :mod:`orcho_mcp.services.run_lookup`: the
already-resolved runs directory is passed explicitly and ``cwd=None`` keeps
the SDK from binding to whatever directory the server was launched from.
"""
from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import Literal

from sdk import (
    WorkspaceCleanupReport as _SDKReport,
    reclaim_workspace_cleanup,
    report_workspace_cleanup,
)

from orcho_mcp.errors import WorkspaceCleanupConfirmationError
from orcho_mcp.schemas.shared import NextActionRecord
from orcho_mcp.schemas.workspace import (
    WorkspaceCleanupReasonRow,
    WorkspaceCleanupReceiptResult,
    WorkspaceCleanupReportResult,
)
from orcho_mcp.services.run_lookup import runs_dir_or_raise

_DEFAULT_OLDER_THAN_DAYS = 30
_TOKEN_PREFIX = "wsclean"
_TOKEN_HEX_LEN = 16


def project_workspace_cleanup_report(
    older_than_days: int | None = None,
    force: bool = False,
) -> WorkspaceCleanupReportResult:
    """Return the read-only cleanup selection plus its ``confirm_token``.

    See the ``orcho_workspace_cleanup_report`` tool docstring (in
    ``orcho_mcp.tools``) for the wire contract.
    """
    cutoff_days = _resolve_older_than_days(older_than_days, force=force)
    report = _select(cutoff_days, force=force)
    token = _confirm_token(report, cutoff_days=cutoff_days, force=force)
    return WorkspaceCleanupReportResult(
        runs_dir=str(report.runs_dir),
        older_than_days=cutoff_days,
        force=force,
        reclaimable_count=report.reclaimable_count,
        protected_count=report.protected_count,
        inert_count=report.inert_count,
        reclaimable_run_root_count=report.reclaimable_run_root_count,
        protected_run_root_count=report.protected_run_root_count,
        reclaimable_reasons=_rows(report.reclaimable_reasons),
        protected_reasons=_rows(report.protected_reasons),
        inert_reasons=_rows(report.inert_reasons),
        reclaimable_run_root_reasons=_rows(report.reclaimable_run_root_reasons),
        protected_run_root_reasons=_rows(report.protected_run_root_reasons),
        confirm_token=token,
        next_actions=_next_actions(
            report, token=token, cutoff_days=cutoff_days, force=force
        ),
    )


def reclaim_workspace_cleanup_confirmed(
    tier: Literal["worktrees", "both"],
    disposition: Literal["archive", "delete"],
    confirm_token: str,
    older_than_days: int | None = None,
    force: bool = False,
) -> WorkspaceCleanupReceiptResult:
    """Re-verify ``confirm_token`` against the live selection, then reclaim.

    Raises :class:`~orcho_mcp.errors.WorkspaceCleanupConfirmationError` when
    the token does not match the selection that exists *now*, which covers
    both a token that was never issued and one issued against a workspace
    that has since changed.
    """
    cutoff_days = _resolve_older_than_days(older_than_days, force=force)
    report = _select(cutoff_days, force=force)
    expected = _confirm_token(report, cutoff_days=cutoff_days, force=force)
    if confirm_token != expected:
        raise WorkspaceCleanupConfirmationError(
            "cleanup confirm_token does not match the current selection. "
            "Call orcho_workspace_cleanup_report with the same "
            "older_than_days / force arguments, show the operator what it "
            "now selects, and pass that report's confirm_token. The token "
            "changes whenever the workspace changes, so a stale one means "
            "the selection is no longer the one that was reviewed."
        )
    receipt = reclaim_workspace_cleanup(
        tier=tier,
        disposition=disposition,
        runs_dir=report.runs_dir,
        cwd=None,
        older_than=timedelta(days=cutoff_days),
        force=force,
    )
    return WorkspaceCleanupReceiptResult(
        receipt_path=str(receipt.receipt_path),
        tier=receipt.tier,
        disposition=receipt.disposition,
        status=receipt.status,
        bytes_selected=receipt.bytes_selected,
        bytes_archived=receipt.bytes_archived,
        bytes_reclaimed=receipt.bytes_reclaimed,
        error_count=receipt.error_count,
        errors=list(receipt.errors),
        archive_paths=[str(path) for path in receipt.archive_paths],
    )


def _select(cutoff_days: int, *, force: bool) -> _SDKReport:
    """Run the engine's selection against the resolved runs directory."""
    return report_workspace_cleanup(
        runs_dir=runs_dir_or_raise(),
        cwd=None,
        older_than=timedelta(days=cutoff_days),
        force=force,
    )


def _resolve_older_than_days(older_than_days: int | None, *, force: bool) -> int:
    """Apply the engine's retention contract to the wire arguments.

    Mirrors the CLI: ``force`` overrides value protections, so it may only be
    used with an explicitly chosen cutoff — never silently against the
    default one.
    """
    if force and older_than_days is None:
        raise ValueError(
            "force=True requires an explicit older_than_days cutoff; "
            "forcing against the default retention window is not allowed"
        )
    resolved = _DEFAULT_OLDER_THAN_DAYS if older_than_days is None else int(older_than_days)
    if resolved <= 0:
        raise ValueError("older_than_days must be a positive number of days")
    return resolved


def _rows(
    summaries: tuple,
) -> list[WorkspaceCleanupReasonRow]:
    return [
        WorkspaceCleanupReasonRow(
            reason=summary.reason, count=summary.count, detail=summary.detail
        )
        for summary in summaries
    ]


def _confirm_token(report: _SDKReport, *, cutoff_days: int, force: bool) -> str:
    """Fingerprint one selection so a later reclaim can prove it is the same.

    Covers the arguments that shape the selection and every count and reason
    the report published. Anything that would change what reclaim removes —
    a run finishing, a checkout disappearing, another operator sweeping
    first — changes at least one of these, and therefore the token.
    """
    material = "\n".join(
        (
            str(report.runs_dir),
            f"older_than_days={cutoff_days}",
            f"force={force}",
            f"reclaimable={report.reclaimable_count}",
            f"protected={report.protected_count}",
            f"inert={report.inert_count}",
            f"root_reclaimable={report.reclaimable_run_root_count}",
            f"root_protected={report.protected_run_root_count}",
            _reason_material("reclaimable", report.reclaimable_reasons),
            _reason_material("protected", report.protected_reasons),
            _reason_material("inert", report.inert_reasons),
            _reason_material("root_reclaimable", report.reclaimable_run_root_reasons),
            _reason_material("root_protected", report.protected_run_root_reasons),
        )
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"{_TOKEN_PREFIX}-{digest[:_TOKEN_HEX_LEN]}"


def _reason_material(bucket: str, summaries: tuple) -> str:
    body = ",".join(
        f"{summary.reason}={summary.count}"
        for summary in sorted(summaries, key=lambda item: item.reason)
    )
    return f"{bucket}:[{body}]"


def _next_actions(
    report: _SDKReport,
    *,
    token: str,
    cutoff_days: int,
    force: bool,
) -> list[NextActionRecord]:
    """Point at reclaim only when there is something to reclaim.

    Always ``operator_input_required``: ``tier`` and ``disposition`` are the
    operator's call, and a checkout is not something a client should remove
    on its own reading of a report.
    """
    if report.reclaimable_count <= 0 and report.reclaimable_run_root_count <= 0:
        return []
    args: dict[str, object] = {
        "confirm_token": token,
        "older_than_days": cutoff_days,
    }
    if force:
        args["force"] = True
    return [
        NextActionRecord(
            intent=(
                f"Reclaim {report.reclaimable_count} retained checkout(s) "
                f"(and {report.reclaimable_run_root_count} run root(s) under "
                "the 'both' tier) after the operator picks how much to remove "
                "and whether to archive or delete."
            ),
            tool="orcho_workspace_cleanup_reclaim",
            args=args,
            optional=True,
            kind="operator_input_required",
            requires_operator_input=True,
            choices=["worktrees", "both"],
            input_schema={
                "tier": {
                    "type": "string",
                    "enum": ["worktrees", "both"],
                    "description": "How much to remove: retained checkouts "
                                   "only, or checkouts plus their run roots.",
                },
                "disposition": {
                    "type": "string",
                    "enum": ["archive", "delete"],
                    "description": "Whether removed content is archived first "
                                   "or deleted outright.",
                },
            },
        ),
    ]


__all__ = [
    "project_workspace_cleanup_report",
    "reclaim_workspace_cleanup_confirmed",
]
