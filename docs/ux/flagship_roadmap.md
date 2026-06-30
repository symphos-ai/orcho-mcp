# Flagship Roadmap — Quick Wins and Reconciled Stages

This roadmap sequences the work that follows the CLI↔MCP audit. It draws directly
on the parity matrix (`captain_workflow_audit.md`) and the draft typed contracts
(`flagship_gap_spec.md`, contracts GC-1…GC-9). It does two things:

1. ranks a **quick-win backlog** by UX-per-effort;
2. lays out a **reconciled stage roadmap** with one-paragraph entry/exit criteria,
   guaranteeing no scope is planned twice against the in-flight run-control parity
   set (the eight-area CLI↔MCP parity initiative, validated against the canonical
   parity definition).

Naming: the **parity set** is Stage 7C — the in-flight eight-area run-control
parity work (A1 pending handoff · A2 handoff decision · A3 human-retry · A4
follow-up lineage · A5 worktree continuity · A6 provider-session fallback · A7
verification receipts · A8 schema/docs). Per the matrix, A1/A2/A3/A4-records/A5/A7/A8
are already **covered-now** in this surface; Stage 7C still owns three residuals
(A4 active-child recommendation for MCP-created children; A5 blocked-worktree
recheck against disk; A6 fallback population). Everything else below is **OUTSIDE**
Stage 7C and is what these stages add.

## Product stance

The CLI is the current dogfood renderer: it is exercised constantly and therefore
surfaces the most polished operator feedback first. MCP is not a plugin on the
side of that experience. MCP is the agent-captain control plane.

That means new core facts must not remain trapped in terminal prose. Core owns
durable run-control facts and evidence. The CLI renders those facts for a human
terminal. MCP must project them as typed, low-guesswork control data:

- typed run diagnosis and ready next actions;
- typed handoff advice and retry guidance;
- typed verification timeline and receipt readiness;
- typed delivery/correction state;
- typed scope-expansion notice/risk/blocker evidence;
- typed economics and retry/cost facts when they affect captain decisions.

Parity is a floor, not the finish line. When the CLI has a compact banner, MCP
should usually expose the underlying fact more precisely than the banner.

---

## Quick-win backlog (ranked by captain UX-per-effort)

Quick wins are the **projection-only** slices that can land early without a core
change. Each is an early increment of exactly one stage below (cross-referenced in
the `Home stage` column) so the same scope is never counted twice. The remaining
core-data-needed contracts (GC-5 mock knob, GC-6a preflight reason, GC-4's
granular `cause_code`, GC-9 finding-lifecycle) are deliberately **excluded** from
quick wins — they are blocked on a core signal that does not exist today.

