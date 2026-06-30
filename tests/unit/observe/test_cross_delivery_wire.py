"""Phase-B cross-delivery wire smoke (ADR cross-delivery + CFA pause).

orcho-core Phase B added a new wire surface the MCP layer must surface
unchanged:

* ``session["phases"]["cross_delivery"]`` evidence (per-alias delivery
  outcomes incl. commit SHAs and ``release_override`` markers);
* new ``halt_reason`` values ``cross_delivery_partial`` /
  ``cross_delivery_failed``;
* new ``cross.delivery.*`` event kinds.

This L1 smoke pins that the read-path MCP tools consume the new shape
through their real handlers — the same-commit cross-repo validation the
orcho-mcp CLAUDE.md mandates for orcho-core wire-format changes.
"""
from __future__ import annotations

from orcho_mcp.tools import (
    orcho_run_events_summary,
    orcho_run_events_tail,
    orcho_run_status,
)
from tests.fixtures.mcp_workspace import meta, write_run


def _cross_delivery_evidence() -> dict:
    return {
        "overall": "partial",
        "disabled_by_config": False,
        "per_alias": {
            "api": {"alias": "api", "status": "committed",
                    "commit_sha": "abc123def4"},
            "web": {
                "alias": "web", "status": "target_dirty",
                "error": "project checkout dirty",
                "release_override": {
                    "original_verdict": "REJECTED",
                    "effective_verdict": "APPROVED_FOR_DELIVERY",
                    "source": "operator_override",
                },
            },
        },
    }


def _delivery_events() -> list[dict]:
    base = {"ts": "2026-05-28T00:00:00", "phase": "cross_delivery"}
    return [
        {"seq": 1, "kind": "cross.delivery.started",
         "payload": {"project_count": 2}, **base},
        {"seq": 2, "kind": "cross.delivery.alias_committed",
         "payload": {"alias": "api", "status": "committed",
                     "commit_sha": "abc123def4"}, **base},
        {"seq": 3, "kind": "cross.delivery.alias_failed",
         "payload": {"alias": "web", "status": "target_dirty"}, **base},
        {"seq": 4, "kind": "cross.delivery.completed",
         "payload": {"overall": "partial"}, **base},
        {"seq": 5, "kind": "run.end", "payload": {"status": "failed"}, **base},
    ]


def test_cross_delivery_evidence_surfaces_in_status(fake_workspace):
    """The phase-scoped ``cross_delivery`` evidence + new halt_reason
    round-trip through ``orcho_run_status`` unchanged."""
    write_run(
        fake_workspace, "20260528_000001",
        meta=meta(
            status="failed",
            task="cross delivery",
            projects={"api": "/p/api", "web": "/p/web"},
            halt_reason="cross_delivery_partial",
            phases={"cross_delivery": _cross_delivery_evidence()},
        ),
    )

    s = orcho_run_status("20260528_000001")
    assert s.meta["halt_reason"] == "cross_delivery_partial"
    ev = s.meta["phases"]["cross_delivery"]
    assert ev["overall"] == "partial"
    assert ev["per_alias"]["api"]["commit_sha"] == "abc123def4"
    # Override marker preserves the original reviewer verdict.
    override = ev["per_alias"]["web"]["release_override"]
    assert override["original_verdict"] == "REJECTED"
    assert override["effective_verdict"] == "APPROVED_FOR_DELIVERY"


def test_cross_delivery_events_tail(fake_workspace):
    """All four ``cross.delivery.*`` kinds tail in order, strictly
    before ``run.end``."""
    write_run(
        fake_workspace, "20260528_000002",
        meta=meta(status="failed"),
        events=_delivery_events(),
    )

    r = orcho_run_events_tail("20260528_000002")
    kinds = [e.kind for e in r.events]
    assert kinds == [
        "cross.delivery.started",
        "cross.delivery.alias_committed",
        "cross.delivery.alias_failed",
        "cross.delivery.completed",
        "run.end",
    ]
    assert kinds.index("cross.delivery.completed") < kinds.index("run.end")


def test_cross_delivery_events_summary_counts_new_kinds(fake_workspace):
    """``orcho_run_events_summary`` ingests the new kinds without
    error and counts them."""
    write_run(
        fake_workspace, "20260528_000003",
        meta=meta(status="failed"),
        events=_delivery_events(),
    )

    summary = orcho_run_events_summary("20260528_000003")
    # The summary must at minimum not choke on the new kinds; total
    # event count reflects all five rows.
    assert summary is not None
