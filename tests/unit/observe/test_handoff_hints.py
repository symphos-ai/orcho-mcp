"""Unit tests for paused-run handoff hint synthesis via ``orcho_run_watch``.

Covers the handoff trigger path: ``HandoffDecisionHint`` payload shape,
findings compaction (including SDK fallback when meta omits findings),
prompt rendering, ``interaction_client`` profile selection, and the
correctness/presentation split that lets the profile shape ``client_hints``
without affecting ``available_actions`` or ``default_action``.

Backed by ``orcho_mcp.observe.handoff_hints``; the ``meta.phase_handoff``
read-model (incl. the SDK findings fallback) is projected by
``orcho_mcp.services.run_projection``, so SDK fallback monkeypatches
target ``orcho_mcp.services.run_projection._sdk_list_findings``.
"""
from __future__ import annotations

import inspect

import pytest

from orcho_mcp.tools import orcho_run_watch
from tests.fixtures.mcp_workspace import write_run


def _ev(seq: int, kind: str = "phase.start", phase: str = "plan", **payload):
    return {"seq": seq, "ts": f"2026-01-01T00:00:{seq:02d}", "kind": kind,
            "phase": phase, "payload": payload}


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ── Stage-5 deferral: handoff_hints stays on the direct meta read ───────────
#
# T2 evaluated replacing the manual ``load_meta`` + ``meta.get("phase_handoff")``
# extraction in ``build_handoff_hint`` with
# ``sdk.run_control.load_run_snapshot(run_id).pending_action``. The
# replacement was DEFERRED (a valid Stage-5 outcome) because it is not
# byte-for-byte identical and offers no clean reduction. These guards pin
# the reasons so a future adaptation has to re-prove single+cross identity
# before flipping them.


def test_handoff_hints_decoupled_from_run_control_snapshot():
    """build_handoff_hint intentionally does NOT consume the run-control
    snapshot model.

    Deferral guard: if someone routes ``build_handoff_hint`` through
    ``load_run_snapshot`` / ``PendingOperatorAction`` they must first
    prove the HandoffDecisionHint stays byte-for-byte identical on single
    AND cross (the T2 done-criterion). This static check flips the moment
    that coupling is introduced.
    """
    from orcho_mcp.observe import handoff_hints

    src = inspect.getsource(handoff_hints)
    assert "load_run_snapshot" not in src
    assert "PendingOperatorAction" not in src
    assert "run_control" not in src


def test_pending_action_drops_subset_on_gate_status(fake_workspace):
    """Why the adaptation diverges (1): on an ``awaiting_gate_decision``
    run the snapshot yields a ``kind="gate"`` action with no
    available_actions / handoff_id / phase, but ``build_handoff_hint``
    fires on this status too and reads those values from meta.phase_handoff.

    Routing the subset through ``pending_action`` would therefore silently
    drop available_actions, handoff_id, and phase for gate pauses.
    """
    from sdk.run_control import load_run_snapshot

    write_run(
        fake_workspace, "20260101_gate",
        meta={
            "project": "/p/x",
            "status": "awaiting_gate_decision",
            "task": "t",
            "phase_handoff": {
                "id": "validate_plan:plan_round:2",
                "phase": "validate_plan",
                "available_actions": ["continue", "halt"],
            },
        },
        events=[_ev(1)],
    )

    snap = load_run_snapshot("20260101_gate", cwd=None)
    pa = snap.pending_action
    assert pa is not None
    # Gate action carries none of the meta.phase_handoff subset.
    assert pa.kind == "gate"
    assert pa.available_actions == ()
    assert pa.handoff_id is None
    assert pa.phase is None


def test_pending_action_subset_matches_single_but_omits_enrichment(
    fake_workspace,
):
    """Why the adaptation diverges (2): for a single ``awaiting_phase_handoff``
    run the {available_actions, handoff_id, phase} subset DOES match the
    snapshot, but PendingOperatorAction does not promote trigger / artifacts
    / findings — build_handoff_hint would still need meta.phase_handoff.

    For single runs ``raw`` happens to equal meta.phase_handoff, so the
    enrichment is reachable; for cross runs ``raw`` is the checkpoint, so
    the same access path would lose trigger / artifacts / findings. That
    cross/single asymmetry is the core reason the swap is not byte-for-byte
    identical and stays deferred.
    """
    from sdk.run_control import load_run_snapshot

    write_run(
        fake_workspace, "20260101_single",
        meta={
            "project": "/p/x",
            "status": "awaiting_phase_handoff",
            "task": "t",
            "phase_handoff": {
                "id": "implement:round:1",
                "phase": "implement",
                "available_actions": ["continue_with_waiver", "halt"],
                "trigger": "incomplete",
                "artifacts": {"incomplete_subtasks": ["T2", "T3"]},
                "findings": [{"title": "x", "severity": "P1"}],
            },
        },
        events=[_ev(1)],
    )

    snap = load_run_snapshot("20260101_single", cwd=None)
    pa = snap.pending_action
    assert pa is not None
    # Subset matches verbatim for the single awaiting_phase_handoff case.
    assert pa.kind == "phase_handoff"
    assert pa.available_actions == ("continue_with_waiver", "halt")
    assert pa.handoff_id == "implement:round:1"
    assert pa.phase == "implement"
    # ...but trigger / artifacts / findings are NOT promoted fields.
    assert not hasattr(pa, "trigger")
    assert not hasattr(pa, "artifacts")
    assert not hasattr(pa, "findings")
    # They are only reachable via raw, which for a single run equals
    # meta.phase_handoff (for a cross run raw is the checkpoint instead).
    assert pa.raw.get("trigger") == "incomplete"
    assert pa.raw.get("artifacts") == {"incomplete_subtasks": ["T2", "T3"]}