| Rank | Item | UX impact | Effort | UX/effort | Scenario unblocked | Home stage |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | **GC-2 + GC-1** safe-resume and `orcho_run_diagnose` | Very high — prevents success-shaped no-ops and gives one typed condition + next action | Low–Med — mostly derives from existing status, lineage, handoff, worktree continuity | ★★★★ | S5a / F3 (terminal-resume silent no-op), stale/superseded runs | Run Diagnosis |
| 2 | **Auto-detect profile selector** | High — lets an agent request Orcho's semantic profile choice and read the typed decision | Low–Med — pass through selector plus project `meta.auto_detect` | ★★★★ | run start profile choice, non-interactive profile selection | Profile Selection UX |
| 3 | **Verification timeline projection** | Very high — exposes the official gate truth that CLI now shows in live/DONE blocks | Med — needs a stable core trail or artifact plus MCP projection | ★★★★ | gate freshness, missing/stale/failed receipts, manual-only gates | Verification Evidence |
| 4 | **GC-14** resilient observation loop | High — prevents client-side long-watch disconnects from looking like run failures | Low — document and test watch → summary → watch reconnect guidance over existing cursors | ★★★★ | long implement/review phases, transport timeouts | Observation UX |
| 4a | **Handoff attention recovery** | High — prevents a durable paused handoff from disappearing from the captain workflow after reconnect or chat loss | Low–Med — mostly watch fast-path, workspace pending-decision projection, and next-action coherence | ★★★★ | already-paused handoffs, post-decision resume, reconnect after operator attention loss | Observation UX |
| 5 | **GC-7 + advice evidence** `orcho_handoff_advice` | High — turns CLI actions 5/6 into agent-native advice and retry input | Med — project existing advice artifacts and invoke the core advisor | ★★★ | advisory-action parity and CI retry decisions | Handoff Decision UX |
| 6 | **GC-3 + correction/delivery evidence** typed `DeliveryState` and correction outcome | High — approved/rejected/correction/applied becomes typed state, not prose/raw meta | Low–Med — project existing delivery dicts; add fields as core lands them | ★★★ | S8 / F1, correction gate-rerun, rejected delivery | Delivery / Evidence UX |
| 7 | **Small-task advisory semantics + allowed modifications** | Med–High — prevents captains from treating advisory critique as either green or blocking | Med — project existing evidence/plan contract fields | ★★ | fast-path plans, companion/scope changes | Delivery / Evidence UX |
| 8 | **GC-8** typed run-economics + `retry_rate` | Med — typed cost/rounds instead of a passthrough dict | Low — type the dict, derive one field | ★★ | cost/rounds/retry-rate | Run Diagnosis |
| 9 | **Provider-pressure diagnosis** | Med–High — distinguishes 429/session/transport pressure from code failure and gives wait/resume actions | Med — depends on typed core provider-pressure state | ★★ | provider 429/session limits, parked retry-later runs | Run Diagnosis |
| 10 | **GC-6b** `orcho_project_inspect` | Med — pre-run safety; catch non-git/ambiguous projects before `orcho_run_start` | Med — needs a non-interactive core entrypoint MCP can call | ★★ | S9 / F6 (project preflight, read side) | Run Diagnosis |

**Why this order.** Rank 1 fixes misleading control flow first: a captain must
never get a success-shaped resume result when nothing advanced. Rank 2 follows
because profile choice is now semantic and can be delegated to core; MCP must
expose the selector and the persisted decision instead of forcing agents back to
terminal prose. Rank 3 follows because verification gates are now a load-bearing
release signal in CLI output; without a typed MCP timeline, the agent cannot know
which gate ran, which receipt proves it, or what exact rerun is needed. Rank 4 makes observation itself
robust: long-running tool calls are a transport convenience, not a correctness
contract, so captains need a short-watch / bounded-summary reconnect loop.
Rank 5 promotes handoff advice from terminal convenience to MCP-native control
input. Rank 6/7 expose the delivery/correction/scope facts that determine
whether a captain should approve, correct, waive, or stop. Economics and project
inspection remain important, but they do not outrank correctness and delivery
control.

---

## Reconciled stage roadmap

Each stage lists entry (what must be true to start) and exit (what proves it is
done) in one paragraph each. The parity set is treated as in-flight; the
post-parity stages touch **only** the OUTSIDE gaps.

### Stage P — Run-control parity set (in flight)

*Scope (as planned):* the eight parity areas. The matrix confirms A1, A2
(four-verb decision vocabulary), A3 retry lifecycle, A4 lineage records, A5
worktree-mode banners, A7 verification receipts, and A8 schema/docs are
**covered-now**. Stage 7C still owns three residuals plus one re-scoped item.
*Re-scoped INTO this stage:* **GC-2 safe-resume `resume_outcome`** — the
terminal-resume no-op is resume-control UX adjacent to Stage 7C's existing
"resume must not surface a raw traceback for an undecided handoff" guarantee, so it
belongs here rather than spawning a new stage for a one-method guard.
**Entry:** the four-verb decision vocabulary, typed pending-handoff, retry
lifecycle, lineage records, worktree-continuity, and verification-receipt surfaces
are present (they are — covered-now in the matrix). **Exit:** the A4 active-child
recommendation also fires for MCP-created children (today `from_run_plan` stamps
`plan_source="run"` not `resume_mode="followup"`, so it never fires — S5c/F4); the
A5 blocked-worktree fields are rechecked against disk so a stale/deleted worktree
surfaces `blocked=true` (today it stays `false` — S5b/F8); A6 provider-session
fallbacks are observably populated end-to-end (a real provider-session miss path,
not mock); and `orcho_run_resume` returns a typed `resume_outcome` distinguishing
`applied` / `rejected_terminal` / `pending_decision` / `superseded_by_child`. No
new prose-only or absent CLI affordance remains inside A1–A8.

