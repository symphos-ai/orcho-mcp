"""orcho_mcp.services.delivery_gate — typed delivery / correction gate read.

``project_delivery_gate(run_id)`` turns an Orcho-managed run's persisted
post-release delivery state into a typed :class:`DeliveryGateProjection`
so a control-loop client never parses terminal prose to tell an
Orcho-managed delivery gate apart from a direct checkout edit.

Authority contract (see ``docs/architecture/delivery_gate_projection.md``):

- The gate ``kind`` and available actions come from orcho-core's SDK
  ``delivery_decision_state(run_id, cwd=None)`` surface. MCP does not
  re-implement delivery policy or decide which actions hard guards allow.
- The durable ``commit_decisions/<id>.json`` artifact and ``diff.patch``
  only *enrich* the diff summary. When either is missing or unreadable on a
  pending gate, the kind is preserved, ``diff.degraded`` is set, the changed
  paths fall back to meta (then the patch), and the message names the
  missing artifact — the gate is never collapsed into
  ``direct_checkout_or_running`` because a secondary artifact failed.
- MCP reads durable artifacts for context; the only mutating path it exposes
  is the SDK-backed ``orcho_delivery_decide`` tool. ``next_actions`` therefore
  emits one ready-to-forward call per SDK-available action.
"""
from __future__ import annotations

from typing import Any, NamedTuple

from sdk import delivery_decision_state as _sdk_delivery_decision_state

from orcho_mcp.schemas import (
    DeliveryActionRecord,
    DeliveryGateDiffSummary,
    DeliveryGateProjection,
    NextActionRecord,
)
from orcho_mcp.schemas.inspection import PrIntentRecord
from orcho_mcp.services.errors import map_sdk_errors
from orcho_mcp.services.run_artifacts import (
    get_run_commit_decision_raw,
    get_run_diff_patch,
    get_run_meta_raw,
)

_KIND_DELIVERY = "delivery_decision_required"
_KIND_CORRECTION = "correction_decision_required"
_KIND_DIRECT = "direct_checkout_or_running"
# A terminal, already-executed Orcho-managed delivery (the diff landed in the
# target checkout). NOT ``direct_checkout_or_running`` — that reads as "no Orcho
# delivery happened / still running"; this reads as "the delivery ran and
# completed". No delivery decision is offered; the real PR link (when any) rides
# in ``pr_url``.
_KIND_COMPLETED = "delivery_completed"

# Statuses on which an Orcho-managed delivery is considered to have landed in the
# target checkout. Kept aligned with the evidence slice's
# ``inspection.evidence._DELIVERY_APPLIED_STATUSES`` (single vocabulary): a new
# commit (``committed``) or an uncommitted apply (``applied_uncommitted``).
_DELIVERY_COMPLETED_STATUSES = frozenset({"committed", "applied_uncommitted"})

# Typed delivery-scope violation reason (mirrors core ``delivery_scope`` /
# ``sdk.run_control.delivery``). When the SDK delivery-decision state reports
# this, shipping is refused because the run resolved under strict mono but
# sibling-repo changes were found outside its scope.
_DELIVERY_SCOPE_VIOLATION = "delivery_scope_violation"

# Static action catalog. ``creates_commit`` is the load-bearing flag: only
# ``approve`` writes a new commit to the target checkout.
_ACTION_CATALOG: dict[str, tuple[str, bool]] = {
    "approve": (
        "Commit the retained worktree diff into the target checkout as a new "
        "commit.",
        True,
    ),
    "apply": (
        "Apply the retained worktree diff to the target checkout working tree "
        "without committing it.",
        False,
    ),
    "fix": (
        "Send the change back for another correction round to address the "
        "rejected release verdict.",
        False,
    ),
    "skip": (
        "Leave the target checkout untouched and discard the pending delivery "
        "without committing.",
        False,
    ),
    "halt": (
        "Halt the run, leaving the retained worktree and diff in place for "
        "manual inspection.",
        False,
    ),
}