@pytest.mark.anyio
async def test_watch_handoff_returns_hint(fake_workspace):
    """Awaiting-handoff status surfaces a populated HandoffDecisionHint
    with id, phase, and available_actions pulled from meta.phase_handoff."""
    write_run(
        fake_workspace, "20260101_000001",
        meta={
            "project": "/p/x",
            "status": "awaiting_phase_handoff",
            "task": "t",
            "phase_handoff": {
                "id": "validate_plan:plan_round:2",
                "phase": "validate_plan",
                "available_actions": ["continue", "halt"],
            },
        },
        events=[_ev(1), _ev(2)],
    )
    r = await orcho_run_watch(
        "20260101_000001", since_seq=0,
        until="handoff_or_terminal", timeout_s=5,
    )
    assert r.trigger.kind == "handoff"
    assert r.handoff is not None
    assert r.handoff.handoff_id == "validate_plan:plan_round:2"
    assert r.handoff.phase == "validate_plan"
    assert r.handoff.available_actions == ["continue", "halt"]
    assert r.handoff.decision_tool == "orcho_phase_handoff_decide"
    assert r.handoff.resume_tool == "orcho_run_resume"
    assert r.handoff.findings_summary is None
    assert r.handoff.feedback_required_for == []
    assert "validate_plan" in r.handoff.recommended_user_prompt


@pytest.mark.anyio
async def test_watch_handoff_hint_full_shape_populated(fake_workspace):
    """Every wire field on ``HandoffDecisionHint`` is populated when
    ``meta.phase_handoff`` carries the full payload.

    Pins the schema completeness: a future change that drops a field
    from the populated path (or renames one) fails here, not later
    in client integration. The asserted set is the wire shape from
    ``schemas/observe.py``:

      kind, run_id, handoff_id, phase, title, findings_summary,
      findings, available_actions, default_action,
      feedback_required_for, decision_tool, resume_tool,
      recommended_user_prompt, client_hints.

    Companion to the per-field behavioural tests (findings fallback,
    interaction_client flavors, prompt content) — this one asserts
    "every field has a value on the happy path".
    """
    write_run(
        fake_workspace, "20260101_000001",
        meta={
            "project": "/p/x",
            "status": "awaiting_phase_handoff",
            "task": "t",
            "phase_handoff": {
                "id": "validate_plan:plan_round:1",
                "phase": "validate_plan",
                "available_actions": ["continue", "retry_feedback", "halt"],
                "findings": [
                    {
                        "id": "F1",
                        "severity": "P1",
                        "title": "missing acceptance criteria",
                        "body": "Plan lacks measurable success criteria for step 3.",
                        "required_fix": "Add acceptance criteria to step 3.",
                        "file": "docs/plan.md",
                        "line": 42,
                    },
                ],
            },
        },
        events=[_ev(1)],
    )

    r = await orcho_run_watch(
        "20260101_000001", since_seq=0,
        until="handoff_or_terminal", timeout_s=5,
    )

    assert r.handoff is not None
    h = r.handoff

    # Wire-shape fields — every one of these has a non-None / non-empty value.
    assert h.kind == "requires_user_decision"
    assert h.run_id == "20260101_000001"
    assert h.handoff_id == "validate_plan:plan_round:1"
    assert h.phase == "validate_plan"
    assert h.title  # non-empty string
    assert h.findings_summary  # non-None when findings present
    assert len(h.findings) == 1
    assert h.findings[0].severity == "P1"
    assert h.findings[0].title  # passed through
    assert h.available_actions == ["continue", "retry_feedback", "halt"]
    assert h.default_action in h.available_actions  # picked from offered set
    assert "retry_feedback" in h.feedback_required_for
    assert h.decision_tool == "orcho_phase_handoff_decide"
    assert h.resume_tool == "orcho_run_resume"
    assert h.recommended_user_prompt  # non-empty bounded prompt
    # client_hints sub-object — always populated (defaults to "generic").
    assert h.client_hints is not None
    assert h.client_hints.client == "generic"

    # choices[] — ready-to-call menu mirrors known runtime actions.
    assert len(h.choices) == 3
    assert {c.action for c in h.choices} == {"continue", "retry_feedback", "halt"}
    # Every choice has a non-empty label and the correct decision tool.
    for c in h.choices:
        assert c.label
        assert c.tool == "orcho_phase_handoff_decide"
        # args always carries run_id + action; handoff_id when present.
        assert c.args["run_id"] == "20260101_000001"
        assert c.args["action"] == c.action
        assert c.args["handoff_id"] == "validate_plan:plan_round:1"


