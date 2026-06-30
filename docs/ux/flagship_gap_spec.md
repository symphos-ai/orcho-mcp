# Flagship Gap Spec — Typed Contracts for the Open Gaps

This spec turns each **open-gap** that survived the CLI↔MCP parity matrix
(`captain_workflow_audit.md`) into a draft typed contract: a proposed tool or
resource name, request fields, a response-model sketch, and the audit scenario it
unblocks. Every contract is classified **projection-only** (a new MCP projection
of data core already produces) or **core-data-needed** (the signal does not exist
in a typed form in core SDK / run state — recorded as a core-contract gap, *not*
implemented here).

No production source or schema is modified by this document. Response sketches are
illustrative pseudo-models, not committed Pydantic.

## Source of the gaps

The open gaps are the rows tagged `OUTSIDE` the eight run-control parity areas in
`captain_workflow_audit.md` (they are not closed by Stage 7C), grounded in the
scenario traces (S1–S9) and friction log (F1–F9):

| Gap | Audit evidence | Contract |
| --- | --- | --- |
| Resume of an already-terminal run is a silent no-op | S5a / F3 | GC-1, GC-2 |
| Delivery-gate verdict + committed state only in raw `meta.commit_delivery` | S8 / F1 | GC-1, GC-3 |
| `commit_delivery_failed` cause is log-only (status token only is structured) | S6 / F5 | GC-1, GC-4 |
| Findings resolved-vs-active has no typed surface | S8 / F9 | GC-9 |
| No mock knob for a rejecting delivery gate | S8 / F2 | GC-5 |
| Non-git `project_dir` → opaque `interrupted`, empty typed errors | S9 / F6 | GC-6 |
| Multiple/nested git-repo disambiguation has no MCP surface | T2 matrix | GC-6 |
| `advice` / `retry_with_advice` advisory actions absent | S2 / S9 | GC-7 |
| Official verification gate timeline exists in CLI output but not typed MCP | verification gate UX audit | GC-10 |
| Handoff advice evidence/ROI is raw-only | advice evidence audit | GC-11 |
| Advisory `small_task` critique and allowed companion changes are not typed for captains | small-task / plan-contract audit | GC-12 |
| Correction route, fixed-point, and scope-expansion evidence need captain projection | correction UX audit | GC-13 |
| Long-running watch calls can outlive client transport limits | observation-loop dogfood | GC-14 |

Three rows the matrix marks **covered-by-7C** are *inside* Stage 7C and are
intentionally **not respecified here** — they are Stage 7C completions, not new
contracts: the follow-up active-child recommendation for MCP-created children
(A4 — `from_run_plan` stamps `plan_source="run"` not `resume_mode="followup"`,
S5c/F4), the blocked-worktree fields not being rechecked against disk (A5 —
stale worktree stays `blocked=false`, S5b/F8), and provider-session-fallback
population (A6 — not observable under mock).

---

## GC-1 — `orcho_run_diagnose` (tool): typed run condition + ready next-actions

**Confirms** the "run-diagnosis with a typed condition + ready-to-call
next_actions" candidate — **reformed**. The candidate's original condition list
(`active / paused / halted / obsolete / stale-worktree / needs-decision /
delivery-ready / already-delivered`) over-claims: several states are *already*
typed today (`meta.status` already distinguishes `running` / `done` / `halted` /
`failed` / `awaiting_phase_handoff`; `worktree_continuity.blocked` already flags a
stale worktree — T2 area A5). The reformed contract keeps only the conditions the
audit proved are **not** derivable from one typed read.

```
tool orcho_run_diagnose(run_id: str) -> RunDiagnosis

RunDiagnosis:
  run_id: str
  condition: Literal[
    "active",                 # running, nothing required
    "needs_decision",         # awaiting_phase_handoff (already typed; echoed for completeness)
    "halted", "failed",
    "resume_inert_terminal",  # NET-NEW: run is terminal; a resume call would be a no-op (S5/F3)
    "superseded_by_child",    # NET-NEW: an unfinished follow-up child exists; resume the child
    "delivery_ready",         # NET-NEW: implement done, delivery/commit not yet applied (S8)
    "already_delivered",      # NET-NEW: delivery/commit applied
    "blocked_worktree",       # echo of worktree_continuity.blocked
  ]
  reason: str                 # one-line human-readable
  next_actions: list[NextAction]   # ready-to-call: {intent, tool, args, optional}
```