def _coerce_str_list(value: Any) -> list[str]:
    """Coerce a meta/artifact list field to ``list[str]``; non-lists → ``[]``."""
    if not isinstance(value, list):
        return []
    return [str(x) for x in value if x is not None]


def _optional_str(value: Any) -> str | None:
    """Coerce a value to a non-empty ``str``, else ``None``."""
    if value is None:
        return None
    s = str(value)
    return s or None


def _extract_commit_delivery(meta: dict) -> dict | None:
    """Resolve the persisted commit-delivery decision dict from meta.

    The session dict is persisted directly as ``meta.json`` (orcho-core's
    ``save_session``), so the decision lives at the top level
    (``meta['commit_delivery']``) exactly like ``meta['phase_handoff']``. A
    legacy nested shape (``meta['session']['commit_delivery']``) is accepted
    as a defensive fallback. Anything not a dict is treated as absent.
    """
    raw = meta.get("commit_delivery") if isinstance(meta, dict) else None
    if not isinstance(raw, dict):
        session = meta.get("session") if isinstance(meta, dict) else None
        raw = session.get("commit_delivery") if isinstance(session, dict) else None
    return raw if isinstance(raw, dict) else None


def _extract_status(cd: dict | None) -> str | None:
    """Authoritative persisted status from the meta commit-delivery dict."""
    if cd is None:
        return None
    return _optional_str(cd.get("status"))


def _map_release(cd: dict | None) -> str:
    """Map ``release_verdict`` to the wire release outcome."""
    if cd is None:
        return "none"
    verdict = str(cd.get("release_verdict") or "").strip().upper()
    if verdict == "APPROVED":
        return "approved"
    if verdict == "REJECTED":
        return "rejected"
    return "none"


def _extract_delivery_branch(cd: dict | None) -> str | None:
    """Published / publishable delivery branch from the meta decision (ADR 0119).

    Reads the authoritative ``meta['commit_delivery'].delivery_branch`` (core's
    ``CommitDeliveryDecision.to_dict`` only emits the key for a branch-policy
    delivery). Defensive to ``None`` / non-dict / absent key: never fabricated,
    absent → ``None``.
    """
    if not isinstance(cd, dict):
        return None
    return _optional_str(cd.get("delivery_branch"))


def _map_pr_intent(cd: dict | None) -> PrIntentRecord | None:
    """Map the durable ``pr_intent`` block to a typed :class:`PrIntentRecord`.

    Mirrors core's ``DeliveryPrIntent.to_dict`` shape (nested under the
    ``pr_intent`` key of ``CommitDeliveryDecision.to_dict``): ``branch`` /
    ``base`` / ``title`` / ``suggested_command``. Defensive to
    ``None`` / non-dict at both levels — a missing or malformed block yields
    ``None`` (never a fabricated record); each field is coerced via
    ``_optional_str``.
    """
    if not isinstance(cd, dict):
        return None
    raw = cd.get("pr_intent")
    if not isinstance(raw, dict):
        return None
    return PrIntentRecord(
        branch=_optional_str(raw.get("branch")),
        base=_optional_str(raw.get("base")),
        title=_optional_str(raw.get("title")),
        suggested_command=_optional_str(raw.get("suggested_command")),
    )


def _extract_pr_url(cd: dict | None) -> str | None:
    """Published pull-request URL from the meta decision (ADR 0119).

    Reads the authoritative ``meta['commit_delivery'].pr_url`` (core's
    ``CommitDeliveryDecision.to_dict`` always keys ``pr_url`` — the value when a
    PR was opened, ``None`` otherwise — so a projection reads it without
    re-parsing ``delivery_notices``). Defensive to ``None`` / non-dict / absent
    key: never fabricated, absent → ``None``. A stale core with no ``pr_url``
    key reads as absence.
    """
    if not isinstance(cd, dict):
        return None
    return _optional_str(cd.get("pr_url"))


