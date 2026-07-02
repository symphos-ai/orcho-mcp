"""Unit tests for the ``orcho_run_evidence(slice="scope_expansion")`` slice.

Pins the MCP projection layer for the ADR 0110 scope-expansion audit that
``final_acceptance`` records in ``meta['phases']['final_acceptance']
['scope_expansion']``. The projector reads durable meta via
``services.run_artifacts.get_run_meta_raw`` (exercised here through a real
synthetic run dir under ``fake_workspace``), so these tests cover the
wire-record mapping and the defensive empty-slice behaviour only.

Product semantics under test: a ``notice`` is informational — it forms no
operator handoff / next_action (the slice carries no such field); a
``blocker`` is reflected as the decision condition via ``has_blocker``.
"""
from __future__ import annotations

from orcho_mcp.inspection.evidence import inspect_run_evidence
from tests.fixtures.mcp_workspace import meta, write_run


def _scope_expansion_meta() -> dict:
    """Meta whose final_acceptance recorded notice + risk + blocker items."""
    return meta(
        status="rejected",
        phases={
            "final_acceptance": {
                "scope_expansion": {
                    "items": [
                        {
                            "path": "docs/notes.md",
                            "status": "notice",
                            "category": "docs",
                            "evidence": ["touched outside plan surface"],
                        },
                        {
                            "path": "src/util/helper.py",
                            "status": "risk",
                            "category": "refactor",
                            "evidence": ["adjacent edit", "not in owned_files"],
                        },
                        {
                            "path": "src/core/engine.py",
                            "status": "blocker",
                            "category": "behavioral",
                            "evidence": ["out-of-scope behaviour change"],
                        },
                    ],
                    "has_blocker": True,
                },
            },
        },
    )


def test_scope_expansion_slice_projects_items(fake_workspace) -> None:
    write_run(
        fake_workspace, "20260101_000101",
        meta=_scope_expansion_meta(),
    )

    result = inspect_run_evidence("20260101_000101", slice="scope_expansion")

    assert result.slice == "scope_expansion"
    assert result.scope_expansion is not None
    se = result.scope_expansion
    assert se.has_blocker is True

    by_path = {i.path: i for i in se.items}
    assert set(by_path) == {
        "docs/notes.md", "src/util/helper.py", "src/core/engine.py",
    }

    notice = by_path["docs/notes.md"]
    assert notice.classification == "notice"
    assert notice.category == "docs"
    assert notice.evidence == ["touched outside plan surface"]

    risk = by_path["src/util/helper.py"]
    assert risk.classification == "risk"
    assert risk.evidence == ["adjacent edit", "not in owned_files"]

    blocker = by_path["src/core/engine.py"]
    assert blocker.classification == "blocker"
    assert blocker.category == "behavioral"


def test_scope_expansion_notice_forms_no_handoff(fake_workspace) -> None:
    """A notice-only audit is informational — the slice exposes no operator
    handoff / next_action field, and ``has_blocker`` stays False."""
    write_run(
        fake_workspace, "20260101_000102",
        meta=meta(
            status="done",
            phases={
                "final_acceptance": {
                    "scope_expansion": {
                        "items": [
                            {
                                "path": "README.md",
                                "status": "notice",
                                "category": None,
                                "evidence": [],
                            },
                        ],
                        "has_blocker": False,
                    },
                },
            },
        ),
    )

    result = inspect_run_evidence("20260101_000102", slice="scope_expansion")

    se = result.scope_expansion
    assert se is not None
    assert se.has_blocker is False
    assert [i.classification for i in se.items] == ["notice"]
    # The scope-expansion slice is data-only: it has no next_actions/handoff
    # surface that a notice could populate.
    assert not hasattr(se, "next_actions")


def test_scope_expansion_absent_yields_empty_slice(fake_workspace) -> None:
    """A run whose meta recorded no scope-expansion audit → clean empty slice."""
    write_run(
        fake_workspace, "20260101_000103",
        meta=meta(status="done"),
    )

    result = inspect_run_evidence("20260101_000103", slice="scope_expansion")

    se = result.scope_expansion
    assert se is not None
    assert se.items == []
    assert se.has_blocker is False


def test_scope_expansion_malformed_items_are_skipped(fake_workspace) -> None:
    """Non-dict / path-less items are dropped defensively, never raised."""
    write_run(
        fake_workspace, "20260101_000104",
        meta=meta(
            status="done",
            phases={
                "final_acceptance": {
                    "scope_expansion": {
                        "items": [
                            "not-a-dict",
                            {"status": "risk"},  # no path → skipped
                            {"path": "keep.py", "status": "blocker"},
                        ],
                        "has_blocker": True,
                    },
                },
            },
        ),
    )

    result = inspect_run_evidence("20260101_000104", slice="scope_expansion")

    se = result.scope_expansion
    assert se is not None
    assert [i.path for i in se.items] == ["keep.py"]
    assert se.items[0].classification == "blocker"
    assert se.items[0].category is None
    assert se.items[0].evidence == []


def _core_shaped_scope_expansion_meta() -> dict:
    """Meta in the real core ``ScopeExpansionAssessment.to_dict()`` shape.

    Core writes ``status`` as the ENUM VALUE (``scope_expansion_notice`` /
    ``scope_expansion_risk`` / ``scope_expansion_blocker`` — see
    ``pipeline.engine.scope_expansion.ScopeExpansionItem.to_dict``) and carries a
    ``counts`` summary alongside ``items`` / ``has_blocker``. This pins that the
    MCP projector normalises those prefixed enum values onto the bare wire
    vocabulary the schema/docs/captain clients branch on.
    """
    return meta(
        status="rejected",
        phases={
            "final_acceptance": {
                "scope_expansion": {
                    "items": [
                        {
                            "path": "package-lock.json",
                            "category": "build",
                            "status": "scope_expansion_notice",
                            "evidence": ["verified", "explained"],
                        },
                        {
                            "path": "src/util/helper.py",
                            "category": "other",
                            "status": "scope_expansion_risk",
                            "evidence": ["no green gate"],
                        },
                        {
                            "path": "src/core/engine.py",
                            "category": "public_wire",
                            "status": "scope_expansion_blocker",
                            "evidence": ["unaligned public wire change"],
                        },
                    ],
                    "has_blocker": True,
                    "counts": {"notice": 1, "risk": 1, "blocker": 1},
                },
            },
        },
    )


def test_scope_expansion_normalizes_core_enum_values(fake_workspace) -> None:
    """Core-produced prefixed enum status values normalise to notice/risk/blocker."""
    write_run(
        fake_workspace, "20260101_000106",
        meta=_core_shaped_scope_expansion_meta(),
    )

    result = inspect_run_evidence("20260101_000106", slice="scope_expansion")

    se = result.scope_expansion
    assert se is not None
    assert se.has_blocker is True
    by_path = {i.path: i for i in se.items}
    assert by_path["package-lock.json"].classification == "notice"
    assert by_path["src/util/helper.py"].classification == "risk"
    assert by_path["src/core/engine.py"].classification == "blocker"
    # No prefixed enum value leaks through to the wire.
    assert all(
        not i.classification.startswith("scope_expansion_") for i in se.items
    )


def test_all_slice_includes_scope_expansion(fake_workspace) -> None:
    write_run(
        fake_workspace, "20260101_000105",
        meta=_scope_expansion_meta(),
    )

    result = inspect_run_evidence("20260101_000105", slice="all")

    assert result.scope_expansion is not None
    assert result.scope_expansion.has_blocker is True
    assert len(result.scope_expansion.items) == 3