- **Request fields:** `run_id`.
- **Unblocks:** S5 (terminal-resume → `resume_inert_terminal` instead of a
  misleading success), S8 (`delivery_ready` / `already_delivered` instead of
  parsing `run.end` prose), follow-up obsolescence (`superseded_by_child`).
- **Classification:** **projection-only** for `active / needs_decision / halted /
  failed / resume_inert_terminal / superseded_by_child / blocked_worktree` — all
  derivable from already-typed `meta.status`, `meta.phase_handoff`,
  `lineage.*`, and `worktree_continuity.*` (T2 areas A1/A4/A5). The
  `delivery_ready` / `already_delivered` conditions are **also projection-only**:
  S8 showed delivery state lives in the raw `meta.commit_delivery` dict
  (`status="committed"`/`"commit_failed"`), so these conditions derive from
  GC-3's projection of existing data — no new core signal required.

---

## GC-2 — safe-resume guard: typed `resume_outcome` on `orcho_run_resume`

**Confirms** the "stale/obsolete detection and safe-resume guidance" candidate.
The audit proved the concrete failure: `orcho_run_resume` on a `halted` run
returns a success-shaped `RunStartedResult` (fresh pid, `command` with `--resume`,
`next_actions=[]`) yet the run stays `halted` — no typed signal that the resume was
inert (S5 / F3).

```
# additive field on the existing orcho_run_resume result
RunResumeResult(RunStartedResult):
  resume_outcome: Literal[
    "applied",              # checkpoint loaded, a fresh subprocess is advancing the run
    "rejected_terminal",    # run is already done/halted/failed — nothing to resume
    "pending_decision",     # active handoff with no decision artifact (already handled today)
    "superseded_by_child",  # an unfinished follow-up child should be resumed instead
  ]
  message: str
  suggested_next_action: NextAction | None
```

- **Request fields:** unchanged (`run_id`, optional `profile`).
- **Unblocks:** S5 — a client can tell "resume took effect" from "resume was a
  no-op against a terminal run" without diffing pre/post status (F3).
- **Classification:** **projection-only.** Terminality (`meta.status`) and lineage
  (`lineage.has_active_child_followup`) are already typed; the guard is a
  pre-flight check the resume path can return instead of spawning. The
  `pending_decision` arm already exists in the lifecycle (T1 S2 confirmed the
  structured pending-decision response).

---

## GC-3 — typed delivery-gate verdict

**Confirms** the verdict half of the "delivery/recovery guidance" candidate,
**reshaped by S8 evidence**. The delivery-gate verdict and committed/applied state
are **already structured in core** — `meta.commit_delivery` carries
`status` (`"committed"` / `"commit_failed"`), `action` (`"approve"`),
`release_verdict` (`"APPROVED"`), `baseline_ref`, `dirty`, and `untracked_paths`
(verified on runs `20260614_234701_49a193` committed and `20260614_234654_7f113d`
commit_failed). The gap is **not** missing core data — it is that
`meta.commit_delivery` is a **raw passthrough dict**, not a typed `RunStatus`
projection (and the duplicate prose paths — `run.end` summary,
`final_acceptance.critique` markdown — are the only typed-ish alternatives today).

```
# typed projection of the existing meta.commit_delivery dict on orcho_run_status
DeliveryState:
  status: Literal["committed", "commit_failed", "uncommitted", "skipped"]
  release_verdict: Literal["APPROVED", "REJECTED", "WAIVED"] | None
  action: str | None           # "approve" | ...
  baseline_ref: str | None
  dirty: bool | None
  changed_paths: list[str]      # from commit_delivery.untracked_paths
```

- **Request fields:** `run_id`.
- **Unblocks:** S8 — read committed/applied state and the delivery verdict as
  typed fields instead of dict-spelunking `meta.commit_delivery` (F1), and feeds
  GC-1 `delivery_ready` / `already_delivered`.