### Stage PS — Profile Selection UX

*Scope:* expose core's semantic profile selector and auto-detect decision to MCP
captains. This stage owns `profile="auto-detect"` as a run-start selector, the
profile catalog distinction between executable profiles and selectors, and the
typed projection of persisted `meta.auto_detect` decisions. **Entry:** core
supports semantic profiles and records auto-detect decisions in run metadata.
**Exit:** MCP accepts the selector without resolving it locally, status/evidence
returns the selected profile/work kind, mode, confidence, disposition, and
fallback/error state when present, and mock run-control tests prove an agent can
request auto-detect and later understand what Orcho chose.

### Stage RD — Run Diagnosis

*Scope:* **GC-1** (`orcho_run_diagnose` typed `condition` + ready `next_actions`),
**GC-8** (typed run-economics + `retry_rate`), **provider-pressure diagnosis**
(typed 429/session/transport pressure, retry state, parked-until-reset when core
provides it), **GC-6b** (`orcho_project_inspect` read-side discovery), and the
core-dependent extension **GC-6a** (typed preflight failure reason). This is the
"tell the client the typed state and the *why* of any run — started, terminal,
failed-to-start, or parked waiting for provider capacity — plus what to call next"
stage.
**Entry:** the parity set's typed run-state fields (`meta.status`,
`meta.phase_handoff`, `lineage.*`, `worktree_continuity.*`) and `metrics.json`
economics are stable to project from (they are today); provider-pressure
projection waits for the core source-of-truth task
`provider-pressure-recovery-stage-0.md`; GC-1's `delivery_ready` /
`already_delivered` conditions are gated on Stage DE landing the typed delivery
state, so they ship in a second increment. **Exit:** a single
`orcho_run_diagnose(run_id)` call returns a typed condition and ready-to-call
next-actions for the non-delivery conditions (S5 terminal/supersede/blocked),
provider-pressure runs expose `retrying` / `exhausted` / `parked_until_reset`
state with safe wait/resume actions, run-economics is a typed model with a
derived `retry_rate`, and a non-git or ambiguous project surfaces a typed
reason/remediation via `orcho_project_inspect` instead of an opaque `interrupted`
(S9/F6) — with the `delivery_ready` conditions added once Stage DE provides the
data.

### Stage VT — Verification Evidence Timeline

*Scope:* project the official verification-gate timeline that the CLI already
renders as live/DONE blocks. The captain needs typed gate events by hook/source:
auto-run, inspected, skipped-fresh, skipped-manual, inherited, missing, stale,
failed, pass, receipt path, searched directory, and exact rerun hint. **Entry:**
core must expose a durable timeline/trail, or MCP must consume a stable core SDK
projection of the existing receipt directories and readiness classification; MCP
must not scrape terminal text. **Exit:** `orcho_run_status` and/or
`orcho_run_evidence` returns a compact `verification_timeline` object that
answers which gates ran, why, with what status, and what to call next when a
required gate is missing/stale/failed.

### Stage OB — Observation Loop Resilience

