"""L3 stdio: the persisted latest-review context reaches an MCP client intact.

The pinned orcho-core resolves, at ``final_acceptance`` time, *which* review
attempt still applies — and persists that answer under
``meta['phases']['final_acceptance']['review_context']``, alongside the
per-attempt ``review`` / ``reverify`` sub-records inside each round. This module
proves the consumer half of that statement: a **fresh** ``python -m orcho_mcp``
subprocess, which never saw the producing process, hands the operator exactly
those durable facts — attempt identity, supersession, the attempt that never
parsed, the repair's standing (claim vs. already-reviewed), and the operator's
waiver — and invents nothing on the way.

Three producer scenarios drive real core writers (``ReviewRoundAdapter``,
``RoundAdapter``, ``FinalAcceptanceAdapter``, ``apply_waiver_to_state``,
``save_session``); no ``meta.json`` is assembled by hand:

* **A** — review REJECTED → repair → re-verify APPROVED, then a second round
  whose review never parsed, under an operator waiver of one finding.
* **B** — review REJECTED, repaired, never re-reviewed: the repair stays a
  claim and the findings stay unresolved.
* **C** — a critique-only round: no review attempt, so there is no context, and
  the key is *absent* rather than null or empty.

What the resolver is *supposed* to answer is never re-implemented here: every
expectation about latest / superseded / invalid / repair standing / operator
records is read back from the pinned core's own
``resolve_final_review_context`` run on a fresh, session-less
:class:`PipelineState`, and the findings slice is compared against
``sdk.evidence_slices.list_findings``. Only producer facts (verdicts, finding
ids, the waiver text) are hardcoded. A duplicated rule here would let core and
MCP drift while this module stayed green.

The no-fabrication guard is byte-level: ``meta.json`` and the
``phase_handoff_decisions/`` directory are identical before and after the MCP
reads, and the serialized context carries no loader provenance.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("mcp.client.stdio")

import pipeline  # noqa: E402

from tests.fixtures.mcp_workspace import in_workspace_project  # noqa: E402
from tests.fixtures.stdio import initialized_stdio_session  # noqa: E402

# Provenance guard, mirroring the env-provenance gate: this proof is only
# meaningful against the orcho-core checkout under review. An installed copy
# would let the module pass while proving nothing about the pinned core.
assert "site-packages" not in pipeline.__file__, (
    f"orcho-core resolved to an installed copy: {pipeline.__file__}"
)
assert "dist-packages" not in pipeline.__file__, (
    f"orcho-core resolved to an installed copy: {pipeline.__file__}"
)

_TASK = "prove the latest review context survives a fresh process"

# The operator's rationale for accepting F1 in round 1. The waiver names the
# finding, which is the only reason the resolver can report its identity key.
_WAIVER_HANDOFF_ID = "review_changes:1"
_WAIVER_PHASE = "review_changes"
_WAIVER_TEXT = (
    "F1 is a pre-existing shape we accept for this round; the fix belongs to "
    "the follow-up that owns that module."
)
_WAIVER_DECIDED_BY = "operator"
_WAIVER_DECIDED_AT = "2026-09-16T01:26:55+00:00"

_PARSE_ERROR = "review parse error: expected a JSON object, found prose"


def _finding(fid: str, **over: Any) -> dict[str, Any]:
    """One reviewer finding, body prose included.

    The body is what the resolver's compact projection must drop: it is the
    single largest field a reviewer emits and has no place in the closing
    gate's evidence block.
    """
    return {
        "id":           fid,
        "severity":     "P1",
        "title":        f"unguarded path in handler {fid}",
        "body": (
            "Long reviewer prose that explains the failure mode at length and "
            "must never reach the resolved review context, only the compact "
            "projection of it."
        ),
        "required_fix": "guard the path before dispatch",
        "file":         "src/orcho_mcp/tools.py",
        "line":         120,
        **over,
    }


def _review_log(
    *,
    verdict: str,
    approved: bool,
    findings: list[dict[str, Any]],
    short_summary: str,
) -> dict[str, Any]:
    """The ``phase_log['review_changes']`` entry a parsed review leaves."""
    return {
        "output":        f"## Review\n\n{short_summary}",
        "clean":         approved,
        "approved":      approved,
        "verdict":       verdict,
        "critique":      "" if approved else "fix the findings above",
        "short_summary": short_summary,
        "findings":      findings,
    }


def _parse_error_log() -> dict[str, Any]:
    """The entry the review handler writes when the contract never parsed.

    Mirrors ``pipeline/phases/builtin/handlers/review_changes.py``: a verdict
    key is present (REJECTED) *and* so is ``parse_error`` — which is exactly
    what makes the attempt recorded-but-unusable.
    """
    body = f"{_PARSE_ERROR}\n\nRaw output:\nlooks good to me"
    return {
        "output":      body,
        "clean":       False,
        "approved":    False,
        "verdict":     "REJECTED",
        "critique":    body,
        "parse_error": _PARSE_ERROR,
    }


def _repair_receipt() -> dict[str, Any]:
    """A repair receipt built by core's own type, not a hand-rolled dict."""
    from pipeline.repair_protocol import (
        ReceiptItem,
        RepairReceipt,
        repair_receipt_to_dict,
    )

    return repair_receipt_to_dict(RepairReceipt(
        source_phase="review_changes",
        source_round=1,
        repair_phase="repair_changes",
        repair_round=1,
        fixed=(ReceiptItem(
            finding_id="F2",
            summary="added the missing guard",
            refs=("src/orcho_mcp/tools.py:120",),
        ),),
        still_open=(ReceiptItem(
            finding_id="F1",
            summary="left as-is; the operator accepted it",
        ),),
    ))