- **Classification:** **projection-only** — every field exists in the raw
  `meta.commit_delivery` dict; this is a typed projection of data core already
  records, no new core signal required. (This supersedes the earlier
  core-data-needed reading: S8 showed the verdict is *not* prose-only — it is a
  structured `release_verdict` already present, merely untyped at the MCP edge.)

---

## GC-4 — typed delivery-failure cause

**Confirms** the failure half of the "delivery/recovery guidance" candidate,
**reshaped by S6/S8 evidence**. A failed delivery is structured to a useful
degree: `meta.commit_delivery.status="commit_failed"` plus `final_message`,
`dirty`, and `baseline_ref` (verified on run `20260614_234654_7f113d`,
`halt_reason="commit_delivery_failed"`). So the *state* is projectable; only the
granular root cause (which git operation failed and why) is not separately
structured beyond `status` + `final_message`.

```
# additive on the typed DeliveryState (GC-3) / orcho_run_evidence(slice="errors")
DeliveryFailure:
  status: Literal["commit_failed"]      # from commit_delivery.status (structured)
  final_message: str                    # from commit_delivery.final_message
  cause_code: Literal[                  # the only NET-NEW field needing core help
    "dirty_tree", "merge_conflict", "no_changes", "commit_rejected", "unknown",
  ]
  recovery_actions: list[NextAction]
```

- **Request fields:** `run_id` (existing evidence slice call).
- **Unblocks:** S6 — diagnose a failed delivery (`status="commit_failed"` +
  `final_message`) without reading `runner.log` (F5); pairs with GC-1
  `delivery_ready` and GC-2 recovery guidance.
- **Classification:** **mostly projection-only** — `status` + `final_message` +
  `dirty` already exist in `meta.commit_delivery` and only need typing; the single
  **core-data-needed** remnant is the granular `cause_code` (which git step failed),
  which core does not record today. (The flakiness — `commit_delivery_failed` vs
  `done` on identical input — is a separate core robustness item, noted for the
  roadmap, not an MCP projection.)

---

## GC-5 — rejecting delivery-gate test affordance (`mock_final_acceptance_reject`)

**Confirms** the "no mock knob for a rejecting delivery gate" gap (S8 / F2). Core's
argv/mock generator exposes only `mock_validate_plan_reject` (plan gate); there is
no equivalent to force a blocking `final_acceptance`, so the rejecting
delivery-gate path is unreachable through the MCP `mock=true` surface.

```
# core argv flag, surfaced as an orcho_run_start parameter once core adds it
orcho_run_start(..., mock_final_acceptance_reject: int = 0)
```

- **Request fields:** integer reject-rounds, mirroring `mock_validate_plan_reject`.
- **Unblocks:** deterministic exercise of a rejecting/blocking delivery gate (and
  therefore GC-1 `delivery_ready=false` and GC-3 `verdict=REJECTED`) under mock.
- **Classification:** **core-data-needed** (a core test-affordance / argv-builder
  change). Recorded as a core-contract gap; not implemented here. MCP wiring is a
  one-line pass-through *once core provides the flag*.

---

## GC-6 — project preflight diagnostics & discovery

**Confirms** two matrix rows: the non-git `project_dir` opaque-`interrupted` case
(S9 / F6) and the multiple/nested git-repo disambiguation row. Audit evidence: the
cold-start first attempt ended `meta.status="interrupted",
halt_reason="interrupted"` with `error_summary=None, errors=[]`; the actionable
cause (`"project_dir is not a git repository … run \`git init\`"`) lived only in
`runner.log`. Core *computes* a reason internally (`pre_run_dirty.py … reason=
"project checkout is not a git repository"`; `worktree.py` raises a worktree config
error) but it never reaches the typed run state MCP reads.

```
# (a) additive typed preflight failure on the errors slice / orcho_run_status
PreflightError:
  reason_code: Literal[
    "not_a_git_repo", "no_head", "dirty_tree_blocked",
    "multiple_git_repos", "worktree_unavailable",
  ]
  message: str
  remediation: list[str]       # e.g. ["run `git init` in <project_dir>", ...]

# (b) optional non-interactive discovery surface (resource or tool)
tool orcho_project_inspect(path: str) -> ProjectResolution
ProjectResolution:
  is_git_repo: bool
  candidates: list[GitRepoCandidate]   # {path, is_nested, recommended}
  needs_git_init: bool
```

