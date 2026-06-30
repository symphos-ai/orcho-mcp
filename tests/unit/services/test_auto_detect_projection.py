"""Auto-detect projection of ``meta.auto_detect`` (T2).

orcho-core persists ``meta.auto_detect`` ONLY for runs started through the
``auto-detect`` selector channel (``run_setup.py`` reads the scoped
``ORCHO_AUTODETECT_DECISION`` env and persists the serialized
``AutoDetectResolution``; a manual concrete profile never writes it). These
tests pin :func:`project_auto_detect`:

- ``requested_selector`` is the core selector token in EVERY case where the
  block is present (a request fact from the presence invariant, not a detector
  decision);
- ``disposition`` / ``trusted`` track ``detection_state`` (trusted only for
  ``recommended``);
- the deterministic ``next_action`` agent-control invariant: it is either
  ``None`` or ``kind='operator_input_required'``, and ``args['profile']`` is
  NEVER ``None`` / empty (the key is either absent or a non-empty profile);
- a missing block projects to ``None``; a partial / junk payload never raises.
"""
from __future__ import annotations

import pytest

from orcho_mcp.services.run_projection import (
    _AUTO_DETECT_PROFILE_TOKEN,
    project_auto_detect,
)
from orcho_mcp.tools import orcho_run_status
from tests.fixtures.mcp_workspace import meta, write_run

# The selector token MCP projects as ``requested_selector`` is the core
# constant — pin it so a drift in the imported token is caught here.
SELECTOR = _AUTO_DETECT_PROFILE_TOKEN


def _block(**extra) -> dict:
    """A core ``meta.auto_detect`` payload (resolution_to_payload shape)."""
    base = {
        "detection_state": "recommended",
        "actual_profile": "feature",
        "actual_mode": "fast",
        "policy": "trust_above_threshold",
        "recommended_profile": "feature",
        "recommended_mode": "fast",
        "confidence": 0.91,
        "rationale": "looks like a feature",
        "risk_flags": [],
        "fallback_used": False,
        "confirmation_state": "auto",
        "error_reason": None,
        "fallback_reason": None,
    }
    base.update(extra)
    return base


# ── disposition / trusted / next_action table ───────────────────────────────


def test_recommended_is_trusted_with_no_next_action():
    proj = project_auto_detect({"auto_detect": _block()})
    assert proj is not None
    assert proj.requested_selector == SELECTOR
    assert proj.detection_state == "recommended"
    assert proj.disposition == "recommended"
    assert proj.trusted is True
    assert proj.selected_profile == "feature"
    assert proj.selected_mode == "fast"
    assert proj.confidence == pytest.approx(0.91)
    # Trusted → no operator step.
    assert proj.next_action is None


def test_low_confidence_fallback_reruns_with_explicit_profile():
    proj = project_auto_detect({"auto_detect": _block(
        detection_state="low_confidence_fallback",
        actual_profile="feature",
        recommended_profile="complex_feature",
        confidence=0.21,
        fallback_used=True,
        confirmation_state=None,
        fallback_reason="confidence 0.21 < threshold 0.7",
    )})
    assert proj is not None
    assert proj.requested_selector == SELECTOR
    assert proj.disposition == "low_confidence_fallback"
    assert proj.trusted is False
    assert proj.fallback_used is True
    na = proj.next_action
    assert na is not None
    assert na.kind == "operator_input_required"
    assert na.tool == "orcho_run_start"
    assert na.requires_operator_input is True
    # candidate = first non-empty of (selected, recommended) → 'feature'.
    assert na.args["profile"] == "feature"


def test_detector_error_fallback_with_empty_profiles_omits_profile_key():
    proj = project_auto_detect({"auto_detect": _block(
        detection_state="detector_error_fallback",
        actual_profile="",
        actual_mode="",
        recommended_profile=None,
        recommended_mode=None,
        confidence=None,
        rationale=None,
        fallback_used=True,
        confirmation_state=None,
        error_reason="provider timeout",
        fallback_reason="detector error",
    )})
    assert proj is not None
    assert proj.requested_selector == SELECTOR
    assert proj.disposition == "detector_error_fallback"
    assert proj.trusted is False
    assert proj.error_reason == "provider timeout"
    na = proj.next_action
    assert na is not None
    assert na.kind == "operator_input_required"
    assert na.requires_operator_input is True
    # No concrete profile → operator must choose; NO profile key emitted.
    assert "profile" not in na.args
    # The dropped final arg is described machine-readably so the agent does
    # not have to parse ``intent`` prose to learn what input is missing.
    assert na.choices or na.input_schema
    assert na.input_schema is not None
    assert "profile" in na.input_schema["properties"]


