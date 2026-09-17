"""L3 stdio: a general waiver never stands in for a required command's proof.

Consumer-side proof of the pinned orcho-core's ADR 0192 rule. A generic
``phase_handoff_waiver`` (a ``review_changes`` handoff the operator accepted
with a rationale) carries no gate command, so it excuses reviewer findings —
not the evidence that a required delivery command actually ran. This drives the
whole statement through a real ``python -m orcho_mcp`` subprocess:

* ``orcho_delivery_gate`` must not advertise ``approve`` / ``apply`` while a
  required command's receipt is missing / failed / stale, and its ``reason``
  must name that command and its status;
* the waiver's rationale stays visible on the ``errors`` evidence slice as the
  ``phase_handoff_waiver`` breadcrumb it really is — never relabelled into a
  ``verification_gate_waived`` record that was never written;
* reading the gate fabricates no receipt: the receipt directory is byte-for-byte
  the set the producer wrote, before and after the MCP calls.

The positive control is deliberately symmetric — same producer, same generic
waiver, only the proof is valid — so a green negative case cannot come from a
gate that refuses everything.

Every expectation about *which* commands are unproven is read from the pinned
core itself (``required_receipt_gaps`` / ``assess_delivery_verification``); this
module hardcodes only producer facts (the command names it declares, the waiver
text it writes, the handoff id it uses). Re-implementing the waiver policy here
would let MCP and core drift while the test stayed green.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("mcp.client.stdio")

import pipeline  # noqa: E402

from tests.fixtures.mcp_workspace import init_git_repo  # noqa: E402
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

_RUN_ID = "20260614_000001"
# A generic handoff waiver: a review round the operator accepted. It names no
# gate command, which is exactly why it cannot excuse a required command.
_WAIVER_HANDOFF_ID = "review_changes:1"
_WAIVER_PHASE = "review_changes"
_WAIVER_TEXT = (
    "Reviewer raised only stylistic objections; accepted for this round so the "
    "change can move on."
)
_WAIVER_DECIDED_BY = "operator"
# The two required delivery commands the producer's plugin declares.
_UNIT = "unit"
_LINT = "lint"

_PLUGIN_SOURCE = """\
PLUGIN = {
    "work_mode": "governed",
    "verification_envs": {"ci": {}},
    "verification": {
        "default_env": "ci",
        "delivery_policy": "require",
        "required": ["unit", "lint"],
        "commands": {
            "unit": {"run": ["python", "-c", "pass"]},
            "lint": {"run": ["python", "-c", "pass"]},
        },
    },
}
"""


@dataclass(frozen=True)
class _Parked:
    """The durable state one producer run left behind, plus its inputs."""

    workspace: Path
    repo: Path
    worktree: Path
    run_dir: Path
    head: str
    waiver: dict[str, Any]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _receipt_files(run_dir: Path) -> dict[str, str]:
    """Name → content for every written command receipt (the tamper baseline)."""
    d = run_dir / "verification_command_receipts"
    if not d.is_dir():
        return {}
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(d.iterdir())}


def _write_receipt(
    parked_paths: tuple[Path, Path, Path], command: str, exit_code: int,
) -> None:
    """Write one native command receipt through core's own writer."""
    from pipeline.evidence.verification_receipt import write_command_receipt
    from pipeline.verification_subject import capture_verification_subject

    run_dir, repo, worktree = parked_paths
    write_command_receipt(
        output_dir=run_dir,
        result={
            "command": command,
            "env": "ci",
            "cwd": str(worktree),
            "placeholders": {"checkout": str(worktree), "project": str(repo)},
            "argv": ["python", "-c", "pass"],
            "assertions": [],
            "exit_code": exit_code,
            "duration_s": 0.01,
            "parity": "absolute",
            "detail": "",
            "git": {
                "checkout_head": _git(worktree, "rev-parse", "HEAD"),
                "baseline_head": _git(repo, "rev-parse", "HEAD"),
            },
            "subject": capture_verification_subject(worktree),
            "dependencies": [],
        },
    )