- **Request fields:** GC-6a — none new (rides `run_id` on existing reads);
  GC-6b — `path`.
- **Unblocks:** S9 — a non-git or ambiguous project surfaces a typed, actionable
  reason instead of an opaque `interrupted`; an MCP client can pre-check a project
  before `orcho_run_start` instead of discovering the failure in logs.
- **Classification:** **core-data-needed** for (a) — core must persist the
  preflight/worktree failure reason into typed run state (it is currently raised
  and logged, then collapsed to `interrupted`). **projection-only** for (b) — the
  discovery logic already exists in core (`project_discovery_prompt`); it needs a
  non-interactive entrypoint MCP can call, not new data.

---

## GC-7 — `orcho_handoff_advice` (tool): advisory-action parity

**Reforms** the `advice` / `retry_with_advice` candidate. Audit evidence (S2 / S9):
the MCP `available_actions` are exactly the four decision verbs; the CLI's
advisory actions 5 (`advice`) and 6 (`retry_with_advice`) have no MCP surface.
Core already carries a typed model — `HandoffAdvice{recommended_action,
confidence, retry_feedback, risks}` plus `AdviceSafety{auto_apply_ok,
needs_confirmation}` and persists advice artifacts (`handoff_advice_artifact`).

```
tool orcho_handoff_advice(run_id: str, handoff_id: str) -> HandoffAdviceResult
HandoffAdviceResult:
  recommended_action: Literal["continue","retry_feedback","halt","continue_with_waiver"]
  confidence: Literal["high","medium","low"]
  rationale: str
  generated_feedback: str | None      # populated when recommended_action == retry_feedback
  risks: list[str]
  safety: {auto_apply_ok: bool, needs_confirmation: bool}
  next_actions: list[NextAction]       # e.g. pre-filled orcho_phase_handoff_decide
```

- **Request fields:** `run_id`, `handoff_id`.
- **Unblocks:** parity with CLI actions 5/6 — a client can request a typed
  recommendation and a pre-filled decision instead of composing one blind.
- **Classification:** **projection-only** — the core `HandoffAdvice` model and
  artifact writer already exist; the gap is MCP wiring plus invoking the existing
  generator. **Reform note (lower priority):** MCP clients are themselves LLMs and
  already receive the typed reviewer findings
  (`meta.phase_handoff.artifacts.findings[]`, T2 area A1), so they can self-advise
  and call `retry_feedback` with their own feedback (proven in S2). This contract
  is parity/convenience and a way to *persist* advice into the decision record —
  not a missing capability. `retry_with_advice` reduces to
  `retry_feedback` with client-or-core-generated feedback; no separate action verb
  is required.

---

## Candidate dispositions (explicit)

| Candidate (prior work) | Disposition | Evidence-based rationale |
| --- | --- | --- |
| run-diagnosis: typed condition + ready next_actions | **CONFIRM → GC-1** (reformed) | Real need at S5/S8, but drop conditions already typed in `meta.status`/`worktree_continuity`; keep only the net-new terminal/delivery/supersede states. |
| stale/obsolete detection + safe-resume guidance | **CONFIRM → GC-2** | S5/F3: terminal resume returns a misleading success with no typed inert signal. |
| delivery/recovery guidance after failure/halt | **CONFIRM → GC-3 + GC-4** | S8/F1 (verdict prose-only) and S6/F5 (failure cause log-only). |
| change / pool / retrospect / plan-followups bridge | **REJECT** | No such subcommands exist in canonical `orcho-core` CLI (it exposes `run, cross, status, metrics, history, evidence, repair, diff, cost, price, profiles, workflows, prompts, workspace, web, verify`); not exercised by any audit scenario. No public run-control affordance to mirror — out of scope for this spec. |
| run-economics: cost / rounds / retry-rate | **REJECT / REFORM (minor)** | Data already exposed: `metrics.json` carries `total_tokens*`, `total_duration_s`, per-phase `attempts`, `total_rounds`, `subtasks`; reachable today via `orcho_run_metrics` (CLI `cost`/`metrics`). The only delta is that `RunMetrics.metrics` is an untyped passthrough `dict` and retry-rate is not pre-derived — a **projection-only** refinement (GC-8), not a missing surface. |