def test_failed_with_empty_profiles_is_machine_readable_operator_input():
    proj = project_auto_detect({"auto_detect": _block(
        detection_state="failed",
        actual_profile="",
        recommended_profile="",
        confidence=None,
        fallback_used=False,
        confirmation_state=None,
        error_reason="provider unavailable",
        fallback_reason="detector error and on_error=fail",
    )})
    assert proj is not None
    assert proj.requested_selector == SELECTOR
    assert proj.disposition == "failed"
    assert proj.trusted is False
    na = proj.next_action
    assert na is not None
    assert na.kind == "operator_input_required"
    assert "profile" not in na.args
    # Typed input contract for the missing profile, not just intent prose.
    assert na.choices or na.input_schema
    assert na.input_schema is not None
    assert "profile" in na.input_schema["properties"]


# ── presence invariant + defensive coercion ─────────────────────────────────


def test_missing_auto_detect_block_projects_to_none():
    assert project_auto_detect({"status": "done"}) is None
    assert project_auto_detect({}) is None
    # Non-dict meta is tolerated.
    assert project_auto_detect(None) is None
    assert project_auto_detect("garbage") is None
    # A non-dict auto_detect value is not a real block.
    assert project_auto_detect({"auto_detect": "auto-detect"}) is None
    assert project_auto_detect({"auto_detect": ["x"]}) is None


def test_requested_selector_present_exactly_when_block_present():
    # Even an otherwise-empty block is a request fact: selector is set.
    proj = project_auto_detect({"auto_detect": {}})
    assert proj is not None
    assert proj.requested_selector == SELECTOR


def test_partial_and_junk_payload_never_raises():
    junk = {
        "detection_state": 123,            # non-str
        "actual_profile": ["nope"],        # non-str
        "actual_mode": None,
        "confidence": "high",              # non-float
        "risk_flags": "scary",             # non-list
        "fallback_used": "yes",            # non-bool → coerced to False
        "confirmation_state": 7,
    }
    proj = project_auto_detect({"auto_detect": junk})
    assert proj is not None
    assert proj.requested_selector == SELECTOR
    # Junk coerces to safe defaults rather than raising.
    assert proj.detection_state is None
    assert proj.selected_profile is None
    assert proj.confidence is None
    assert proj.risk_flags == []
    assert proj.fallback_used is False
    # Unknown/missing detection_state → no disposition, no next_action.
    assert proj.disposition is None
    assert proj.trusted is False
    assert proj.next_action is None


def test_bool_confidence_rejected_not_read_as_one():
    # ``True`` is an int subclass; it must NOT read as confidence 1.0.
    proj = project_auto_detect({"auto_detect": _block(confidence=True)})
    assert proj is not None
    assert proj.confidence is None


# ── cross-case agent-control invariant ──────────────────────────────────────


@pytest.mark.parametrize("block", [
    _block(),  # recommended → next_action None
    _block(detection_state="low_confidence_fallback", actual_profile="feature"),
    _block(detection_state="low_confidence_fallback", actual_profile="",
           recommended_profile="refactor"),
    _block(detection_state="detector_error_fallback", actual_profile="",
           recommended_profile=None),
    _block(detection_state="failed", actual_profile="", recommended_profile=""),
    _block(detection_state="bogus_state"),  # unknown → next_action None
])
def test_next_action_never_emits_empty_profile(block):
    """Invariant: across every disposition, ``next_action`` is either ``None``
    or ``operator_input_required`` and ``args['profile']`` is never
    ``None`` / empty (the key is absent or a non-empty string)."""
    proj = project_auto_detect({"auto_detect": block})
    assert proj is not None
    # requested_selector populated whenever the block is present.
    assert proj.requested_selector == SELECTOR
    na = proj.next_action
    if na is None:
        return
    assert na.kind == "operator_input_required"
    if "profile" in na.args:
        assert na.args["profile"]  # non-empty, not None
    else:
        # No final profile arg → the missing input MUST be described
        # machine-readably (operator_input_required contract).
        assert na.choices or na.input_schema


# ── wire-level: RunStatus.auto_detect end-to-end ────────────────────────────


