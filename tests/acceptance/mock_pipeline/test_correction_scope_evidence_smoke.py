"""Fixture-driven mock-smoke: delivery + correction + scope + advisory evidence.

Builds ONE synthetic durable run directory carrying every new
``orcho_run_evidence`` fact at once — a ``commit_delivery`` decision, a
``correction_fixed_point`` non-convergence block, a
``final_acceptance.scope_expansion`` audit, explicit ``validate_plan`` findings
forwarded into a whole-plan implement (so the advisory rule is really
exercised), and a ``parsed_plan.json`` with ``allowed_modifications`` — then
reads it through the real MCP wire ``orcho_run_evidence(slice="all")`` in-process
and asserts the shape of all new slices.

Fixture-driven (not a live pipeline run): no mock profile parks a run at a
delivery gate AND records a correction fixed point AND a scope-expansion audit
in one run, so a synthetic run dir is the only way to pin every new slice's
wire shape together. The projections themselves run through the real
``inspect_run_evidence`` path — no SDK monkeypatch.

Marked ``mcp_integration`` so the default fast suite skips it. Run with::

    python -m pytest -q -m mcp_integration \
        tests/acceptance/mock_pipeline/test_correction_scope_evidence_smoke.py
"""
from __future__ import annotations

import pytest

from tests.fixtures.mcp_workspace import meta, write_run

pytestmark = pytest.mark.mcp_integration

_RUN_ID = "20260401_000001"


def _fixture_meta() -> dict:
    """Meta carrying delivery + correction + scope + advisory facts at once."""
    return meta(
        status="halted",
        halt_reason="correction_not_converging",
        commit_delivery={
            "status": "committed",
            "action": "approve",
            "release_verdict": "approved",
            "commit_sha": "cafe123",
        },
        correction_fixed_point={
            "repeated": ["blocker-a", "blocker-b"],
            "parent_run_id": "20260401_000000",
            "child_run_id": _RUN_ID,
            "suggested_actions": [
                "Inspect the recurring blockers manually.",
                "Stop the correction loop.",
            ],
            "reason": "child repeated the parent's release blockers",
        },
        phases={
            # Advisory rule inputs: validate_plan findings forwarded into a
            # successful whole-plan implement (output present, no subtask DAG,
            # no guardrail/failure) → those findings are advisory.
            "validate_plan": [
                {"attempt": 1, "findings": [
                    {"id": "VP1", "severity": "P1", "title": "plan gap",
                     "body": "b"},
                ]},
            ],
            "review_changes": [
                {"attempt": 1, "findings": [
                    {"id": "R1", "severity": "P2", "title": "review note",
                     "body": "b"},
                ]},
            ],
            "implement": {"output": "delivered whole plan", "meta": {}},
            "final_acceptance": {
                # Persisted in the real core ``ScopeExpansionAssessment.to_dict()``
                # shape: ``status`` is the ENUM VALUE (``scope_expansion_*``) plus
                # a ``counts`` summary — pins that the projector normalises the
                # prefixed values onto the bare wire vocabulary on a core-shaped
                # artifact, not just hand-built short tokens.
                "scope_expansion": {
                    "items": [
                        {"path": "package-lock.json",
                         "status": "scope_expansion_notice",
                         "category": "build", "evidence": ["verified"]},
                        {"path": "src/util/helper.py",
                         "status": "scope_expansion_risk",
                         "category": "other", "evidence": ["no green gate"]},
                        {"path": "src/core/engine.py",
                         "status": "scope_expansion_blocker",
                         "category": "public_wire",
                         "evidence": ["unaligned public wire change"]},
                    ],
                    "has_blocker": True,
                    "counts": {"notice": 1, "risk": 1, "blocker": 1},
                },
            },
        },
    )


def _parsed_plan() -> dict:
    return {
        "short_summary": "smoke plan",
        "planning_context": "pc",
        "tasks": [],
        "allowed_modifications": ["docs/**", "src/util/*.py"],
    }


def test_evidence_all_carries_delivery_correction_scope_advisory(
    fake_workspace,
) -> None:
    from orcho_mcp.tools import orcho_run_evidence

    write_run(
        fake_workspace, _RUN_ID,
        meta=_fixture_meta(),
        parsed_plan=_parsed_plan(),
    )

    result = orcho_run_evidence(_RUN_ID, slice="all")

    assert result.run_id == _RUN_ID
    assert result.slice == "all"

    # ── delivery ────────────────────────────────────────────────────────────
    d = result.delivery
    assert d is not None
    assert d.release_verdict == "approved"
    assert d.decision_status == "committed"
    assert d.applied is True
    assert d.committed is True
    assert d.commit_sha == "cafe123"
    assert d.skipped is False
    assert d.failed is False

    # ── correction ──────────────────────────────────────────────────────────
    c = result.correction
    assert c is not None
    assert c.non_converging is True
    assert c.repeated == ["blocker-a", "blocker-b"]
    assert c.parent_run_id == "20260401_000000"
    assert c.child_run_id == _RUN_ID
    assert c.suggested_actions  # operator-decision hints present
    assert c.reason == "child repeated the parent's release blockers"

    # ── scope_expansion ─────────────────────────────────────────────────────
    se = result.scope_expansion
    assert se is not None
    assert se.has_blocker is True
    by_path = {i.path: i for i in se.items}
    assert by_path["package-lock.json"].classification == "notice"
    assert by_path["src/util/helper.py"].classification == "risk"
    assert by_path["src/core/engine.py"].classification == "blocker"
    # Core's prefixed enum values are normalised — none leak to the wire.
    assert all(
        not i.classification.startswith("scope_expansion_") for i in se.items
    )

    # ── advisory findings (really exercised) ─────────────────────────────────
    assert result.findings is not None
    by_id = {f.id: f for f in result.findings}
    assert by_id["VP1"].advisory is True
    assert by_id["R1"].advisory is False
    # Advisory findings are excluded from the active release-blocker set.
    active_ids = {f.id for f in result.findings if not f.advisory}
    assert "VP1" not in active_ids
    assert "R1" in active_ids

    # ── plan.allowed_modifications ───────────────────────────────────────────
    assert result.plan is not None
    assert result.plan.allowed_modifications == ["docs/**", "src/util/*.py"]