### GC-8 — (minor, projection-only) typed run-economics model

A thin refinement, not an open gap: replace the untyped `RunMetrics.metrics: dict`
passthrough with a typed model and add one derived field.

```
RunEconomics:
  total_tokens: int
  total_duration_s: float
  total_rounds: int
  retry_rate: float        # NET-NEW derived = (sum(phase.attempts) - n_phases) / n_phases
  phases: list[PhaseCost]  # {phase, total_tokens, duration_s, attempts}
```

- **Classification:** **projection-only** — every input already exists in
  `metrics.json`; `retry_rate` is a pure derivation over `phase.attempts`.

---

## GC-9 — findings resolved-vs-active classification

**Confirms** the third delivery-gate question (S8 / F9): "which findings are
resolved vs active?" has no typed surface. Findings appear typed on an active
handoff (`meta.phase_handoff.artifacts.findings[]` — `id/severity/title/body/
required_fix`, S2), but once a run advances or completes there is no field that
says a given finding was resolved by a later round vs is still active.

```
# additive on orcho_run_evidence(slice="findings")
ClassifiedFinding:
  id: str
  severity: str
  title: str
  phase: str
  state: Literal["active", "resolved", "waived"]   # NET-NEW classification
  resolved_in_round: int | None
```

- **Request fields:** `run_id` (existing evidence slice call).
- **Unblocks:** S8 delivery-gate question (c) — read resolved/active findings as a
  typed list instead of reconstructing it by hand.
- **Classification:** depends on core. The raw findings exist per round
  (projectable), but a typed **resolved/active/waived** state requires core to
  correlate findings across rounds (a verdict transitioned from REJECTED→APPROVED,
  or a waiver applied). If core already records per-round finding lifecycle, this
  is **projection-only**; if not, the lifecycle correlation is **core-data-needed**.
  The audit did not surface a per-finding lifecycle field, so it is recorded as
  **core-data-needed (pending confirmation)** — a core-contract gap, not
  implemented here.

---

## GC-10 — typed verification gate timeline

**Confirms** the official verification timeline gap. Core/CLI now render compact
gate blocks that answer which official gates ran, which were fresh/stale/missing,
which were manual-only, which receipt proves the result, and which command should
be rerun. MCP currently exposes some raw evidence and phase-level receipts, but
not the same official gate timeline as typed captain data.

```
# additive on orcho_run_status and/or orcho_run_evidence(slice="verification")
VerificationTimeline:
  gates: list[VerificationGateEntry]
  residuals: list[VerificationResidual]

VerificationGateEntry:
  command_id: str
  env_id: str | None
  hook: str | None                  # after_phase(implement), before_delivery, ...
  source: Literal["auto_run", "inspected", "inherited", "manual_only", "skipped"]
  policy: Literal["required", "warn", "suggest", "manual_only"] | None
  status: Literal["PASS", "FAIL", "MISSING", "STALE", "SKIPPED", "FRESH"]
  receipt_path: str | None
  searched_run_dir: str | None
  rerun_hint: str | None
```

- **Request fields:** `run_id`.
- **Unblocks:** a captain can decide whether to deliver, rerun a gate, or stop
  without reading terminal output or receipt JSON.
- **Classification:** **core-data-needed for the durable source of truth** if the
  timeline still lives only in in-memory run extras or terminal formatting.
  **MCP projection** once core exposes a stable trail/artifact/SDK projection.

### Core follow-up gap — per-firing scheduled-event trail is not durable

The MCP projection (`orcho_run_evidence(slice="verification_timeline")`, fed by
`sdk.get_verification_timeline`) carries the run-level official truth: readiness
classification (per-gate `status` in the six-value enum `{PASS, FAIL, MISSING,
STALE, SKIPPED, FRESH}` with per-gate `rerun_hint`/`searched_run_dirs`,
`manual_only` as `SKIPPED` + `policy=manual_only`) plus the auto-run events. That
is sufficient for the criteria above (typed gate timeline, per-gate remediation,
manual-only distinction, inherited/fresh distinction).