@pytest.mark.anyio
async def test_watch_handoff_missing_meta_keys_safe(fake_workspace):
    """Malformed meta (no phase_handoff block) must not crash; hint
    falls back to safe defaults so the user still gets a prompt."""
    write_run(
        fake_workspace, "20260101_000001",
        meta={
            "project": "/p/x",
            "status": "awaiting_phase_handoff",
            "task": "t",
        },
        events=[_ev(1)],
    )
    r = await orcho_run_watch(
        "20260101_000001", since_seq=0,
        until="handoff_or_terminal", timeout_s=5,
    )
    assert r.trigger.kind == "handoff"
    assert r.handoff is not None
    assert r.handoff.handoff_id is None
    assert r.handoff.available_actions == []


# ── implement / incomplete pause phrasing ───────────────────────────────────


@pytest.mark.anyio
async def test_watch_handoff_implement_incomplete_phrasing(fake_workspace):
    """phase='implement' + trigger='incomplete' surfaces the natural
    "paused at implement: N subtask(s) incomplete" phrasing in both the
    prompt and the title, with N = |incomplete ∪ missing receipts|."""
    write_run(
        fake_workspace, "20260101_000001",
        meta={
            "project": "/p/x",
            "status": "awaiting_phase_handoff",
            "task": "t",
            "phase_handoff": {
                "id": "implement:round:1",
                "phase": "implement",
                "trigger": "incomplete",
                "available_actions": [
                    "continue_with_waiver", "retry_feedback", "halt",
                ],
                "artifacts": {
                    # union {T2, T3, T4} → N = 3
                    "incomplete_subtasks": ["T2", "T3"],
                    "missing_subtask_receipts": ["T3", "T4"],
                },
            },
        },
        events=[_ev(1)],
    )
    r = await orcho_run_watch(
        "20260101_000001", since_seq=0,
        until="handoff_or_terminal", timeout_s=5,
    )
    assert r.handoff is not None
    prompt = r.handoff.recommended_user_prompt
    assert "paused at implement: 3 subtasks incomplete" in prompt
    assert "implement" in r.handoff.title
    assert "3" in r.handoff.title
    # Correctness fields untouched — waiver still feedback-gated.
    assert "continue_with_waiver" in r.handoff.available_actions
    assert "continue_with_waiver" in r.handoff.feedback_required_for


@pytest.mark.anyio
async def test_watch_handoff_implement_incomplete_singular(fake_workspace):
    """N == 1 uses the singular "1 subtask incomplete" form."""
    write_run(
        fake_workspace, "20260101_000001",
        meta={
            "project": "/p/x",
            "status": "awaiting_phase_handoff",
            "task": "t",
            "phase_handoff": {
                "id": "implement:round:1",
                "phase": "implement",
                "trigger": "incomplete",
                "available_actions": ["continue_with_waiver", "halt"],
                "artifacts": {"incomplete_subtasks": ["T2"]},
            },
        },
        events=[_ev(1)],
    )
    r = await orcho_run_watch(
        "20260101_000001", since_seq=0,
        until="handoff_or_terminal", timeout_s=5,
    )
    assert r.handoff is not None
    assert (
        "paused at implement: 1 subtask incomplete"
        in r.handoff.recommended_user_prompt
    )


@pytest.mark.anyio
async def test_watch_handoff_implement_malformed_trigger_artifacts_safe(
    fake_workspace,
):
    """Malformed meta at an implement pause — trigger absent and
    ``artifacts`` not a dict — must not raise; the hint falls back to the
    generic "paused at implement." phrasing without a count."""
    write_run(
        fake_workspace, "20260101_000001",
        meta={
            "project": "/p/x",
            "status": "awaiting_phase_handoff",
            "task": "t",
            "phase_handoff": {
                "id": "implement:round:1",
                "phase": "implement",
                # no ``trigger`` key
                "available_actions": ["continue_with_waiver", "halt"],
                "artifacts": "not-a-dict",
            },
        },
        events=[_ev(1)],
    )
    r = await orcho_run_watch(
        "20260101_000001", since_seq=0,
        until="handoff_or_terminal", timeout_s=5,
    )
    assert r.handoff is not None  # no exception at the pause moment
    prompt = r.handoff.recommended_user_prompt
    assert "Orcho run 20260101_000001 paused at implement." in prompt
    assert "subtasks incomplete" not in prompt
    assert r.handoff.title == "implement requires a decision"