def _produce_parked_run(workspace: Path, lint_proof: str) -> _Parked:
    """Drive a real producer to a parked delivery gate under a generic waiver.

    ``lint_proof`` selects how the second required command is proven:
    ``"valid"`` (a passing receipt), ``"missing"`` (no receipt), ``"failed"``
    (a non-zero exit code) or ``"stale"`` (a receipt whose subject the worktree
    moved past afterwards). ``unit`` is always proven.
    """
    from core.io.git_helpers import create_worktree
    from pipeline.engine.commit_delivery import resolve_commit_delivery
    from pipeline.engine.session import save_session
    from pipeline.project.handoff_waiver import WAIVER_KEY, apply_waiver_to_state

    repo = workspace / "project"
    init_git_repo(repo)
    plugin_dir = repo / ".orcho" / "multiagent"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.py").write_text(_PLUGIN_SOURCE, encoding="utf-8")
    (repo / "product.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "declare the verification contract")
    head = _git(repo, "rev-parse", "HEAD")

    run_dir = workspace / "runspace" / "runs" / _RUN_ID
    run_dir.mkdir(parents=True, exist_ok=True)
    worktree = run_dir / "checkout"
    created = create_worktree(
        repo=repo,
        base_ref=head,
        target_path=worktree,
        branch_name=f"orcho/run/{_RUN_ID}",
    )
    assert created.ok, created.error

    product = worktree / "product.py"
    product.write_text("VALUE = 2\n", encoding="utf-8")

    paths = (run_dir, repo, worktree)
    if lint_proof == "stale":
        # The receipt is written against the tree as it then stood; the next
        # edit moves the subject past it, which is what makes it stale.
        _write_receipt(paths, _LINT, 0)
        product.write_text("VALUE = 3\n", encoding="utf-8")
        _write_receipt(paths, _UNIT, 0)
    else:
        _write_receipt(paths, _UNIT, 0)
        if lint_proof == "failed":
            _write_receipt(paths, _LINT, 1)
        elif lint_proof == "valid":
            _write_receipt(paths, _LINT, 0)

    waiver = apply_waiver_to_state(
        SimpleNamespace(extras={}),
        handoff_id=_WAIVER_HANDOFF_ID,
        phase=_WAIVER_PHASE,
        waiver_text=_WAIVER_TEXT,
        decided_by=_WAIVER_DECIDED_BY,
    )

    session: dict[str, Any] = {
        "status": "done",
        "project": str(repo),
        "task": "prove the waiver does not substitute for a receipt",
        "phases": {
            "final_acceptance": {
                "verdict": "APPROVED",
                "short_summary": "the change reads as complete",
            },
        },
        WAIVER_KEY: waiver,
    }
    decision = resolve_commit_delivery(
        project_dir=repo,
        source_worktree=worktree,
        run_dir=run_dir,
        run_id=_RUN_ID,
        session=session,
        commit_config={
            "enabled": True,
            "add_untracked": True,
            "branch_policy": "bypass",
        },
        no_interactive=True,
        decision_mode="defer",
    )
    assert decision.status == "pending", decision.status

    session.update(
        status="halted",
        halt_reason="commit_delivery_pending",
        commit_delivery=decision.to_dict(),
    )
    save_session(run_dir, session)
    return _Parked(
        workspace=workspace,
        repo=repo,
        worktree=worktree,
        run_dir=run_dir,
        head=head,
        waiver=waiver,
    )


def _core_verdict(parked: _Parked) -> tuple[list[dict[str, Any]], Any]:
    """The pinned core's own answer: ``(required gaps, delivery assessment)``."""
    from pipeline.plugins import load_plugin
    from pipeline.project.handoff_waiver import WAIVER_KEY
    from pipeline.verification_contract import (
        VerificationContract,
        placeholder_context_for,
    )
    from pipeline.verification_delivery import assess_delivery_verification
    from pipeline.verification_readiness import required_receipt_gaps

    contract = VerificationContract.from_plugin(load_plugin(str(parked.repo)))
    assert contract is not None
    ctx = placeholder_context_for(
        contract,
        checkout=str(parked.worktree),
        project=str(parked.repo),
        workspace=str(parked.workspace),
        run_dir=str(parked.run_dir),
    )
    extras = {WAIVER_KEY: parked.waiver}
    gaps = required_receipt_gaps(contract, parked.run_dir, ctx, extras=extras)
    assessment = assess_delivery_verification(
        contract,
        parked.run_dir,
        ctx,
        extras,
        diff_cwd=str(parked.worktree),
        baseline_ref=parked.head,
    )
    return gaps, assessment


def _waiver_breadcrumbs(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in errors if e.get("kind") == "phase_handoff_waiver"]


@pytest.mark.anyio
@pytest.mark.parametrize("lint_proof", ["missing", "failed", "stale"])
async def test_stdio_generic_waiver_does_not_unlock_an_unproven_command(
    fake_workspace, lint_proof,
) -> None:
    from orcho_mcp.schemas import DeliveryGateProjection

    parked = _produce_parked_run(fake_workspace, lint_proof)
    receipts_before = _receipt_files(parked.run_dir)

    # The pinned core's verdict — the only source of what "unproven" means here.
    gaps, assessment = _core_verdict(parked)
    assert assessment is not None
    assert assessment.blocking is True
    assert assessment.waived_gates == ()
    unproven = {
        "missing": assessment.required_missing,
        "failed": assessment.required_failed,
        "stale": assessment.required_stale,
    }
    assert _LINT in unproven[lint_proof]
    assert gaps
    assert any(_LINT in json.dumps(gap) for gap in gaps)

    async with initialized_stdio_session(fake_workspace) as (session, _):
        gate_result = await session.call_tool(
            "orcho_delivery_gate", {"run_id": _RUN_ID},
        )
        errors_result = await session.call_tool(
            "orcho_run_evidence", {"run_id": _RUN_ID, "slice": "errors"},
        )
        delivery_result = await session.call_tool(
            "orcho_run_evidence", {"run_id": _RUN_ID, "slice": "delivery"},
        )
        live_result = await session.call_tool(
            "orcho_run_live_status", {"run_id": _RUN_ID},
        )

    # (a) The gate refuses to ship and says which command is unproven.
    assert gate_result.isError is False
    gate = DeliveryGateProjection.model_validate(gate_result.structuredContent)
    assert gate.kind == "delivery_decision_required"
    assert {"approve", "apply"} <= set(gate.blocked_actions)
    offered = {a.action for a in gate.available_actions}
    assert offered.isdisjoint({"approve", "apply"})
    assert gate.default_action not in ("approve", "apply")
    assert gate.reason is not None
    for status, commands in unproven.items():
        for command in commands:
            assert command in gate.reason
            assert status in gate.reason
    assert all(
        na.args.get("action") not in ("approve", "apply")
        for na in gate.next_actions
        if na.kind == "ready_call"
    )

    # (b) The rationale stays the waiver it was — not a verification excuse.
    assert errors_result.isError is False
    errors = errors_result.structuredContent["errors"]["errors"]
    [breadcrumb] = _waiver_breadcrumbs(errors)
    assert breadcrumb["waiver_text"] == _WAIVER_TEXT
    assert breadcrumb["handoff_id"] == _WAIVER_HANDOFF_ID
    assert breadcrumb["decided_by"] == _WAIVER_DECIDED_BY
    assert not [e for e in errors if e.get("kind") == "verification_gate_waived"]

    # (c) Nothing shipped, and the live card routes to the gate.
    assert delivery_result.isError is False
    delivery = delivery_result.structuredContent["delivery"]
    assert delivery["committed"] is False
    assert delivery["applied"] is False
    assert delivery["decision_status"] == "pending"

    assert live_result.isError is False
    card = live_result.structuredContent
    assert card["terminal"]["delivery_committed"] is False
    assert card["terminal"]["delivery_published"] is False
    assert "orcho_delivery_gate" in (card["next_action"] or "")

    # (d) Reading the gate invented no proof.
    assert _receipt_files(parked.run_dir) == receipts_before
    if lint_proof == "missing":
        assert f"{_LINT}.json" not in receipts_before
    if lint_proof == "failed":
        lint_receipt = next(
            json.loads(body)
            for name, body in receipts_before.items()
            if json.loads(body).get("command") == _LINT
        )
        assert lint_receipt["exit_code"] == 1


@pytest.mark.anyio
async def test_stdio_valid_proof_under_the_same_waiver_offers_approve(
    fake_workspace,
) -> None:
    """The symmetric control: only the proof changes, and approve returns."""
    from orcho_mcp.schemas import DeliveryGateProjection

    parked = _produce_parked_run(fake_workspace, "valid")

    gaps, assessment = _core_verdict(parked)
    assert gaps == []
    assert assessment is not None
    assert assessment.blocking is False

    async with initialized_stdio_session(fake_workspace) as (session, _):
        gate_result = await session.call_tool(
            "orcho_delivery_gate", {"run_id": _RUN_ID},
        )
        errors_result = await session.call_tool(
            "orcho_run_evidence", {"run_id": _RUN_ID, "slice": "errors"},
        )

    assert gate_result.isError is False
    gate = DeliveryGateProjection.model_validate(gate_result.structuredContent)
    assert gate.kind == "delivery_decision_required"
    assert "approve" in {a.action for a in gate.available_actions}
    assert gate.reason is None

    # The waiver is unchanged and still visible — it was never the blocker.
    assert errors_result.isError is False
    errors = errors_result.structuredContent["errors"]["errors"]
    [breadcrumb] = _waiver_breadcrumbs(errors)
    assert breadcrumb["waiver_text"] == _WAIVER_TEXT
    assert breadcrumb["handoff_id"] == _WAIVER_HANDOFF_ID