@dataclass(frozen=True)
class _Produced:
    """What one producer run left on disk, plus the inputs it used."""

    workspace: Path
    run_id: str
    run_dir: Path
    project: str
    meta: dict[str, Any]
    context: dict[str, Any] | None


# ── Producer: real core writers only ─────────────────────────────────────────

def _new_state(project: str, run_dir: Path, run_id: str):
    """A ``PipelineState`` whose only session source is ``run_dir/meta.json``.

    ``lifecycle_ctx`` is deliberately left unset: the resolver then finds no
    live session on the context and falls through to the durable read, which
    is the fresh-process path this module is about.
    """
    from pipeline.plugins import PluginConfig
    from pipeline.runtime import PipelineState

    state = PipelineState(task=_TASK, project_dir=project, plugin=PluginConfig())
    state.output_dir = run_dir
    state.extras["run_id"] = run_id
    return state


def _write_attempt(state, session: dict, *, round_n: int, log: dict, reverify: bool = False):
    """Record one review attempt through ``ReviewRoundAdapter``.

    ``reverify`` raises the runner-owned flag the adapter reads to label the
    post-repair pass — the same signal the loop runner sets before it
    re-dispatches ``review_changes``.
    """
    from pipeline.review_round_record import REVERIFY_FLAG, ReviewRoundAdapter

    state.phase_log["review_changes"] = log
    if reverify:
        state.extras[REVERIFY_FLAG] = True
    try:
        ReviewRoundAdapter().write("review_changes", state, session, round_n=round_n)
    finally:
        state.extras.pop(REVERIFY_FLAG, None)


def _close_round(state, session: dict, *, round_n: int, pending: dict) -> None:
    """Close the round through ``RoundAdapter``, filling its provisional entry."""
    from pipeline.session_adapters import RoundAdapter

    state.phase_log["rounds_pending"] = pending
    try:
        RoundAdapter().write("rounds", state, session, round_n=round_n)
    finally:
        state.phase_log.pop("rounds_pending", None)