@pytest.mark.anyio
async def test_watch_handoff_incomplete_trigger_non_implement_phase(
    fake_workspace,
):
    """The special phrasing requires BOTH implement phase AND incomplete
    trigger — a non-implement phase keeps the generic phrasing even with
    ``trigger='incomplete'``."""
    write_run(
        fake_workspace, "20260101_000001",
        meta={
            "project": "/p/x",
            "status": "awaiting_phase_handoff",
            "task": "t",
            "phase_handoff": {
                "id": "validate_plan:plan_round:2",
                "phase": "validate_plan",
                "trigger": "incomplete",
                "available_actions": ["retry_feedback", "halt"],
                "artifacts": {"incomplete_subtasks": ["T2", "T3"]},
            },
        },
        events=[_ev(1)],
    )
    r = await orcho_run_watch(
        "20260101_000001", since_seq=0,
        until="handoff_or_terminal", timeout_s=5,
    )
    assert r.handoff is not None
    prompt = r.handoff.recommended_user_prompt
    assert "paused at validate_plan." in prompt
    assert "subtasks incomplete" not in prompt
    assert r.handoff.title == "validate_plan requires a decision"


# ── interactive handoff enrichment ──────────────────────────────────────────


@pytest.mark.anyio
async def test_watch_handoff_prompt_includes_findings_and_actions(
    fake_workspace,
):
    """Enriched handoff carries compact findings + structured-choice prompt."""
    write_run(
        fake_workspace, "20260101_000001",
        meta={
            "project": "/p/x",
            "status": "awaiting_phase_handoff",
            "task": "t",
            "phase_handoff": {
                "id": "validate_plan:plan_round:2",
                "phase": "validate_plan",
                "available_actions": ["retry_feedback", "continue", "halt"],
                "findings": [
                    {
                        "id": "f1",
                        "severity": "P1",
                        "title": "Missing stdio smoke",
                        "body": "L3 layer skipped",
                        "required_fix": "Add L3 test",
                    },
                ],
            },
        },
        events=[_ev(1), _ev(2)],
    )
    r = await orcho_run_watch(
        "20260101_000001", since_seq=0,
        until="handoff_or_terminal", timeout_s=5,
    )
    assert r.handoff is not None
    assert r.handoff.run_id == "20260101_000001"
    assert r.handoff.default_action == "retry_feedback"
    assert r.handoff.feedback_required_for == ["retry_feedback"]
    assert len(r.handoff.findings) == 1
    f = r.handoff.findings[0]
    assert f.severity == "P1"
    assert f.title == "Missing stdio smoke"
    assert f.required_fix == "Add L3 test"
    assert r.handoff.findings_summary is not None
    assert "P1" in r.handoff.findings_summary
    prompt = r.handoff.recommended_user_prompt
    assert "Choose one" in prompt
    assert "retry_feedback" in prompt
    assert "If retry_feedback, provide feedback text" in prompt
    assert "validate_plan" in prompt
    # Client hints — generic profile defaults. With no
    # ``interaction_client`` passed, the response carries the generic
    # profile: free-form chat with the follow-up tools called out.
    assert r.handoff.client_hints.client == "generic"
    assert r.handoff.client_hints.interaction_style == "free_form"
    assert r.handoff.client_hints.preferred_render == "chat"
    assert r.handoff.client_hints.show_actions is True
    assert r.handoff.client_hints.allow_feedback_text is True
    assert r.handoff.client_hints.clarify_on_ambiguous_reply is True
    assert r.handoff.client_hints.include_followup_tools is True
    assert r.handoff.client_hints.action_field == "action"
    assert r.handoff.client_hints.feedback_field == "feedback"


@pytest.mark.anyio
async def test_watch_handoff_omits_unavailable_actions(fake_workspace):
    """``retry_feedback`` never surfaces if the runtime did not offer it."""
    write_run(
        fake_workspace, "20260101_000001",
        meta={
            "project": "/p/x",
            "status": "awaiting_phase_handoff",
            "task": "t",
            "phase_handoff": {
                "id": "gate:x",
                "phase": "final_qa",
                "available_actions": ["continue", "halt"],
                "findings": [],
            },
        },
        events=[_ev(1)],
    )
    r = await orcho_run_watch(
        "20260101_000001", since_seq=0,
        until="handoff_or_terminal", timeout_s=5,
    )
    assert r.handoff is not None
    assert "retry_feedback" not in r.handoff.available_actions
    assert r.handoff.default_action == "continue"
    assert r.handoff.feedback_required_for == []
    prompt = r.handoff.recommended_user_prompt
    assert "retry_feedback" not in prompt
    assert "If retry_feedback, provide feedback text" not in prompt


@pytest.mark.anyio
async def test_watch_handoff_malformed_findings_do_not_crash(fake_workspace):
    """Pathological ``findings`` shapes must not break Pydantic, and the
    bounded prompt cap must hold."""
    write_run(
        fake_workspace, "20260101_000001",
        meta={
            "project": "/p/x",
            "status": "awaiting_phase_handoff",
            "task": "t",
            "phase_handoff": {
                "id": "gate:x",
                "phase": "validate_plan",
                "available_actions": ["retry_feedback", "halt"],
                "findings": [
                    "raw string finding",
                    {"title": None},
                    {"title": "x" * 5000, "body": "y" * 10000},
                    {"title": "ok", "severity": "P2"},
                    # 5th valid
                    {"title": "another"},
                    # 6th — must be dropped (limit = 5)
                    {"title": "should not appear"},
                ],
            },
        },
        events=[_ev(1)],
    )
    r = await orcho_run_watch(
        "20260101_000001", since_seq=0,
        until="handoff_or_terminal", timeout_s=5,
    )
    assert r.handoff is not None
    assert len(r.handoff.findings) <= 5
    # No 6th survived.
    titles = [f.title for f in r.handoff.findings]
    assert "should not appear" not in titles
    # Per-field caps enforced.
    for f in r.handoff.findings:
        assert len(f.title) <= 160
        if f.body is not None:
            assert len(f.body) <= 300
    # Prompt cap.
    assert len(r.handoff.recommended_user_prompt) <= 1500