*Scope:* **GC-14** resilient observation guidance for long-running runs. This is
not a new run-state source of truth; it makes the existing `orcho_run_watch`
reconnect cursor and `orcho_run_events_summary` polling surface into an
explicit captain workflow. The handoff-attention increment also owns the
dogfood gap where a run is durably paused but no active MCP request brings that
decision back into focus. **Entry:** `orcho_run_watch` already returns
`next_seq` / `trigger.seq`, and `orcho_run_events_summary` already returns
bounded status, current phase, current subtask, pending handoff, and next
actions. **Exit:** MCP workflow docs and tests recommend short bounded watches
(for example 120-240s), then `orcho_run_events_summary` on timeout or client
disconnect, then reconnect with the returned cursor. `orcho_run_watch` wakes
immediately for an already-paused handoff, successful start/resume responses
lead clients into the watch loop with a ready `orcho_run_watch` next action,
pending handoffs are discoverable after a new client/session attaches via the
artifact-built `orcho_workspace_pending_decisions` projector, and post-decision
surfaces (`orcho_run_diagnose`, `orcho_run_live_status`,
`orcho_run_events_summary.pending_handoff`, and that projector) direct the
captain to resume rather than decide again. A client-side watch disconnect is
documented and tested as observation loss, not run failure.

### Stage HD — Handoff Decision UX

*Scope:* **GC-7** (`orcho_handoff_advice`) plus durable advice evidence. This
stage adds the advisory surface that sits *beyond* the parity set's four-verb
decision vocabulary; it must not re-touch the decision actions themselves
(covered-now in A2). **Entry:** the parity set's typed handoff (A1) and four-verb
decision (A2) surfaces are present (they are), and core's existing
`HandoffAdvice` model + advice-artifact writer are reachable through the
run-control boundary. **Exit:** an MCP client can request a typed
`HandoffAdviceResult` (recommended action, confidence, generated feedback,
risks, safety) for a paused handoff, receive a pre-filled
`orcho_phase_handoff_decide` next-action, and later read typed advice evidence
(applied/repeated/resolved/stopped, usage/cost) without parsing the terminal
`Agent advice` block.

### Stage DE — Delivery / Correction / Scope Evidence UX

*Scope:* the delivery cluster — a **projection-first** stage with core-dependent
tails: **GC-3** typed `DeliveryState`, **GC-4** delivery-failure surface,
**GC-9** findings resolved/active, **GC-5** the
`mock_final_acceptance_reject` test affordance, correction route/fixed-point
evidence, small-task advisory semantics, and scope-expansion
notice/risk/blocker entries. **Entry:** GC-3 and the projection-only half of
GC-4 can start immediately — S8 confirmed the delivery verdict and committed
state are already structured in `meta.commit_delivery`; correction and
scope-expansion fields enter as their core source-of-truth stages land. **Exit:**
the captain can distinguish approved delivery, rejected delivery, correction
requested, correction non-converging, benign scope expansion, scope blocker,
advisory validate-plan bypass, and active release blockers through typed MCP
data. Stage RD's `delivery_ready` / `already_delivered` conditions light up on
GC-3's projection.

### Stage CB — Change / Pool Bridge (deferred, entry-gated)

*Scope (after reconciliation):* a prospective MCP bridge for change / pool /
retrospect / plan-followup workflows. The gap spec **rejected** this candidate on
evidence: canonical `orcho-core` exposes no such CLI subcommands today (its surface
is `run, cross, status, metrics, history, evidence, repair, diff, cost, price,
profiles, workflows, prompts, workspace, web, verify`), and no audit scenario
exercised it. **Entry (gate):** a public, stable core/CLI surface for these
workflows must exist first — this stage cannot begin while there is no upstream
affordance to mirror; opening it sooner would invent an MCP surface with no core
contract behind it. **Exit:** once such a core surface lands, each of its
operator-relevant affordances is mirrored as a typed MCP tool/resource with the
same disposition discipline used in the gap spec (projection-only vs
core-data-needed) — scoped in a fresh audit, not pre-committed here.

---

## No-duplication ledger

Every scope item is owned by exactly one stage. Nothing below appears in two
stages, and no post-parity stage re-plans a parity-set area.