def _finalize(
    state, session: dict, run_dir: Path, *, findings: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Persist the rounds, resolve the review context, write final acceptance.

    The first ``save_session`` matters: the resolver runs against the state
    *after* the durable session exists, so it reads ``meta.json`` — never an
    in-memory shortcut the fresh reader would not have.
    """
    from pipeline.engine.session import save_session
    from pipeline.phases.builtin.final_review_context import (
        resolve_final_review_context,
    )
    from pipeline.session_adapters import FinalAcceptanceAdapter

    save_session(run_dir, session)
    ctx = resolve_final_review_context(state)

    log: dict[str, Any] = {
        "output":        "## Final Acceptance\n\nthe change reads as complete",
        "verdict":       "APPROVED",
        "approved":      True,
        "short_summary": "the change reads as complete",
    }
    if findings is not None:
        log["findings"] = findings
    if ctx is not None:
        log["review_context"] = ctx.to_dict()
    state.phase_log["final_acceptance"] = log
    FinalAcceptanceAdapter().write("final_acceptance", state, session)
    save_session(run_dir, session)
    return ctx.to_dict() if ctx is not None else None


def _start(workspace: Path, run_id: str) -> tuple[str, Path, dict[str, Any]]:
    project = in_workspace_project(workspace)
    run_dir = workspace / "runspace" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    session: dict[str, Any] = {"status": "done", "project": project, "task": _TASK}
    return project, run_dir, session


def _produced(
    workspace: Path, run_id: str, run_dir: Path, project: str, ctx: dict | None,
) -> _Produced:
    return _Produced(
        workspace=workspace,
        run_id=run_id,
        run_dir=run_dir,
        project=project,
        meta=json.loads((run_dir / "meta.json").read_text(encoding="utf-8")),
        context=ctx,
    )


_FA_FINDING = _finding("FA1", severity="P3", title="non-blocking note")


def _produce_reverified(workspace: Path, run_id: str) -> _Produced:
    """(A) review → repair → re-verify, then a round whose review never parsed."""
    from pipeline.project.handoff_waiver import WAIVER_KEY, apply_waiver_to_state

    project, run_dir, session = _start(workspace, run_id)
    state = _new_state(project, run_dir, run_id)

    # The waiver names the finding it accepts — without ``findings`` the
    # resolver has no identity to report, so this is the load-bearing argument.
    session[WAIVER_KEY] = apply_waiver_to_state(
        SimpleNamespace(extras={}),
        handoff_id=_WAIVER_HANDOFF_ID,
        phase=_WAIVER_PHASE,
        waiver_text=_WAIVER_TEXT,
        decided_by=_WAIVER_DECIDED_BY,
        findings=[_finding("F1")],
        decided_at=_WAIVER_DECIDED_AT,
    )

    _write_attempt(state, session, round_n=1, log=_review_log(
        verdict="REJECTED", approved=False,
        findings=[_finding("F1"), _finding("F2")],
        short_summary="two blockers remain",
    ))
    _write_attempt(state, session, round_n=1, reverify=True, log=_review_log(
        verdict="APPROVED", approved=True, findings=[],
        short_summary="the repair holds",
    ))
    _close_round(state, session, round_n=1, pending={
        "critique":       "fix the findings above",
        "repair_output":  "## Repair\n\nguarded the path",
        "repair_receipt": _repair_receipt(),
    })

    _write_attempt(state, session, round_n=2, log=_parse_error_log())
    _close_round(state, session, round_n=2, pending={"critique": _PARSE_ERROR})

    ctx = _finalize(state, session, run_dir, findings=[_FA_FINDING])
    return _produced(workspace, run_id, run_dir, project, ctx)


def _produce_repaired_not_rereviewed(workspace: Path, run_id: str) -> _Produced:
    """(B) one REJECTED review, repaired, never reviewed again."""
    project, run_dir, session = _start(workspace, run_id)
    state = _new_state(project, run_dir, run_id)

    _write_attempt(state, session, round_n=1, log=_review_log(
        verdict="REJECTED", approved=False, findings=[_finding("F1")],
        short_summary="one blocker remains",
    ))
    _close_round(state, session, round_n=1, pending={
        "critique":       "fix the finding above",
        "repair_output":  "## Repair\n\nclaims the guard is in place",
        "repair_receipt": _repair_receipt(),
    })

    ctx = _finalize(state, session, run_dir)
    return _produced(workspace, run_id, run_dir, project, ctx)


def _produce_critique_only(workspace: Path, run_id: str) -> _Produced:
    """(C) a round with a critique and no review attempt at all."""
    project, run_dir, session = _start(workspace, run_id)
    state = _new_state(project, run_dir, run_id)
    _close_round(state, session, round_n=1, pending={"critique": "nothing to fix"})
    ctx = _finalize(state, session, run_dir)
    return _produced(workspace, run_id, run_dir, project, ctx)


# ── Fresh-process readers ────────────────────────────────────────────────────

def _resolve_fresh(produced: _Produced) -> dict[str, Any] | None:
    """The pinned core's own answer, re-derived from ``meta.json`` alone."""
    from pipeline.phases.builtin.final_review_context import (
        resolve_final_review_context,
    )

    state = _new_state(produced.project, produced.run_dir, produced.run_id)
    ctx = resolve_final_review_context(state)
    return ctx.to_dict() if ctx is not None else None


def _core_findings(produced: _Produced) -> list[tuple[str, int, str, str]]:
    """``sdk.list_findings`` projected to the identity the wire also carries."""
    from sdk.evidence_slices import list_findings

    return [
        (f.phase, f.attempt, f.id, f.severity)
        for f in list_findings(
            produced.run_id,
            runs_dir=produced.workspace / "runspace" / "runs",
            cwd=None,
        )
    ]


def _durable_snapshot(run_dir: Path) -> tuple[bytes, dict[str, bytes]]:
    """``meta.json`` bytes + the handoff-decision artifacts, as written."""
    decisions = run_dir / "phase_handoff_decisions"
    files = (
        {p.name: p.read_bytes() for p in sorted(decisions.iterdir())}
        if decisions.is_dir() else {}
    )
    return (run_dir / "meta.json").read_bytes(), files


@pytest.fixture
def anyio_backend():
    # Pin to asyncio — pytest-anyio defaults try trio too, which we don't ship.
    return "asyncio"


# ── A: review → repair → re-verify, plus an unparseable attempt ──────────────

@pytest.mark.anyio
async def test_stdio_exposes_the_latest_valid_review_after_a_reverify(fake_workspace):
    run_id = "20260916_000401"
    produced = _produce_reverified(fake_workspace, run_id)
    before = _durable_snapshot(produced.run_dir)

    async with initialized_stdio_session(fake_workspace) as (session, _):
        status = await session.call_tool("orcho_run_status", {"run_id": run_id})
        status_all = await session.call_tool(
            "orcho_run_status", {"run_id": run_id, "include": ["all"]},
        )
        findings = await session.call_tool(
            "orcho_run_evidence", {"run_id": run_id, "slice": "findings"},
        )
        errors = await session.call_tool(
            "orcho_run_evidence", {"run_id": run_id, "slice": "errors"},
        )

    for result in (status, status_all, findings, errors):
        assert result.isError is False

    phases = status.structuredContent["meta"]["phases"]
    rounds = phases["rounds"]
    durable_rounds = produced.meta["phases"]["rounds"]

    # (a) Per-attempt identity survives the summary projection verbatim.
    for index, passes in ((0, ("review", "reverify")), (1, ("review",))):
        for pass_kind in passes:
            wire = rounds[index][pass_kind]
            assert wire == durable_rounds[index][pass_kind]
            for key in ("pass", "attempt", "verdict", "approved", "repair_preceded"):
                assert key in wire, f"rounds[{index}].{pass_kind} lost {key}"
    assert (rounds[0]["review"]["pass"], rounds[0]["review"]["attempt"]) == ("review", 1)
    assert rounds[0]["review"]["repair_preceded"] is False
    assert rounds[0]["reverify"]["repair_preceded"] is True
    assert "reverify" not in rounds[1]
    assert rounds[1]["review"]["parse_error"] == _PARSE_ERROR
    # The summary contract still holds around the sub-records.
    assert all("critique" not in r and "critique_chars" in r for r in rounds)

    # (b) The context on the wire is the persisted one, and the persisted one
    # is what the pinned core resolves from meta.json alone.
    final_acceptance = phases["final_acceptance"]
    ctx = final_acceptance["review_context"]
    durable_ctx = produced.meta["phases"]["final_acceptance"]["review_context"]
    assert ctx == durable_ctx == _resolve_fresh(produced)

    assert (ctx["latest"]["round"], ctx["latest"]["pass"]) == (1, "reverify")
    assert ctx["latest"]["approved"] is True
    assert [(a["round"], a["pass"]) for a in ctx["superseded"]] == [(1, "review")]
    assert [(a["round"], a["pass"]) for a in ctx["invalid"]] == [(2, "review")]
    assert ctx["invalid"][0]["parse_error"] == _PARSE_ERROR
    assert ctx["unresolved_findings"] == []
    # The repair was re-reviewed, so it is context — not an open claim.
    assert ctx["repair_before_latest"] == durable_rounds[0]["repair_receipt"]
    assert "repair_claim" not in ctx

    # (c) The operator's waiver, with the finding identity it actually named.
    [operator] = ctx["operator"]
    assert operator["kind"] == "waiver"
    assert operator["handoff_id"] == _WAIVER_HANDOFF_ID
    assert operator["phase"] == _WAIVER_PHASE
    assert operator["text"] == _WAIVER_TEXT
    assert operator["decided_at"] == _WAIVER_DECIDED_AT
    assert operator["waived_finding_ids"] == ["id:F1"]

    # (d) Reviewer prose stays out of the context; loader provenance never
    # enters it.
    for attempt in (ctx["latest"], *ctx["superseded"], *ctx["invalid"]):
        assert all("body" not in f for f in attempt["findings"])
    assert [f["id"] for f in ctx["superseded"][0]["findings"]] == ["F1", "F2"]
    serialized = json.dumps(ctx)
    for leak in ("meta.json", "lifecycle", "run_config", "loaded_from"):
        assert leak not in serialized

    # (e) ``include=["all"]`` is identity over the durable phases.
    assert status_all.structuredContent["meta"]["phases"] == produced.meta["phases"]

    # (f) Round sub-record findings are not synthesised into the evidence
    # slice: it is exactly what the pinned core's own reader returns.
    wire_findings = [
        (f["phase"], f["attempt"], f["id"], f["severity"])
        for f in findings.structuredContent["findings"]
    ]
    assert wire_findings == _core_findings(produced)
    assert wire_findings == [("final_acceptance", 1, "FA1", "P3")]
    assert not [f for f in wire_findings if f[0] == "review_changes"]

    # (g) The waiver stays visible as the breadcrumb it is.
    breadcrumbs = [
        e for e in errors.structuredContent["errors"]["errors"]
        if e.get("kind") == "phase_handoff_waiver"
    ]
    assert [b["handoff_id"] for b in breadcrumbs] == [operator["handoff_id"]]

    # (h) Reading fabricated nothing.
    assert _durable_snapshot(produced.run_dir) == before


# ── B: repaired, never re-reviewed ───────────────────────────────────────────

@pytest.mark.anyio
async def test_stdio_keeps_an_unreviewed_repair_a_claim(fake_workspace):
    run_id = "20260916_000402"
    produced = _produce_repaired_not_rereviewed(fake_workspace, run_id)
    before = _durable_snapshot(produced.run_dir)

    async with initialized_stdio_session(fake_workspace) as (session, _):
        status = await session.call_tool("orcho_run_status", {"run_id": run_id})
        findings = await session.call_tool(
            "orcho_run_evidence", {"run_id": run_id, "slice": "findings"},
        )

    assert status.isError is False
    assert findings.isError is False

    phases = status.structuredContent["meta"]["phases"]
    ctx = phases["final_acceptance"]["review_context"]
    assert ctx == produced.meta["phases"]["final_acceptance"]["review_context"]
    assert ctx == _resolve_fresh(produced)

    assert (ctx["latest"]["round"], ctx["latest"]["pass"]) == (1, "review")
    assert ctx["latest"]["verdict"] == "REJECTED"
    assert ctx["latest"]["approved"] is False
    assert ctx["superseded"] == []
    assert ctx["invalid"] == []
    assert ctx["operator"] == []

    # The repair is the repairer's claim; it closed nothing, and the finding
    # it was written against is still unresolved — projected, not quoted.
    assert ctx["repair_claim"] == produced.meta["phases"]["rounds"][0]["repair_receipt"]
    assert "repair_before_latest" not in ctx
    assert ctx["unresolved_findings"] == [{
        k: v for k, v in _finding("F1").items() if k != "body"
    }]

    wire_findings = [
        (f["phase"], f["attempt"], f["id"], f["severity"])
        for f in findings.structuredContent["findings"]
    ]
    assert wire_findings == _core_findings(produced) == []
    assert _durable_snapshot(produced.run_dir) == before


# ── C: no review attempt at all ──────────────────────────────────────────────

@pytest.mark.anyio
async def test_stdio_omits_the_context_when_no_review_ran(fake_workspace):
    run_id = "20260916_000403"
    produced = _produce_critique_only(fake_workspace, run_id)
    before = _durable_snapshot(produced.run_dir)

    assert produced.context is None
    assert _resolve_fresh(produced) is None

    async with initialized_stdio_session(fake_workspace) as (session, _):
        status = await session.call_tool("orcho_run_status", {"run_id": run_id})

    assert status.isError is False
    phases = status.structuredContent["meta"]["phases"]

    # Absent, not null and not empty: there is nothing durable to say.
    assert "review_context" not in phases["final_acceptance"]
    assert "review_context" not in produced.meta["phases"]["final_acceptance"]
    assert "review" not in phases["rounds"][0]
    assert "reverify" not in phases["rounds"][0]
    # The review adapter writes no phase entry of its own, ever.
    assert "review_changes" not in phases
    assert "review_changes" not in produced.meta["phases"]

    assert _durable_snapshot(produced.run_dir) == before