@pytest.mark.anyio
async def test_watch_handoff_fallback_to_sdk_findings(
    fake_workspace, monkeypatch,
):
    """When meta does not carry ``findings``, the helper falls back to
    ``_sdk_list_findings``. Monkeypatched here so the L1 layer stays
    fixture-cheap; the real SDK path is exercised in L4."""
    from types import SimpleNamespace

    fake_finding = SimpleNamespace(
        id="sdk-1",
        severity="P2",
        title="Plan missing test plan",
        body="Acceptance criteria not stated",
        required_fix="Add acceptance criteria",
        file=None,
        line=None,
    )

    def fake_list_findings(run_id, cwd=None, phases=None):  # noqa: ARG001
        assert phases == ("validate_plan",)
        return [fake_finding]

    monkeypatch.setattr(
        "orcho_mcp.services.run_projection._sdk_list_findings", fake_list_findings,
    )

    write_run(
        fake_workspace, "20260101_000001",
        meta={
            "project": "/p/x",
            "status": "awaiting_phase_handoff",
            "task": "t",
            "phase_handoff": {
                "id": "gate:x",
                "phase": "validate_plan",
                "available_actions": ["retry_feedback", "halt"],
                # no ``findings`` key — triggers the SDK fallback.
            },
        },
        events=[_ev(1)],
    )
    r = await orcho_run_watch(
        "20260101_000001", since_seq=0,
        until="handoff_or_terminal", timeout_s=5,
    )
    assert r.handoff is not None
    assert len(r.handoff.findings) == 1
    assert r.handoff.findings[0].title == "Plan missing test plan"
    assert r.handoff.findings[0].severity == "P2"
    assert r.handoff.findings_summary is not None
    assert "P2" in r.handoff.findings_summary


@pytest.mark.anyio
async def test_watch_handoff_empty_findings_triggers_fallback(
    fake_workspace, monkeypatch,
):
    """Regression: ``meta.phase_handoff.findings == []`` must still
    fall back to ``_sdk_list_findings``.

    An empty list is list-like but carries no useful information — in
    real payloads it commonly means "not embedded in meta", while the
    evidence path may still have findings. A truthy guard
    (``isinstance(raw, list) and raw``) keeps the SDK fallback active
    in this case.
    """
    from types import SimpleNamespace

    fake_finding = SimpleNamespace(
        id="sdk-empty-1",
        severity="P1",
        title="Surfaced via SDK fallback",
        body=None,
        required_fix="Re-plan with acceptance criteria",
        file=None,
        line=None,
    )
    calls: list[tuple] = []

    def fake_list_findings(run_id, cwd=None, phases=None):  # noqa: ARG001
        calls.append((run_id, phases))
        return [fake_finding]

    monkeypatch.setattr(
        "orcho_mcp.services.run_projection._sdk_list_findings", fake_list_findings,
    )

    write_run(
        fake_workspace, "20260101_000001",
        meta={
            "project": "/p/x",
            "status": "awaiting_phase_handoff",
            "task": "t",
            "phase_handoff": {
                "id": "gate:x",
                "phase": "validate_plan",
                "available_actions": ["retry_feedback", "halt"],
                "findings": [],  # the regression target: empty list
            },
        },
        events=[_ev(1)],
    )
    r = await orcho_run_watch(
        "20260101_000001", since_seq=0,
        until="handoff_or_terminal", timeout_s=5,
    )
    assert r.handoff is not None
    # Fallback fired — fake_list_findings was invoked with the right phase.
    assert calls == [("20260101_000001", ("validate_plan",))]
    # And its result populated the handoff.
    assert len(r.handoff.findings) == 1
    assert r.handoff.findings[0].title == "Surfaced via SDK fallback"
    assert r.handoff.findings_summary is not None
    assert "P1" in r.handoff.findings_summary