def _extract_delivery_notices(cd: dict | None) -> list[str]:
    """Human-readable delivery notices from the meta decision (ADR 0119).

    Reads ``meta['commit_delivery'].delivery_notices`` (core only emits the key
    when non-empty). Defensive to ``None`` / non-dict / absent / non-list:
    coerced through :func:`_coerce_str_list`, so absence → ``[]`` (never
    fabricated).
    """
    if not isinstance(cd, dict):
        return []
    return _coerce_str_list(cd.get("delivery_notices"))


def _published_pr_intent(
    pr_intent: PrIntentRecord | None, published: bool,
) -> PrIntentRecord | None:
    """Project the PR intent for a completed delivery.

    On a published delivery (a PR is already open — its live link is
    ``pr_url``) the durable ``suggested_command`` is a stale, misleading "run
    this to open a PR" instruction, so it is dropped (``None``). The rest of the
    intent (branch / base / title) is preserved. On an unpublished delivery the
    intent passes through unchanged.
    """
    if pr_intent is None or not published:
        return pr_intent
    return pr_intent.model_copy(update={"suggested_command": None})


def _parse_patch_files(diff_text: str | None) -> list[str] | None:
    """Parse changed file paths from a unified ``diff.patch`` body.

    Returns ``None`` when the content is non-empty yet carries no
    recognizable diff structure (a corrupt / unreadable patch), ``[]`` for
    an empty (clean) patch, else the ordered distinct changed paths. Used
    only to enrich the diff summary and to detect a corrupt secondary
    artifact — never to classify the gate.
    """
    if diff_text is None:
        return None
    if not diff_text.strip():
        return []
    paths: list[str] = []
    seen: set[str] = set()
    found_header = False
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            found_header = True
            parts = line.split(" b/", 1)
            if len(parts) == 2:
                p = parts[1].strip()
                if p and p not in seen:
                    seen.add(p)
                    paths.append(p)
        elif line.startswith("+++ "):
            found_header = True
            p = line[4:].strip()
            if p.startswith("b/"):
                p = p[2:]
            if p and p != "/dev/null" and p not in seen:
                seen.add(p)
                paths.append(p)
    if not found_header:
        return None
    return paths


def _safe_read_diff_patch(run_id: str) -> str | None:
    """Read ``diff.patch`` defensively; ``None`` on missing / any read error."""
    try:
        return get_run_diff_patch(run_id)
    except Exception:
        return None


def _safe_read_commit_decision(run_id: str) -> dict | None:
    """Read the commit-decision artifact defensively; ``None`` on any error."""
    try:
        return get_run_commit_decision_raw(run_id)
    except Exception:
        return None


def _build_diff_summary(
    run_id: str, cd: dict | None,
) -> tuple[DeliveryGateDiffSummary, list[str]]:
    """Build the gate diff summary and the list of degraded secondary artifacts.

    Primary source is the authoritative meta decision (``changed_paths`` /
    ``untracked_paths``). The ``commit_decisions`` artifact and ``diff.patch``
    only enrich it; a missing / corrupt one degrades the summary (and is
    named in the returned list) but never changes the gate kind.
    """
    changed_paths = _coerce_str_list(cd.get("changed_paths")) if cd else []
    untracked_paths = _coerce_str_list(cd.get("untracked_paths")) if cd else []

    missing: list[str] = []

    # diff.patch — enrichment + corruption signal.
    diff_text = _safe_read_diff_patch(run_id)
    if diff_text is None:
        missing.append("diff.patch")
        parsed_patch: list[str] | None = None
    else:
        parsed_patch = _parse_patch_files(diff_text)
        if parsed_patch is None:
            missing.append("diff.patch (unreadable)")

    # commit_decisions/<id>.json — enrichment + presence signal.
    decision = _safe_read_commit_decision(run_id)
    if decision is None:
        missing.append("commit_decisions")

    # Changed paths come from meta first; fall back to the audit artifact's
    # staged files, then to the parsed patch, when meta carries none.
    if not changed_paths and decision is not None:
        changed_paths = _coerce_str_list(decision.get("files_staged"))
    if not changed_paths and parsed_patch:
        changed_paths = list(parsed_patch)

    summary = DeliveryGateDiffSummary(
        files_changed=len(changed_paths),
        changed_paths=changed_paths,
        untracked_paths=untracked_paths,
        degraded=bool(missing),
    )
    return summary, missing


