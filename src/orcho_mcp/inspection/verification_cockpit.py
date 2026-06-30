"""orcho_mcp.inspection.verification_cockpit — cockpit projection builder.

Pure, read-only projection: :func:`build_verification_cockpit` turns the SAME
``sdk.get_verification_timeline`` :class:`VerificationTimelineProjection` that
already feeds the ``verification_timeline`` slice into a typed
:class:`VerificationCockpit`. It performs NO SDK call of its own — the caller
(``inspection/evidence.py``) reads the projection once and feeds both slices,
so the cockpit augments the timeline without a second read and without changing
the timeline's shape.

``trigger`` is derived deterministically here (never read from core); see the
per-row docstring in ``schemas/inspection.py`` for the exact rule. ``gate_class``
provenance is honest: ``class_source='core'`` ONLY when a durable core gate field
supplies the class — currently the SDK gate carries none, so manual/operator-only
rows derive a class with ``class_source='derived'`` and everything else stays
``'unspecified'``. ``mode`` is ``None`` because ``work_mode`` is not yet part of
the timeline projection; if a connected core grows a mode field on ``proj`` it
should be threaded through here instead of ``None``.
"""
from __future__ import annotations

from typing import Any

from orcho_mcp.schemas import (
    VerificationCockpit,
    VerificationGateCockpitRow,
)


def _optional_str(value: Any) -> str | None:
    """Coerce an SDK breadcrumb field to a non-empty str, else ``None``.

    Mirrors ``inspection.evidence._optional_str`` so the cockpit collapses the
    SDK's empty-string sentinels (``env`` / ``hook`` / ``receipt_path`` /
    ``source_run_id`` / ``stale_reason``) to ``None`` exactly like the timeline.
    """
    if value is None:
        return None
    s = str(value)
    return s or None


def _autorun_commands(proj: Any) -> set[str]:
    """Commands the run's automation acted on across all autorun events.

    The union of every event's ``ran_pass`` / ``ran_fail`` / ``skipped_fresh``;
    ``skipped_manual`` is deliberately excluded — an intentionally not-auto-run
    command is not evidence of automation acting on the gate.
    """
    acted: set[str] = set()
    for e in proj.autorun_events:
        acted.update(str(c) for c in e.ran_pass)
        acted.update(str(c) for c in e.ran_fail)
        acted.update(str(c) for c in e.skipped_fresh)
    return acted


def _derive_trigger(
    command: str,
    policy: str | None,
    manual_only: set[str],
    acted: set[str],
) -> str:
    """Deterministic trigger classification — see VerificationGateCockpitRow.

    ``operator_only`` when the command is manual-only (membership or policy);
    else ``auto`` when the automation acted on it; else ``manual``.
    """
    if command in manual_only or policy == "manual_only":
        return "operator_only"
    if command in acted:
        return "auto"
    return "manual"


def _derive_class(trigger: str, gate_class: str | None) -> tuple[str | None, str]:
    """Return ``(gate_class, class_source)`` with honest provenance.

    ``'core'`` only when the SDK gate supplied a class (it currently does not).
    Otherwise an operator-only gate derives ``'operator'`` and a manual gate
    ``'expensive'`` with ``class_source='derived'``; an auto gate has no class
    signal so it stays ``(None, 'unspecified')``.
    """
    if gate_class:
        return gate_class, "core"
    if trigger == "operator_only":
        return "operator", "derived"
    if trigger == "manual":
        return "expensive", "derived"
    return None, "unspecified"