@pytest.mark.anyio
async def test_watch_handoff_summary_false_still_returns_handoff(
    fake_workspace,
):
    """``summary=False`` must not suppress the interactive handoff
    packet — the agent still needs the structured decision data."""
    write_run(
        fake_workspace, "20260101_000001",
        meta={
            "project": "/p/x",
            "status": "awaiting_phase_handoff",
            "task": "t",
            "phase_handoff": {
                "id": "gate:x",
                "phase": "validate_plan",
                "available_actions": ["retry_feedback", "halt"],
                "findings": [{"title": "X", "severity": "P1"}],
            },
        },
        events=[_ev(1)],
    )
    r = await orcho_run_watch(
        "20260101_000001", since_seq=0,
        until="handoff_or_terminal", timeout_s=5, summary=False,
    )
    assert r.summary is None
    assert r.handoff is not None
    assert r.handoff.run_id == "20260101_000001"
    assert r.handoff.available_actions == ["retry_feedback", "halt"]
    assert len(r.handoff.findings) == 1
    assert "Choose one" in r.handoff.recommended_user_prompt


# ── client interaction profiles ─────────────────────────────────────────────


def _seed_handoff_run(fake_workspace, run_id="20260101_000001"):
    """Shared helper for profile tests — paused run with the same
    findings + actions across cases so profile assertions stay focused."""
    write_run(
        fake_workspace, run_id,
        meta={
            "project": "/p/x",
            "status": "awaiting_phase_handoff",
            "task": "t",
            "phase_handoff": {
                "id": "validate_plan:plan_round:2",
                "phase": "validate_plan",
                "available_actions": ["retry_feedback", "continue", "halt"],
                "findings": [
                    {"severity": "P1", "title": "Missing stdio smoke"},
                ],
            },
        },
        events=[_ev(1), _ev(2)],
    )
    return run_id


@pytest.mark.anyio
async def test_watch_handoff_codex_client_hints(fake_workspace):
    """``interaction_client="codex"`` shapes ``client_hints`` toward the
    Ask-style render. Available actions remain unchanged."""
    run_id = _seed_handoff_run(fake_workspace)
    r = await orcho_run_watch(
        run_id, since_seq=0, until="handoff_or_terminal",
        timeout_s=5, interaction_client="codex",
    )
    assert r.handoff is not None
    hints = r.handoff.client_hints
    assert hints.client == "codex"
    assert hints.interaction_style == "ask"
    assert hints.preferred_render == "ask"
    assert hints.show_actions is True
    assert hints.allow_feedback_text is True
    assert hints.clarify_on_ambiguous_reply is True
    assert hints.include_followup_tools is True
    # Profile does not affect correctness.
    assert r.handoff.available_actions == [
        "retry_feedback", "continue", "halt",
    ]
    assert r.handoff.default_action == "retry_feedback"
    # The closing render-hint line varies per profile (presentation only).
    assert "Ask prompt" in r.handoff.recommended_user_prompt


@pytest.mark.anyio
async def test_watch_handoff_claude_code_client_hints(fake_workspace):
    """``interaction_client="claude-code"`` shapes ``client_hints``
    toward a structured-choice chat render."""
    run_id = _seed_handoff_run(fake_workspace)
    r = await orcho_run_watch(
        run_id, since_seq=0, until="handoff_or_terminal",
        timeout_s=5, interaction_client="claude-code",
    )
    assert r.handoff is not None
    hints = r.handoff.client_hints
    assert hints.client == "claude-code"
    assert hints.interaction_style == "structured_choice"
    assert hints.preferred_render == "chat"
    assert hints.clarify_on_ambiguous_reply is True
    assert hints.include_followup_tools is True
    # Correctness invariants.
    assert r.handoff.available_actions == [
        "retry_feedback", "continue", "halt",
    ]
    assert "Present these options" in r.handoff.recommended_user_prompt


@pytest.mark.anyio
async def test_watch_handoff_unknown_client_uses_generic(fake_workspace):
    """Unknown ``interaction_client`` values fall back to ``generic``
    without crashing — forward compat for future client names."""
    run_id = _seed_handoff_run(fake_workspace)
    r = await orcho_run_watch(
        run_id, since_seq=0, until="handoff_or_terminal",
        timeout_s=5, interaction_client="antigravity",
    )
    assert r.handoff is not None
    hints = r.handoff.client_hints
    assert hints.client == "generic"
    assert hints.interaction_style == "free_form"
    assert hints.preferred_render == "chat"
    # Correctness invariants.
    assert r.handoff.available_actions == [
        "retry_feedback", "continue", "halt",
    ]
    assert r.handoff.default_action == "retry_feedback"


@pytest.mark.anyio
async def test_interaction_client_does_not_affect_trigger_or_actions(
    fake_workspace,
):
    """Profile selection is presentation-only — correctness fields must
    be identical across ``generic`` and ``codex`` for the same fixture."""
    run_id = _seed_handoff_run(fake_workspace)

    r_generic = await orcho_run_watch(
        run_id, since_seq=0, until="handoff_or_terminal",
        timeout_s=5, interaction_client="generic",
    )
    r_codex = await orcho_run_watch(
        run_id, since_seq=0, until="handoff_or_terminal",
        timeout_s=5, interaction_client="codex",
    )

    # Trigger shape stable.
    assert r_generic.trigger.kind == r_codex.trigger.kind == "handoff"
    assert r_generic.trigger.seq == r_codex.trigger.seq
    assert r_generic.trigger.status == r_codex.trigger.status
    assert r_generic.trigger.phase == r_codex.trigger.phase

    # Decision data stable.
    assert r_generic.handoff is not None and r_codex.handoff is not None
    assert (
        r_generic.handoff.available_actions
        == r_codex.handoff.available_actions
    )
    assert r_generic.handoff.default_action == r_codex.handoff.default_action
    assert (
        r_generic.handoff.feedback_required_for
        == r_codex.handoff.feedback_required_for
    )
    assert r_generic.handoff.handoff_id == r_codex.handoff.handoff_id
    assert r_generic.handoff.decision_tool == r_codex.handoff.decision_tool
    assert r_generic.handoff.resume_tool == r_codex.handoff.resume_tool

    # Only client_hints (and the closing render-hint line) differ.
    assert r_generic.handoff.client_hints.client == "generic"
    assert r_codex.handoff.client_hints.client == "codex"