def _available_actions(names: tuple[str, ...]) -> list[DeliveryActionRecord]:
    """Wire action records for SDK-available action names."""
    out: list[DeliveryActionRecord] = []
    for name in names:
        if name not in _ACTION_CATALOG:
            continue
        effect, creates_commit = _ACTION_CATALOG[name]
        out.append(
            DeliveryActionRecord(
                action=name, effect=effect, creates_commit=creates_commit,
            ),
        )
    return out


def _ready_next_actions(run_id: str, names: tuple[str, ...]) -> list[NextActionRecord]:
    """Ready-to-forward ``orcho_delivery_decide`` calls for available actions."""
    records: list[NextActionRecord] = []
    for name in names:
        if name not in _ACTION_CATALOG:
            continue
        effect, _creates_commit = _ACTION_CATALOG[name]
        records.append(
            NextActionRecord(
                intent=f"{effect} Uses orcho-core's delivery decision API.",
                tool="orcho_delivery_decide",
                args={"run_id": run_id, "action": name},
                optional=True,
                kind="ready_call",
            ),
        )
    return records


def held_diff_path(run_id: str) -> str | None:
    """Absolute path to the run's retained ``diff.patch``, or ``None`` if absent.

    Read defensively (the retained diff is recovery *context*, never required):
    any lookup / stat failure degrades to ``None`` so a missing patch never
    breaks a projection.
    """
    try:
        from orcho_mcp.services.run_lookup import find_run_dir

        patch = find_run_dir(run_id) / "diff.patch"
        return str(patch) if patch.is_file() else None
    except Exception:
        return None


def is_correction_followup_state(
    state_kind: str, available_action_names: tuple[str, ...] | list[str],
) -> bool:
    """True for a correction gate whose ``fix`` is already decided / dead-ended.

    orcho-core's ``delivery_decision_state`` returns ``kind='correction'``
    with ``fix`` no longer available once the correction was requested (or the run
    auto-refused a rejected release): repeating ``fix`` is inert and the real next
    step is a from_run_plan follow-up. A freshly defer-parked rejected gate still
    offers ``fix`` (the actionable operator decision) and is NOT this state.
    """
    return state_kind == "correction" and "fix" not in available_action_names


def build_followup_next_action(
    run_id: str,
    project_dir: str | None,
    diff_path: str | None,
    retained_worktree: str | None = None,
) -> NextActionRecord:
    """Ready ``orcho_run_start`` from_run_plan follow-up carrying the held diff.

    The actionable step for a correction whose ``fix`` was already requested (or a
    rejected dead-end) is a NEW run that carries the parent's plan forward via
    ``from_run_plan``. The parent's retained ``diff.patch`` and its
    checkout context are NOT ``orcho_run_start`` parameters, so they never ride in
    ``args`` (which stays forwardable verbatim, holding only real tool
    parameters). They are published instead as the typed, machine-readable
    ``context`` block — ``from_run_plan`` / ``diff_path`` / ``project_dir`` /
    ``retained_worktree`` — so a typed client reads the diff/worktree pointers
    from structured keys, never from the ``intent`` prose.

    Shared by the delivery-gate projection and the run-diagnosis wire so both
    surfaces emit a byte-identical typed action.
    """
    diff_ctx = (
        f" The parent's retained diff is at {diff_path}." if diff_path else ""
    )
    args: dict[str, Any] = {"from_run_plan": run_id, "profile": "feature"}
    if project_dir:
        args["project_dir"] = project_dir
    context: dict[str, Any] = {"from_run_plan": run_id}
    if diff_path:
        context["diff_path"] = diff_path
    if project_dir:
        context["project_dir"] = project_dir
    if retained_worktree:
        context["retained_worktree"] = retained_worktree
    return NextActionRecord(
        intent=(
            f"Start a from_run_plan follow-up of run {run_id} to deliver the "
            "requested correction: it carries the parent's plan forward as a "
            f"fresh run.{diff_ctx} A bare resume or a repeated fix is inert."
        ),
        tool="orcho_run_start",
        args=args,
        optional=False,
        kind="ready_call",
        context=context,
    )


