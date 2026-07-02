# Captain Semantics: Delivery, Correction, Scope, and Advisory Evidence

A captain reads a run's post-release state through `orcho_run_evidence` typed
slices — never raw logs. This doc defines how to read the `delivery`,
`correction`, `scope_expansion`, and advisory-`findings` projections and where
each fact comes from. All four are read-only durable projections; none mutate
state. The interactive decision surface stays `orcho_delivery_gate` /
`orcho_delivery_decide` (see `docs/architecture/delivery_gate_projection.md`).

Package boundaries for these reads are pinned in
`docs/architecture/mcp_boundaries.md`: projections live in
`inspection/evidence.py`, durable reads go through `services/run_artifacts.py`,
and `tools.py` handlers stay thin. The wire contract is enforced by
`tests/integration/protocol/test_schema_snapshot.py` and exercised end-to-end by
`tests/acceptance/mock_pipeline/test_correction_scope_evidence_smoke.py`.

## `delivery` — post-release commit-delivery outcome

`orcho_run_evidence(slice="delivery")` returns a `DeliverySummaryRecord`
projected from the authoritative `meta['commit_delivery']` decision (the
persisted `CommitDeliveryDecision` from
`pipeline.engine.commit_delivery`). `None` when the run recorded no
commit-delivery decision.

| Field | Meaning for the captain |
| --- | --- |
| `release_verdict` | `approved` / `rejected` / `none` — the release gate outcome. |
| `decision_status` | Raw `CommitDeliveryStatus`, preserved verbatim. |
| `action` | `approve` / `apply` / `fix` / `skip` / `halt`. |
| `applied` | The diff landed in the checkout (`applied_uncommitted` or `committed`). |
| `committed` | A new commit was written (`committed`, or a `commit_sha` is present). |
| `commit_sha` | The commit hash, when one was written. |
| `skipped` | The delivery was skipped without landing. |
| `failed` | `commit_failed` / `apply_failed` / `halted` / `verification_blocked` / `target_dirty`. |
| `halt_reason` | Recoverable halt reason, when the delivery halted. |
| `implement_delivery` | The same implement delivery/waiver audit the `errors` slice carries. |

Read the four booleans as mutually informative, not mutually exclusive:
`committed` implies `applied`. An unrecognized `decision_status` leaves all four
`False` while the raw status is still preserved — a forward-compatible read, not
a silent drop.

- **approved** — `release_verdict == "approved"`; `applied` / `committed`
  describe whether the change actually landed.
- **rejected** — `release_verdict == "rejected"`. A `fix_requested`
  `decision_status` means the operator was asked to send the change back for
  another correction round; it is a correction-flow state, **not** a delivery
  `failed`.
- **correction requested** — `action == "fix"` (typically alongside a rejected
  release): the run is waiting for a fix decision.

### approved-after-`gate_rerun` and inherited vs current receipts

A correction child re-run after a `gate_rerun` reads `release_verdict ==
"approved"` from **its own** `commit_delivery` block — the parent's earlier
rejection does not leak into the child's verdict.

The delivery slice does **not** duplicate the verification receipts behind that
approval. To explain which receipts were inherited from the parent run and which
were produced by the current child, read them from two other slices:

- `verification_timeline` — the `inherited` aggregate plus each gate's
  `inherited` flag and `source_run_id` (the deciding receipt's origin run).
- `receipts` — the per-subtask delivery receipts.

So the captain answers "is this approval standing on the child's own work or on
inherited receipts?" from `verification_timeline.inherited` + `receipts`, never
from the delivery slice alone.

## `correction` — fixed-point / non-convergence

`orcho_run_evidence(slice="correction")` returns a `CorrectionSliceRecord` read
from `meta['correction_fixed_point']` (the ADR 0098 non-convergence block core
writes when a correction child repeats its parent's blockers) plus the run's
`halt_reason`. `None` when core recorded no fixed-point block.

| Field | Meaning |
| --- | --- |
| `non_converging` | `True` — an **operator-decision** condition: the loop is not making progress. |
| `repeated` | The blocker fingerprints that recurred parent → child. |
| `parent_run_id` / `child_run_id` | The two runs in the non-converging pair. |
| `suggested_actions` | Advisory next-step hints — **never** auto-applied. |
| `reason` | Why core flagged non-convergence. |

`non_converging=True` means the captain should **stop and decide** (e.g. halt the
correction loop and inspect the recurring blockers), choosing from
`suggested_actions`. MCP surfaces the fact and the hints; it never launches a
follow-up fix on its own.

## `scope_expansion` — the ADR 0110 plan-vs-delivered axis

`orcho_run_evidence(slice="scope_expansion")` returns a
`ScopeExpansionSliceRecord` projected from
`meta['phases']['final_acceptance']['scope_expansion']`: the audit of paths the
run touched **outside** the plan's declared surface. Empty slice when the run
recorded no such audit.

Each item carries a `classification`:

| `classification` | Captain reading |
| --- | --- |
| `notice` | **Information only.** No operator decision, no handoff. |
| `risk` | **Warning** worth attention — not a hard stop on its own. |
| `blocker` | **Operator decision** — a release-blocking out-of-scope change; reflected in the slice's `has_blocker` flag. |

A `notice` never forms an operator handoff or a `next_action`; the slice carries
no such field for it. `has_blocker=True` is the decision condition. MCP changes
no core policy here — it reflects what `final_acceptance` recorded.

Core persists each item's `status` as the `ScopeExpansionStatus` **enum value**
(`scope_expansion_notice` / `scope_expansion_risk` / `scope_expansion_blocker`;
see `pipeline.engine.scope_expansion`). The projector normalises those onto the
bare `notice` / `risk` / `blocker` tokens above, so a captain always branches on
the short form regardless of the core build.

### Two distinct scope axes — do not conflate

There are **two** unrelated "scope" facts in the evidence surface:

- **`scope_expansion` (ADR 0110)** — this slice. *Plan-vs-delivered* surface
  audit at `final_acceptance`: did the change stray outside the declared plan
  surface? Classifications: `notice` / `risk` / `blocker`.
- **delivery `scope_disclosure`** — on `DeliveryGateProjection`
  (`orcho_delivery_gate`; see `docs/architecture/delivery_gate_projection.md`).
  *Strict-mono sibling* disclosure: the per-alias sibling-repo files
  (`[alias]/rel/path`) implicated by a `delivery_scope_violation` shipping
  block.

They answer different questions (plan-surface drift vs. cross-repo shipping
scope) and come from different durable sources. A captain must not read one as
the other.

## Advisory findings — visible but not active

`orcho_run_evidence(slice="findings")` marks each `FindingRecord` with an
`advisory` flag. A `validate_plan` finding whose critique was forwarded into a
**successful whole-plan implement** (the implement record carries `output`, has
no `guardrail_blocked` / `failed` / `error`, and ran no subtask DAG) is
`advisory=True`: **visible, but not an active release blocker**. This mirrors
core's `_review_finding_summary` advisory rule
(`pipeline.project.finalization`) — a durable-data rule, not an LLM
classification.

Advisory is scoped to the **latest** `validate_plan` attempt only, exactly as
core computes it: findings from earlier attempts are historical/resolved
(neither active nor advisory), and if the latest attempt was **approved** core
marks nothing advisory. The SDK flattens findings across all attempts, so the
projector keys `advisory` on the latest attempt number, not the phase alone.

`advisory` isolates **only** that forwarded-critique subset — it is **not** a
full active/resolved classification. `sdk.list_findings` flattens findings
across every attempt, so the `advisory=False` set still contains
historical/resolved entries. **Do not read `advisory=False` as "active":**

- Under a **whole-plan** implement with a non-approved latest `validate_plan`
  attempt, that attempt's findings are advisory and excluded from the active
  release-blocker set.
- Under a **subtask DAG** (positive `subtask_count`), the same validate_plan
  findings are **not** advisory — the whole-plan gate is `False`.
- When the latest `validate_plan` attempt was **approved**, no finding is
  advisory, yet its earlier-attempt findings are historical/resolved:
  `advisory=False` **and not active**.
- Findings from earlier, superseded attempts of any phase are `advisory=False`
  but historical — again `advisory=False` yet not active.

Because non-advisory is **not** the same as active, a captain must not build the
active release-blocker set from the `advisory=False` findings alone. Use the
reviewer / `final_acceptance` release verdicts — surfaced typed through the
`delivery` and `correction` slices — as the authority on what is currently
blocking.

## `plan.allowed_modifications`

`orcho_run_evidence(slice="plan")` includes `allowed_modifications` — the plan's
declared in-plan modification globs (ADR 0087), read from the durable
`parsed_plan.json` top-level field (the SDK plan summary does not carry it).
Empty when the plan declared none.