# ── HandoffDecisionHint.choices — ready-to-call decision menu ───────────────


_HANDOFF_RUN_ID = "20260101_choices"


def _write_handoff_run(fake_workspace, *, available_actions: list[str]):
    """Build a paused run with the given ``available_actions`` set."""
    write_run(
        fake_workspace, _HANDOFF_RUN_ID,
        meta={
            "project": "/p/x",
            "status": "awaiting_phase_handoff",
            "task": "t",
            "phase_handoff": {
                "id": "validate_plan:plan_round:1",
                "phase": "validate_plan",
                "available_actions": available_actions,
            },
        },
        events=[_ev(1)],
    )


async def _watch_for_handoff(run_id: str):
    """Drive ``orcho_run_watch`` until the handoff trigger fires."""
    return await orcho_run_watch(
        run_id, since_seq=0,
        until="handoff_or_terminal", timeout_s=5,
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("available", "want_choice_actions"),
    [
        pytest.param(
            ["retry_feedback", "continue", "halt"],
            ["retry_feedback", "continue", "halt"],
            id="full-three",
        ),
        pytest.param(
            ["continue", "retry_feedback", "continue_with_waiver", "halt"],
            # choices preserve the runtime's own order within the known set.
            ["continue", "retry_feedback", "continue_with_waiver", "halt"],
            id="all-four",
        ),
        pytest.param(
            ["continue", "halt"],
            ["continue", "halt"],
            id="continue-and-halt",
        ),
        pytest.param(
            ["halt"],
            ["halt"],
            id="halt-only",
        ),
    ],
)
async def test_handoff_choices_match_known_runtime_actions(
    fake_workspace, available: list[str], want_choice_actions: list[str],
):
    """``choices[]`` mirrors the order of known runtime actions.

    Every entry in the input lands as a choice (all of them are
    known); ordering follows the runtime's own preference.
    """
    _write_handoff_run(fake_workspace, available_actions=available)
    r = await _watch_for_handoff(_HANDOFF_RUN_ID)

    assert r.handoff is not None
    assert [c.action for c in r.handoff.choices] == want_choice_actions


@pytest.mark.anyio
async def test_handoff_choices_filter_unknown_runtime_verbs(fake_workspace):
    """Unknown runtime verbs are dropped from ``choices`` but kept in
    ``available_actions``.

    Forward-compatibility safety: if the runtime offers a verb the
    wire layer does not know how to call, the ready-to-call menu
    must not include it (the agent would blindly forward the
    unknown ``action`` to ``orcho_phase_handoff_decide``). The raw
    verb stays on ``available_actions`` so callers that handle
    new runtime verbs natively can still see them.
    """
    _write_handoff_run(
        fake_workspace,
        available_actions=["continue", "future_unknown_verb", "halt"],
    )
    r = await _watch_for_handoff(_HANDOFF_RUN_ID)

    assert r.handoff is not None
    # available_actions: verbatim runtime offering, unknown verb preserved.
    assert "future_unknown_verb" in r.handoff.available_actions
    # choices: filtered to known callables only.
    choice_actions = [c.action for c in r.handoff.choices]
    assert "future_unknown_verb" not in choice_actions
    assert choice_actions == ["continue", "halt"]


@pytest.mark.anyio
async def test_handoff_choice_retry_feedback_safe_args(fake_workspace):
    """``retry_feedback`` choice never carries a placeholder feedback
    string in ``args``.

    ``args`` is always safe to forward verbatim; the feedback kwarg
    is added by the agent after collecting user input. This pins
    the contract that a weak agent cannot accidentally send a
    placeholder string to ``orcho_phase_handoff_decide``.
    """
    _write_handoff_run(
        fake_workspace, available_actions=["retry_feedback"],
    )
    r = await _watch_for_handoff(_HANDOFF_RUN_ID)
    assert r.handoff is not None
    [choice] = r.handoff.choices
    assert choice.action == "retry_feedback"
    assert choice.requires_feedback is True
    assert choice.feedback_field == "feedback"
    assert choice.feedback_placeholder  # non-empty operator-side hint
    assert choice.elicitation is not None
    assert choice.elicitation.mode == "form"
    assert choice.elicitation.client_capability == "elicitation.form"
    assert choice.elicitation.field == "feedback"
    assert choice.elicitation.message == choice.feedback_placeholder
    assert choice.elicitation.requested_schema["required"] == ["feedback"]
    # args MUST NOT contain feedback — that is the agent's responsibility.
    assert "feedback" not in choice.args
    assert choice.args == {
        "run_id": _HANDOFF_RUN_ID,
        "handoff_id": "validate_plan:plan_round:1",
        "action": "retry_feedback",
    }
    # Followup is resume with complete args (safe to forward verbatim).
    assert choice.followup is not None
    assert choice.followup.tool == "orcho_run_resume"
    assert choice.followup.args == {"run_id": _HANDOFF_RUN_ID}