What it cannot yet reconstruct is the **per-firing scheduled-hook trail**. In core:

- `extras['verification_gate_events']`
  (`pipeline/project/gate_repair.py::VERIFICATION_GATE_EVENTS_KEY`) — the
  gate_repair routing-decision trail (one record per scheduled-gate firing, with
  `executed_pass` / `executed_fail` / `skipped_fresh` / `skipped_manual`) — and
- `verification_gate_routing_plans`
  (`pipeline/project/gate_repair.py::VERIFICATION_GATE_ROUTING_PLANS_KEY`)

are **in-memory run extras only**. Unlike `verification_autorun`, which
`pipeline/project/verification_autorun.py::_record_autorun_evidence` mirrors to a
durable per-phase sink at `session['phase_log'][phase]['verification_autorun']`
(persisted into `meta.json`), the scheduled-gate trail has **no durable session
mirror or run-dir artifact**. So `pipeline/project/verification_timeline.py::build_verification_timeline`
can fold scheduled events in-process from live extras, but an out-of-process MCP
reader cannot recover the per-firing sequence of scheduled hooks
(`before_phase` / `after_phase` / `before_delivery`) with the
`executed_pass`/`executed_fail`/`skipped_fresh`/`skipped_manual` split **per firing**.

Consequently the SDK projection reports `scheduled_trail_available = False` and
omits a `scheduled_events` field.

**What closes the gap:** add a durable mirror of the gate-event trail — parallel
to the `verification_autorun` mirror in
`pipeline/project/verification_autorun.py` and written where `gate_repair`
records decisions in `pipeline/project/gate_repair.py` — either a per-phase
`session`/`meta.json` sink or a dedicated artifact under the run dir. Once that
durable trail exists, extend `sdk.get_verification_timeline` with a
`scheduled_events` field and set `scheduled_trail_available = True`; the MCP
slice then surfaces the per-scheduled-hook firing timeline additively.

**Not a workaround:** terminal banners and CLI gate blocks are presentation, not
truth, and must never be parsed as the source of the scheduled-event trail.

---

## GC-11 — typed handoff advice evidence

**Extends GC-7.** `orcho_handoff_advice` gives the next recommendation at a
pause. A captain also needs retrospective advice evidence: whether advice was
called, applied, repeated, resolved, stopped, and what it cost. Core already
builds this for CLI evidence, but MCP currently exposes it only through raw
bundle data, not typed slices.

```
# additive on orcho_run_evidence(slice="handoff_advice")
HandoffAdviceEvidence:
  calls: int
  applied_retries: int
  resolved: int
  repeated: int
  stopped: int
  by_source: dict[str, int]
  usage: {tokens: int | None, api_equiv: float | None}
  artifacts: list[{phase: str, handoff_id: str, path: str}]
```

- **Request fields:** `run_id`.
- **Unblocks:** cost-aware captain decisions and post-run learning without
  parsing the CLI `Agent advice` block.
- **Classification:** **MCP-only projection** when core evidence already contains
  `handoff_advice`; no new core data required unless the desired field is absent
  from the evidence bundle.

---

## GC-12 — small-task advisory and allowed companion plan evidence

**Confirms** the fast-path evidence gap. Core distinguishes a bypassed
`validate_plan` critique in `small_task` as advisory rather than blocking. Core
also records allowed companion modifications in the plan contract. MCP must not
force a captain to infer these semantics from raw plan JSON or terminal prose.

```
# additive on orcho_run_evidence(slice="findings" | "plan")
AdvisoryFinding:
  id: str
  phase: str
  severity: str
  title: str
  advisory: bool
  bypass_reason: str | None

AllowedModification:
  pattern: str
  reason: str
  scope: Literal["plan", "subtask"]
  subtask_id: str | None
```

- **Request fields:** `run_id`.
- **Unblocks:** a captain can tell "this critique was forwarded as advisory" from
  "this is an active release blocker", and can recognize legitimate companion
  files before flagging them as scope violations.
- **Classification:** **MCP projection** if core evidence/parsed plan already
  contains the advisory and allowed-modification data; **core-data-needed** for
  any missing durable advisory marker.

---

## GC-13 — correction and scope-expansion captain evidence