def _gate_kind_from_state(kind: str) -> str:
    """Map SDK state kind to the MCP projection kind."""
    if kind == "delivery":
        return _KIND_DELIVERY
    if kind == "correction":
        return _KIND_CORRECTION
    return _KIND_DIRECT


def _gate_message(
    kind: str,
    missing: list[str],
    state_reason: str | None,
) -> str | None:
    """Compose the human-readable gate message.

    Names the missing / unreadable secondary artifact(s) when the diff
    summary degraded; the gate kind is explicitly unaffected.
    """
    reason = f" Core guard reason: {state_reason}." if state_reason else ""
    if missing:
        return (
            "Delivery gate diff summary is degraded — missing or unreadable "
            f"secondary artifact(s): {', '.join(missing)}. The gate kind is "
            "unaffected: it is classified by orcho-core's delivery decision "
            "state. Resolve the decision with orcho_delivery_decide; do not "
            f"apply the retained diff manually.{reason}"
        )
    if kind == _KIND_CORRECTION:
        return (
            "Orcho-managed correction gate: the release was rejected and a "
            "correction decision is required. Resolve it with "
            f"orcho_delivery_decide; do not apply the retained diff manually.{reason}"
        )
    return (
        "Orcho-managed delivery gate is pending an operator decision. Resolve "
        f"it with orcho_delivery_decide; do not apply the retained diff manually.{reason}"
    )


def _superseded_child(meta: dict) -> str | None:
    """Child run id from a durable ``superseded_by_followup`` marker, else None.

    orcho-core's finalization stamps ``superseded_by_followup`` on a
    rejected-FA / correction parent once a from_run_plan follow-up child has
    delivered, settling the parent to ``done`` and evicting its phantom gate.
    """
    marker = meta.get("superseded_by_followup") if isinstance(meta, dict) else None
    if isinstance(marker, dict):
        return _optional_str(marker.get("child_run_id"))
    return None


def _direct_message(
    cd: dict | None, status: str | None, superseded_child: str | None = None,
) -> str:
    """Message for a non-gate (direct checkout / running / terminal) run."""
    if superseded_child:
        return (
            "This run was superseded by a successful from_run_plan follow-up "
            f"({superseded_child}); its correction is closed and no delivery "
            "gate is pending. Inspect the follow-up child for the delivered "
            "change — do not act on this parent's old release blockers."
        )
    if cd is None:
        return (
            "No pending Orcho commit-delivery gate in run meta — this is a "
            "direct checkout edit or a still-running / non-delivery run. Test "
            "and commit the checkout directly."
        )
    if status:
        return (
            f"Orcho commit-delivery is terminal (status={status!r}); the gate "
            "is closed. Inspect the checkout directly — there is no pending "
            "delivery decision."
        )
    return (
        "No pending Orcho commit-delivery decision in run meta; treat this as "
        "a direct checkout."
    )


def _completed_message(status: str | None, pr_url: str | None) -> str:
    """Message for a run whose Orcho-managed delivery already landed.

    Announces that the delivery ran and completed (naming the open PR when
    there is one) so a client never mistakes this terminal state for a direct
    checkout edit. It never advises testing / committing the checkout by hand —
    the delivery already happened.
    """
    if pr_url:
        return (
            f"Orcho-managed delivery already landed (status={status!r}) and a "
            f"pull request is open: {pr_url}. The delivery gate is closed — "
            "there is no decision to make and nothing to commit by hand. Follow "
            "the pull request to review or merge the published change."
        )
    return (
        f"Orcho-managed delivery already landed (status={status!r}); the change "
        "was delivered to the target checkout. The delivery gate is closed — "
        "there is no decision to make. Inspect the delivered commit or the run "
        "evidence; do not re-apply the retained diff by hand."
    )