@pytest.mark.anyio
async def test_handoff_choice_continue_with_waiver_safe_args(fake_workspace):
    """``continue_with_waiver`` is a feedback-gated choice: it advertises
    native elicitation, keeps feedback out of ``args``, and points at the
    resume followup (it advances the run like ``continue``)."""
    _write_handoff_run(
        fake_workspace, available_actions=["continue_with_waiver"],
    )
    r = await _watch_for_handoff(_HANDOFF_RUN_ID)
    assert r.handoff is not None
    [choice] = r.handoff.choices
    assert choice.action == "continue_with_waiver"
    assert choice.requires_feedback is True
    assert choice.feedback_field == "feedback"
    assert choice.feedback_placeholder  # non-empty operator-side hint
    assert choice.elicitation is not None
    assert choice.elicitation.mode == "form"
    assert choice.elicitation.client_capability == "elicitation.form"
    assert choice.elicitation.field == "feedback"
    assert choice.elicitation.requested_schema["required"] == ["feedback"]
    # args MUST NOT contain feedback — the agent/operator supplies the waiver.
    assert "feedback" not in choice.args
    assert choice.args == {
        "run_id": _HANDOFF_RUN_ID,
        "handoff_id": "validate_plan:plan_round:1",
        "action": "continue_with_waiver",
    }
    # Unlike halt, waiver is non-terminal — resume followup present.
    assert choice.followup is not None
    assert choice.followup.tool == "orcho_run_resume"
    assert choice.followup.args == {"run_id": _HANDOFF_RUN_ID}


@pytest.mark.anyio
async def test_watch_handoff_waiver_feedback_required_and_prompt(fake_workspace):
    """When the runtime offers ``continue_with_waiver`` it joins
    ``feedback_required_for`` and the prompt instructs feedback for it."""
    write_run(
        fake_workspace, "20260101_000001",
        meta={
            "project": "/p/x",
            "status": "awaiting_phase_handoff",
            "task": "t",
            "phase_handoff": {
                "id": "validate_plan:plan_round:2",
                "phase": "validate_plan",
                "available_actions": [
                    "retry_feedback", "continue", "continue_with_waiver", "halt",
                ],
                "findings": [{"severity": "P1", "title": "Missing stdio smoke"}],
            },
        },
        events=[_ev(1), _ev(2)],
    )
    r = await orcho_run_watch(
        "20260101_000001", since_seq=0,
        until="handoff_or_terminal", timeout_s=5,
    )
    assert r.handoff is not None
    assert r.handoff.feedback_required_for == [
        "retry_feedback", "continue_with_waiver",
    ]
    prompt = r.handoff.recommended_user_prompt
    assert "If continue_with_waiver, provide feedback text" in prompt


@pytest.mark.anyio
async def test_handoff_choice_continue_complete_args(fake_workspace):
    """``continue`` choice has complete args and a resume followup.

    No feedback needed; ``args`` is safe to forward as-is.
    """
    _write_handoff_run(
        fake_workspace, available_actions=["continue"],
    )
    r = await _watch_for_handoff(_HANDOFF_RUN_ID)
    assert r.handoff is not None
    [choice] = r.handoff.choices
    assert choice.action == "continue"
    assert choice.requires_feedback is False
    assert choice.feedback_field is None
    assert choice.feedback_placeholder is None
    assert choice.elicitation is None
    assert choice.args == {
        "run_id": _HANDOFF_RUN_ID,
        "handoff_id": "validate_plan:plan_round:1",
        "action": "continue",
    }
    # Followup is resume.
    assert choice.followup is not None
    assert choice.followup.tool == "orcho_run_resume"


@pytest.mark.anyio
async def test_handoff_choice_halt_terminal_no_followup(fake_workspace):
    """``halt`` is terminal — no followup, no feedback needed."""
    _write_handoff_run(
        fake_workspace, available_actions=["halt"],
    )
    r = await _watch_for_handoff(_HANDOFF_RUN_ID)
    assert r.handoff is not None
    [choice] = r.handoff.choices
    assert choice.action == "halt"
    assert choice.requires_feedback is False
    assert choice.feedback_field is None
    assert choice.feedback_placeholder is None
    assert choice.elicitation is None
    assert choice.followup is None
    assert choice.args == {
        "run_id": _HANDOFF_RUN_ID,
        "handoff_id": "validate_plan:plan_round:1",
        "action": "halt",
    }