def _policy_summary(policies: list[str], has_contract: bool) -> tuple[str, str]:
    """Aggregate gate policies into ``(policy_summary, effect)``.

    Deterministic precedence: ``require`` dominates, then ``warn``; any other
    policy-bearing contract (``suggest``, ``manual_only``, operator-only, …)
    still has gates, so it folds to ``suggest`` rather than ``none``. ``none``
    is reserved for a contract-less projection or one with no policy-bearing
    gate, so a manual-only-only contract never reports "no verification gates"
    while also listing gate rows (requirement F2). ``effect`` is the
    human-readable consequence of that aggregate.
    """
    if not has_contract or not policies:
        return "none", "no verification gates"
    if "require" in policies:
        return "require", "blocks delivery on missing/failed receipts"
    if "warn" in policies:
        return "warn", "warn on missing/failed receipts"
    return "suggest", "suggests rerun on missing/failed receipts"


def build_verification_cockpit(proj: Any) -> VerificationCockpit:
    """Build a :class:`VerificationCockpit` from an SDK timeline projection.

    Pure function over the already-fetched
    ``sdk.get_verification_timeline`` projection — no SDK call, no file read.
    ``has_contract`` is threaded EXPLICITLY from ``proj.has_contract`` (never
    left on the model default), so the cockpit can report a genuinely absent
    contract without falsely claiming one is missing (requirement F1).
    """
    manual_only = {str(c) for c in proj.manual_only}
    acted = _autorun_commands(proj)
    # Fallback policy lookup keyed by command, for gates whose own ``policy``
    # field is empty but the readiness summary still records one.
    policy_by_command = {str(c): str(p) for c, p in proj.policy_by_command}
    # Environment names from the readiness env-status pairs (name, ok).
    envs = [str(name) for name, _ok in proj.env_statuses]

    gates: list[VerificationGateCockpitRow] = []
    seen_policies: list[str] = []
    for g in proj.gates:
        policy = _optional_str(g.policy) or policy_by_command.get(str(g.command))
        trigger = _derive_trigger(str(g.command), policy, manual_only, acted)
        # The SDK gate carries no durable class today; read defensively so a
        # future core field is honoured as 'core' without a code change here.
        raw_class = _optional_str(getattr(g, "gate_class", None))
        gate_class, class_source = _derive_class(trigger, raw_class)
        if policy:
            seen_policies.append(policy)
        gates.append(VerificationGateCockpitRow(
            command=str(g.command),
            hook=_optional_str(g.hook),
            trigger=trigger,
            policy=policy,
            # required reflects the EFFECTIVE policy, not raw ``g.required``:
            # the SDK gate's ``required`` flag means "member of contract.required",
            # so a manual_only/operator-only command from a required contract
            # would otherwise read as a blocking gate. Only a ``require`` policy
            # is genuinely blocking here (requirement F1).
            required=(policy == "require"),
            gate_class=gate_class,
            class_source=class_source,
            status=g.status,
            env=_optional_str(g.env),
            receipt_path=_optional_str(g.receipt_path),
            inherited=bool(g.inherited),
            source_run_id=_optional_str(g.source_run_id),
            stale_reason=_optional_str(g.stale_reason),
            rerun_hint=[str(h) for h in g.rerun_hint],
            # Environment-provenance note: populated when a broken
            # verification_environment receipt downgrades the gate to FAIL. The
            # ``getattr`` default tolerates a version-skewed core without it.
            detail=_optional_str(getattr(g, "detail", None)),
        ))

    has_contract = bool(proj.has_contract)
    policy_summary, effect = _policy_summary(seen_policies, has_contract)

    return VerificationCockpit(
        run_id=str(proj.run_id),
        has_contract=has_contract,
        # mode stays None: work_mode is not (yet) carried by the timeline
        # projection. Thread proj's mode field through here once core adds it.
        mode=None,
        envs=envs,
        policy_summary=policy_summary,
        effect=effect,
        gates=gates,
        residual_missing=[str(c) for c in proj.residual_missing],
        residual_stale=[str(c) for c in proj.residual_stale],
        residual_failed=[str(c) for c in proj.residual_failed],
        manual_only=[str(c) for c in proj.manual_only],
        inherited=[str(c) for c in proj.inherited],
        suggested_commands=[str(c) for c in proj.suggested_commands],
    )


__all__ = ["build_verification_cockpit"]