| Scope item | Source | Owner stage | Not in |
| --- | --- | --- | --- |
| A1 pending handoff, A2 four-verb decision, A3 retry lifecycle, A4 lineage records, A5 worktree-mode banners, A7 verification receipts, A8 schema/docs | T2 (covered-now) | Stage P (delivered) | RD, HD, DE, CB |
| A4 active-child recommendation for MCP-created children | T2 (covered-by-7C) | Stage P (residual) | RD, HD, DE, CB |
| A5 blocked-worktree recheck against disk (stale worktree) | T2 (covered-by-7C) | Stage P (residual) | RD, HD, DE, CB |
| A6 provider-session fallback population | T2 (covered-by-7C) | Stage P (residual) | RD, HD, DE, CB |
| GC-2 safe-resume `resume_outcome` | T3 (projection-only) | Stage P (re-scoped) | RD, HD, DE, CB |
| auto-detect profile selector and `meta.auto_detect` projection | core semantic profile cutover | Stage PS | P, RD, VT, OB, HD, DE, CB |
| GC-1 `orcho_run_diagnose` (non-delivery conditions) | T3 (projection-only) | Stage RD | P, HD, DE, CB |
| GC-1 delivery conditions (`delivery_ready` / `already_delivered`) | T3 (projection of GC-3 data) | Stage RD (2nd increment, on GC-3) | P, HD, CB |
| GC-8 typed run-economics + `retry_rate` | T3 (projection-only) | Stage RD | P, HD, DE, CB |
| provider-pressure diagnosis (`provider_pressure`, retry state, parked-until-reset) | dogfood + core provider-pressure task | Stage RD | P, VT, HD, DE, CB |
| GC-6b `orcho_project_inspect` | T3 (projection-only) | Stage RD | P, HD, DE, CB |
| GC-6a typed preflight failure reason | T3 (core-data-needed) | Stage RD (core-dep) | P, HD, DE, CB |
| official verification gate timeline | CLI/core verification UX audit | Stage VT | P, RD, HD, DE, CB |
| GC-14 resilient observation loop | observation-loop dogfood | Stage OB | P, RD, VT, HD, DE, CB |
| handoff attention recovery after reconnect / already-paused handoff | dogfood run `20260625_233708_79d845` | Stage OB | P, RD, VT, HD, DE, CB |
| GC-7 `orcho_handoff_advice` | T3 (projection-only) | Stage HD | P, RD, DE, CB |
| handoff advice evidence / ROI | core advice evidence | Stage HD | P, RD, DE, CB |
| GC-3 typed `DeliveryState` (verdict + committed) | T3 (projection-only) | Stage DE | P, RD, HD, CB |
| GC-4 delivery-failure surface | T3 (mostly projection-only) | Stage DE | P, RD, HD, CB |
| GC-9 findings resolved/active | T3 (core-data-needed) | Stage DE | P, RD, HD, CB |
| GC-5 `mock_final_acceptance_reject` | T3 (core-data-needed) | Stage DE | P, RD, HD, CB |
| correction route / fixed-point evidence | core correction UX | Stage DE | P, RD, HD, CB |
| small-task advisory semantics | core small-task evidence | Stage DE | P, RD, HD, CB |
| allowed companion / scope expansion evidence | core plan and scope UX | Stage DE | P, RD, HD, CB |
| change / pool / retrospect / plan-followups bridge | T3 (rejected) | Stage CB (deferred, gated) | P, RD, HD, DE |

**Reconciliation check:** Stage P delivers and owns A1–A8 plus the GC-2 resume
guard and the three A4/A5/A6 residuals; Stages RD/VT/OB/HD/DE/CB touch only
OUTSIDE gaps GC-1/3/4/5/6/7/8/9/10/11/12/13/14. No Stage 7C area is re-planned
in a later stage, and no GC contract appears in more than one owner stage (GC-1's
delivery conditions are a sequenced second increment of its single owner RD,
derived from GC-3's projection, not a second owner).