**Confirms** the correction/scope captain gap. Core correction UX needs to expose
truthful release state, correction route, repeated/fixed-point blockers, and
scope-expansion notice/risk/blocker evidence. MCP must treat these as control
facts, not terminal text.

```
# additive on status/evidence/diagnosis
CorrectionState:
  release_state: Literal["approved", "rejected", "correction_requested", "blocked"]
  route: Literal["code_fix", "gate_rerun", "contract_ack", "blocked"] | None
  non_converging: bool
  repeated_blockers: list[str]
  next_actions: list[NextAction]

ScopeExpansionEntry:
  path: str
  classification: Literal["notice", "risk", "blocker"]
  category: str | None              # build companion, fixture, schema, ...
  reason: str
  evidence_ref: str | None
```

- **Request fields:** `run_id`.
- **Unblocks:** a captain can approve, launch correction, stop because correction
  is not converging, or simply note a benign companion change without parsing
  banners.
- **Classification:** follows core. MCP projection is mandatory once core writes
  durable correction/scope evidence; until then, the missing fields are
  **core-data-needed** source-of-truth gaps.

---

## GC-14 — resilient observation loop guidance

**Confirms** the observation-loop resilience gap. `orcho_run_watch` is already
reconnectable: callers pass the previous `next_seq` as the next `since_seq`, and
`orcho_run_events_summary` provides bounded polling. The open gap is that a
captain can still treat a long `orcho_run_watch(timeout_s=1200+)` call as the
only happy path. Some MCP clients or transports cap long-lived tool calls below
the requested watch timeout; when that happens the run keeps advancing in the
background, but the observing agent loses the connection and may misread that as
a run failure.

```
ObservationLoopContract:
  recommended_watch_timeout_s: int        # small bounded value, e.g. 120-240
  reconnect_cursor: int                   # summary.next_seq / trigger.seq
  fallback_sequence: list[NextAction]     # watch -> events_summary -> watch
  stale_observer_warning: str | None      # client-side disconnect is not run failure
```

- **Request fields:** no new run-control input required for the first slice.
- **Unblocks:** a captain can monitor long implement/review/repair phases without
  relying on one fragile long-lived request. Client-side watch disconnects are
  handled as observation transport loss: reconnect with `next_seq`, or bounded
  `orcho_run_events_summary`, before making any decision about the run.
- **Classification:** **MCP projection / workflow guidance.** Core already has
  reconnect cursors and bounded summaries. The missing piece is MCP-facing
  workflow advice, docs, and tests proving the recommended loop does not require
  a single long watch to survive.

---

## Classification roll-up

| Contract | Surface | Classification |
| --- | --- | --- |
| GC-1 `orcho_run_diagnose` | tool | projection-only (delivery conditions derive from GC-3's projection) |
| GC-2 safe-resume `resume_outcome` | result field | projection-only |
| GC-3 typed `DeliveryState` (verdict + committed) | status | projection-only (project raw `meta.commit_delivery`) |
| GC-4 delivery-failure cause | status/evidence | mostly projection-only (`status`+`final_message` exist; granular `cause_code` core-data-needed) |
| GC-5 `mock_final_acceptance_reject` | core argv / run_start param | **core-data-needed** (core-contract gap) |
| GC-6a preflight failure reason | status/evidence | **core-data-needed** |
| GC-6b `orcho_project_inspect` | tool | projection-only |
| GC-7 `orcho_handoff_advice` | tool | projection-only (parity/convenience) |
| GC-8 typed run-economics | metrics model | projection-only |
| GC-9 findings resolved/active | evidence(findings) | **core-data-needed** (pending confirmation of per-round lifecycle) |
| GC-10 typed verification timeline | status/evidence | core-data-needed for durable trail, then MCP projection |
| GC-11 handoff advice evidence | evidence(handoff_advice) | MCP projection |
| GC-12 advisory findings / allowed modifications | evidence(plan/findings) | MCP projection if core markers exist; otherwise core-data-needed |
| GC-13 correction / scope expansion evidence | status/evidence/diagnosis | MCP projection once core source-of-truth lands; otherwise core-data-needed |
| GC-14 resilient observation loop | workflow/docs/tests | MCP projection / workflow guidance |