def project_delivery_gate(run_id: str) -> DeliveryGateProjection:
    """Project a run's post-release delivery / correction gate.

    Reads orcho-core's authoritative ``delivery_decision_state`` for gate kind
    and action availability, then enriches the projection from durable
    artifacts (``meta.json``, ``commit_decisions``, ``diff.patch``). Secondary
    artifact failures degrade the diff summary but never hide a decidable gate.
    """
    with map_sdk_errors(run_id):
        state = _sdk_delivery_decision_state(run_id, cwd=None)

    meta = get_run_meta_raw(run_id) or {}
    if not isinstance(meta, dict):
        meta = {}

    cd = _extract_commit_delivery(meta)
    status = _extract_status(cd)
    release = _map_release(cd)
    delivery_branch = _extract_delivery_branch(cd)
    pr_intent = _map_pr_intent(cd)

    if not state.decidable:
        superseded_child = _superseded_child(meta)
        # A terminal, already-executed delivery (``committed`` /
        # ``applied_uncommitted``) is NOT "nothing happened / still running":
        # the diff landed. Surface the distinct ``delivery_completed`` kind with
        # the published PR facts, so a client reads the real outcome instead of
        # a misleading ``direct_checkout_or_running``. A superseded parent keeps
        # the direct/superseded message (its own decision was evicted).
        if superseded_child is None and status in _DELIVERY_COMPLETED_STATUSES:
            pr_url = _extract_pr_url(cd)
            published = bool(pr_url)
            return DeliveryGateProjection(
                run_id=run_id,
                kind=_KIND_COMPLETED,
                release=release,
                target_checkout=_target_checkout(meta, cd),
                retained_worktree=_retained_worktree(meta, cd),
                diff=DeliveryGateDiffSummary(),
                default_action=None,
                available_actions=[],
                blocked_actions=[],
                published=published,
                pr_url=pr_url,
                delivery_notices=_extract_delivery_notices(cd),
                delivery_branch=delivery_branch,
                pr_intent=_published_pr_intent(pr_intent, published),
                message=_completed_message(status, pr_url),
                next_actions=[],
            )
        return DeliveryGateProjection(
            run_id=run_id,
            kind=_KIND_DIRECT,
            release=release,
            target_checkout=_target_checkout(meta, cd),
            retained_worktree=_retained_worktree(meta, cd),
            diff=DeliveryGateDiffSummary(),
            default_action=None,
            available_actions=[],
            blocked_actions=[],
            delivery_branch=delivery_branch,
            pr_intent=pr_intent,
            message=_direct_message(cd, status, superseded_child),
            next_actions=[],
        )

    kind = _gate_kind_from_state(state.kind)
    diff_summary, missing = _build_diff_summary(run_id, cd)
    available_action_names = tuple(str(a) for a in state.available_actions)
    scope_disclosure, scope_blocker = _scope_fields(state)

    # Correction-followup contract: a correction whose ``fix`` was already requested (only ``halt``
    # left) is no longer a "choose a delivery decide" gate — the actionable next
    # step is a from_run_plan follow-up carrying the retained diff. Surface that
    # typed ``orcho_run_start`` action FIRST, ahead of the residual ``halt``
    # decide call, so the gate never advertises an inert fix repeat.
    next_actions = _ready_next_actions(run_id, available_action_names)
    if is_correction_followup_state(state.kind, available_action_names):
        next_actions = [
            build_followup_next_action(
                run_id,
                _target_checkout(meta, cd),
                held_diff_path(run_id),
                _retained_worktree(meta, cd),
            ),
            *next_actions,
        ]

    return DeliveryGateProjection(
        run_id=run_id,
        kind=kind,
        release=release,
        target_checkout=_target_checkout(meta, cd),
        retained_worktree=_retained_worktree(meta, cd),
        diff=diff_summary,
        default_action=state.default_action,
        available_actions=_available_actions(available_action_names),
        blocked_actions=[str(a) for a in state.blocked_actions],
        scope_blocker=scope_blocker,
        scope_disclosure=scope_disclosure,
        delivery_branch=delivery_branch,
        pr_intent=pr_intent,
        message=_gate_message(kind, missing, state.reason),
        next_actions=next_actions,
    )