def test_run_status_carries_typed_auto_detect(fake_workspace):
    write_run(
        fake_workspace, "20260101_000010",
        meta=meta(status="done", project="/p/x", task="t",
                  auto_detect=_block(detection_state="low_confidence_fallback",
                                     actual_profile="feature",
                                     fallback_used=True)),
    )
    status = orcho_run_status("20260101_000010")
    assert status.auto_detect is not None
    assert status.auto_detect.requested_selector == SELECTOR
    assert status.auto_detect.detection_state == "low_confidence_fallback"
    assert status.auto_detect.next_action is not None
    assert status.auto_detect.next_action.args["profile"] == "feature"


def test_run_status_surfaces_topology_for_cross_signal(fake_workspace):
    # Mock E2E (T5): a cross-signal task persists topology/delivery_scope/
    # projects in meta.auto_detect (core T2 shape); orcho_run_status must
    # surface them plus the three typed topology choices. The recommendation
    # never widened delivery — delivery_scope stays strict_mono.
    write_run(
        fake_workspace, "20260101_000020",
        meta=meta(
            status="done", project="/p/x", task="sdk wire + mcp schema change",
            auto_detect=_block(
                recommended_topology="cross_recommended",
                delivery_scope="strict_mono",
                delivery_projects=["orcho-core", "orcho-mcp"],
                topology_reason=(
                    "core SDK wire change likely requires MCP schema/tool "
                    "update"
                ),
            ),
        ),
    )
    status = orcho_run_status("20260101_000020")
    ad = status.auto_detect
    assert ad is not None
    assert ad.recommended_topology == "cross_recommended"
    assert ad.delivery_scope == "strict_mono"
    assert ad.projects == ["orcho-core", "orcho-mcp"]
    assert ad.topology_reason
    # Three typed choices: start cross / expanded mono / strict mono.
    assert len(ad.topology_next_actions) == 3
    for rec in ad.topology_next_actions:
        assert rec.kind == "operator_input_required"
        # Invariant: never an empty args.profile (absent or non-empty).
        if "profile" in rec.args:
            assert rec.args["profile"]
    # F1: each choice carries a STABLE machine-readable selector so a client
    # distinguishes start_cross / expanded_mono / strict_mono WITHOUT parsing
    # intent prose. ``topology_choice`` is present on all three.
    choices = [rec.args.get("topology_choice") for rec in ad.topology_next_actions]
    assert choices == ["start_cross", "expanded_mono", "strict_mono"]
    cross_rec, expanded_rec, strict_rec = ad.topology_next_actions
    # The two mono choices pre-fill the resulting delivery_scope.
    assert expanded_rec.args["delivery_scope"] == "expanded_mono"
    assert strict_rec.args["delivery_scope"] == "strict_mono"
    # The cross choice advertises its cross scope via input_schema (the
    # operator confirms the cross run; scope is not a pre-filled mono arg).
    assert "delivery_scope" not in cross_rec.args
    assert (
        cross_rec.input_schema["properties"]["delivery_scope"]["const"] == "cross"
    )


def test_run_status_mono_recommendation_has_no_topology_choices(fake_workspace):
    # A mono recommendation (no topology fields) surfaces empty projects and
    # no topology choices — the cross block is gated on cross_recommended.
    write_run(
        fake_workspace, "20260101_000021",
        meta=meta(status="done", project="/p/x", task="t",
                  auto_detect=_block()),
    )
    status = orcho_run_status("20260101_000021")
    ad = status.auto_detect
    assert ad is not None
    assert ad.recommended_topology is None
    assert ad.projects == []
    assert ad.topology_next_actions == []


def test_run_status_auto_detect_none_without_block(fake_workspace):
    write_run(
        fake_workspace, "20260101_000011",
        meta=meta(status="done", project="/p/x", task="t"),
    )
    status = orcho_run_status("20260101_000011")
    assert status.auto_detect is None


def test_run_status_unknown_detection_state_does_not_raise_wire(fake_workspace):
    # A version-skewed core emitting an unknown detection_state must degrade
    # to None at the wire (Literal contract) rather than raise.
    write_run(
        fake_workspace, "20260101_000012",
        meta=meta(status="done", project="/p/x", task="t",
                  auto_detect=_block(detection_state="future_state")),
    )
    status = orcho_run_status("20260101_000012")
    assert status.auto_detect is not None
    assert status.auto_detect.detection_state is None
    assert status.auto_detect.disposition is None
    assert status.auto_detect.requested_selector == SELECTOR