def _scope_fields(state: Any) -> tuple[list[str], str | None]:
    """Per-alias sibling disclosure + the typed scope blocker from SDK state.

    Both are read defensively (``getattr``) so a core that predates the
    delivery-scope axis simply yields ``([], None)`` — no MCP-side fabrication.
    ``scope_blocker`` is ``delivery_scope_violation`` when the SDK reason names
    it (the authoritative signal); the disclosure carries the concrete
    ``[alias]/rel`` sibling files behind that block.
    """
    raw = getattr(state, "scope_disclosure", None)
    disclosure = (
        [str(p) for p in raw if isinstance(p, str) and p]
        if isinstance(raw, (list, tuple)) else []
    )
    reason = getattr(state, "reason", None)
    blocker = (
        _DELIVERY_SCOPE_VIOLATION
        if isinstance(reason, str) and _DELIVERY_SCOPE_VIOLATION in reason
        else None
    )
    return disclosure, blocker


def _target_checkout(meta: dict, cd: dict | None) -> str | None:
    """Resolve the target checkout: decision ``project_path`` then meta."""
    if cd is not None:
        path = _optional_str(cd.get("project_path"))
        if path is not None:
            return path
    return _optional_str(meta.get("project"))


def _retained_worktree(meta: dict, cd: dict | None) -> str | None:
    """Resolve the retained worktree: decision ``source_path`` then meta."""
    if cd is not None:
        path = _optional_str(cd.get("source_path"))
        if path is not None:
            return path
    worktree = meta.get("worktree") if isinstance(meta, dict) else None
    if isinstance(worktree, dict):
        return _optional_str(worktree.get("path"))
    return None


class DeliveryDisposition(NamedTuple):
    """Cheap terminal delivery disposition (committed / published / pr_url).

    The light read behind a live terminal card: whether an Orcho-managed
    delivery landed (``committed`` — a ``committed`` / ``applied_uncommitted``
    status, the SAME set that classifies a ``delivery_completed`` gate),
    whether it opened a pull request (``published``), and that PR's live
    ``pr_url``. All defaults are the empty disposition so a run with no
    delivery reads ``(False, False, None)``.
    """
    committed: bool = False
    published: bool = False
    pr_url: str | None = None


def delivery_disposition(run_id: str) -> DeliveryDisposition:
    """Read a run's terminal delivery disposition cheaply and single-source.

    A light-weight sibling of :func:`project_delivery_gate` for the live
    terminal card: it reads ONLY the durable ``meta['commit_delivery']`` block
    through the same ``_extract_*`` helpers (no SDK ``delivery_decision_state``
    probe, no diff / commit-decision artifact reads), so the terminal poll never
    pays for the full gate projection.

    Defensive: a missing / unreadable meta, a non-dict meta, or an absent
    commit-delivery block all yield the empty disposition
    ``(False, False, None)`` — never an exception, never a fabricated fact.
    """
    try:
        meta = get_run_meta_raw(run_id)
    except Exception:
        return DeliveryDisposition()
    if not isinstance(meta, dict):
        return DeliveryDisposition()
    cd = _extract_commit_delivery(meta)
    committed = _extract_status(cd) in _DELIVERY_COMPLETED_STATUSES
    pr_url = _extract_pr_url(cd)
    return DeliveryDisposition(
        committed=committed, published=bool(pr_url), pr_url=pr_url,
    )


__all__ = [
    "DeliveryDisposition",
    "build_followup_next_action",
    "delivery_disposition",
    "held_diff_path",
    "is_correction_followup_state",
    "project_delivery_gate",
]
