# Captain Workflow Audit — MCP Surface Walkthrough

This audit drives nine operator ("captain") workflows end-to-end through the
**public MCP surface only** (tools + resources), with `mock=true`, against a
canonical `orcho-core` checkout. For each scenario it records the exact call
sequence with arguments and `run_id`, where typed data was sufficient, where a
consumer had to fall back to parsing prose / logs / guessing, where raw errors
leaked untyped, and a paired 1–10 usability score for the MCP path and for the
equivalent CLI path.

The point is a reproducible trace per scenario as an MCP-speaking agent would
experience it — not exhaustive assertion coverage (the L4 acceptance suite under
`tests/acceptance/mock_pipeline/` owns that).

## Provenance & Environment

All runs were driven from an editable environment built **outside** the repo
checkout, so the spawned pipeline subprocess resolves to the canonical
`orcho-core`:

- Driver interpreter / MCP server spawn interpreter (authoritative):
  `/path/to/orcho/workspace-orchestrator/runspace/mcp-local-core/.venv/bin/python`
  (CPython 3.12.9)
- `import pipeline` → `/path/to/orcho/orcho-core/pipeline/__init__.py`
- `import orcho_mcp` → `…/runspace/worktrees/wt_20260614_225823/checkout/src/orcho_mcp/__init__.py`

Every spawned run's argv confirms the binding, e.g.:

```
<venv>/bin/python -m pipeline.project_orchestrator --project <git-project>
  --workspace …/workspace-orchestrator --run-id <id> --mock --profile <p>
```

### Method

Scenarios 1–8 call the `@mcp.tool` handlers in-process (the handlers *are* the
MCP surface; the subprocess they spawn is canonical core), reusing the
subprocess discipline of `tests/acceptance/mock_pipeline/` — a fresh
`ORCHO_WORKSPACE`, a real git-initialised project per run (orcho-core's worktree
resolver hard-fails on a non-git `project_dir`), and a supervisor-singleton reset
between scenarios. Scenario 9 (cold-start) runs over a **real stdio MCP session**
(`python -m orcho_mcp` as a subprocess) to exercise the resource channel and the
client-facing discovery loop exactly as a fresh client would.

Run ids below are from one captured pass and are reproducible by re-running the
same call sequence; absolute run ids change per pass, the observed shapes do not.

---

## Scenario 1 — start → observe (happy path)

**Calls**
1. `orcho_run_start(task="s1 start observe", project_dir=<git-proj>, profile="lite", mock=True, max_rounds=1)` → `run_id=20260614_231946_965d83`
2. `orcho_run_watch(run_id, until="terminal", timeout_s=90)` → `triggered=True, trigger.kind="terminal", summary.status="done"`
3. `orcho_run_events_summary(run_id)` → `total_count=19, status="done", by_kind["run.end"]=1`
4. `orcho_run_status(run_id)` → `meta.status="done", artefacts=[parsed_plan, diff, evidence]`

**Typed-data sufficed:** terminal state (`summary.status`/`meta.status`), event
roll-up by kind/count, and advertised artefact kinds + URIs are all first-class.
No prose parsing needed to know the run finished or what it produced.

**Prose/guess fallback:** none required for the lifecycle decision.
**Raw-error leaks:** none.

- **MCP score: 9/10** — long-poll `watch` returns the moment it's terminal; one
  typed snapshot answers "done? what artefacts?".
- **CLI score: 6/10** — `orcho run …` streams a human transcript; an agent must
  scrape stdout for the terminal line and artefact paths.

---

## Scenario 2 — rejected handoff → retry_feedback

**Calls**
1. `orcho_run_start(profile="advanced", mock=True, max_rounds=1, mock_validate_plan_reject=3)` → `run_id=20260614_231947_5e05d5`, polls to `meta.status="awaiting_phase_handoff"`
2. `orcho_run_status` → `meta.phase_handoff = {id:"validate_plan:plan_round:2", phase:"validate_plan", available_actions:[continue, retry_feedback, halt, continue_with_waiver]}`
3. `orcho_phase_handoff_decide(run_id, handoff_id, action="retry_feedback", feedback="Tighten subtask scope; address F1.")` → `action="retry_feedback", feedback round-tripped`, `next_actions=[(orcho_run_resume, optional=False)]`
4. `orcho_run_resume(run_id)` → argv carries `--resume`; run settles to `done`

**Typed-data sufficed:** the pause is fully typed (`phase_handoff.id` +
`available_actions`), the decision echoes the action and the verbatim feedback,
and the single non-optional follow-up (`orcho_run_resume`) is advertised typed —
no guessing what to call next.

**Prose/guess fallback:** to *compose* the feedback an operator reads the
rejection findings; the findings themselves are typed (`orcho_run_evidence
slice="findings"`), so this is content authorship, not protocol guessing.
**Raw-error leaks:** none.

- **MCP score: 8/10** — clean decision→resume contract with a typed next-action.
- **CLI score: 5/10** — the reject pause is an interactive prompt; scripting a
  retry-with-feedback means driving a TTY and parsing the printed findings.

---

## Scenario 3 — continue_with_waiver

**Calls**
1. `orcho_run_start(advanced, mock=True, max_rounds=1, mock_validate_plan_reject=3)` → `run_id=20260614_231950_932886` → `awaiting_phase_handoff`
2. `orcho_phase_handoff_decide(handoff_id, action="continue_with_waiver", feedback="F1 known false positive on mock fixtures; accepted.")` → `action="continue_with_waiver", feedback==waiver`
3. `orcho_run_resume(run_id)` → settles `done`; `meta.phase_handoff` cleared; `meta.phase_handoff_waiver.waiver_text` persists the waiver verbatim

**Typed-data sufficed:** the waiver round-trips into a durable typed field
(`phase_handoff_waiver.waiver_text`) that survives the resume into a fresh
process, and the active handoff payload clears — a downstream gate can read the
waiver typed rather than re-litigating findings.

**Prose/guess fallback:** none for the protocol.
**Raw-error leaks:** none.

- **MCP score: 8/10** — waiver is a first-class, durable, queryable record.
- **CLI score: 5/10** — same interactive-prompt friction as S2; the durable
  waiver is observable only by re-reading state output.

---

## Scenario 4 — halt

**Calls**
1. `orcho_run_start(advanced, mock=True, max_rounds=1, mock_validate_plan_reject=3)` → `run_id=20260614_231952_d902f9` → `awaiting_phase_handoff`
2. `orcho_phase_handoff_decide(handoff_id, action="halt", note="not salvageable")` → `action="halt"`; `meta.status="halted"`
3. `orcho_run_evidence(run_id, slice="errors")` → `status="halted", halt_reason="phase_handoff_halt", halted_at` set

**Typed-data sufficed:** terminal `halted` status plus the typed
`halt_reason="phase_handoff_halt"` and `halted_at` are readable without touching
a log — the errors slice is the single inspection point.

**Prose/guess fallback:** none.
**Raw-error leaks:** none.

- **MCP score: 9/10** — halt reason is typed and reachable in one call.
- **CLI score: 6/10** — halt is visible in the transcript, but the structured
  `halt_reason` token must be parsed out of prose.

---

## Scenario 5 — resume halted run + stale/obsolete

This scenario has three variants per the contract: (5a) resume a halted run,
(5b) resume over a stale worktree, (5c) resume a run whose parent has a newer
follow-up (obsolete-run case). All three were driven through MCP; 5b and 5c are
recorded with the exact failing MCP step.

### 5a — resume a halted run

**Calls**
1. `orcho_run_start(advanced, reject=3)` → `run_id=20260614_231954_7f95c2`, decided `halt` → `meta.status="halted"`
2. `orcho_run_resume(run_id)` → returns a **success-shaped** `RunStartedResult` (fresh `pid=1101`, argv has `--resume`), `next_actions=[]`
3. poll → `meta.status` stays `halted`
4. `orcho_run_status(run_id)` → `lineage.recommended_action=None`, `lineage.recommendation=None`

**Failing/weak step (audit finding):** resuming an already-terminal (`halted`)
run is accepted at the MCP layer and returns the same success shape as a live
resume — a fresh pid and a populated `command` — yet the run does not advance and
stays `halted`. There is **no typed signal** that the resume was a no-op against a
terminal run: no `obsolete`/`already-terminal` status on the result, and the
empty `next_actions=[]` is the only (weak, easily-missed) hint. An MCP-only agent
cannot distinguish "resume took effect" from "resume was inert" without diffing
the status before and after.

### 5b — resume over a stale worktree

**Calls** (`run_id=20260614_234651_704173`)
1. `orcho_run_start(advanced, reject=3)` → paused; `orcho_run_status.worktree_continuity` = `{has_worktree=true, subject_mode="same_run_retained", path="…/worktrees/wt_20260614_234651_704173/checkout", blocked=false, block_message=null, degraded_reason=null}`
2. delete that worktree directory on disk (out-of-band, to make it stale)
3. `orcho_run_status(run_id)` again → `worktree_continuity` is **byte-for-byte unchanged**: `blocked=false, block_message=null, degraded_reason=null, worktree_preserved=true` — the missing worktree is not detected
4. decide `continue` + `orcho_run_resume(run_id)` → settles `awaiting_phase_handoff`, `halt_reason=null`; `worktree_continuity` still `blocked=false`

**Failing step (audit finding):** `worktree_continuity` is a projection of the
**spawn-time continuity decision**, not a live on-disk staleness check. After the
worktree directory is removed, `orcho_run_status.worktree_continuity.blocked`
stays `false` and `degraded_reason`/`block_message` stay `null` — the typed `blocked`
/ `block_message` fields exist (Stage 7C area A5) but are **never recomputed against
disk**, so a stale/missing worktree is invisible to an MCP-only agent until a
resume happens to fail. The exact step where it should surface and does not:
`orcho_run_status(run_id).worktree_continuity.blocked` returning `false` for a
deleted worktree path.

### 5c — obsolete run (parent with a newer follow-up)

**Calls** (`parent=20260614_234652_efacbe`, `child=20260614_234654_7f113d`)
1. `orcho_run_start(advanced)` (parent) → terminal
2. `orcho_run_start(advanced, from_run_plan=<parent>)` (child) → the only MCP way to spawn a child off a parent
3. poll `orcho_run_status(parent).lineage` while the child is alive **and** after it terminates → `has_active_child_followup=false`, `active_child_run_id=null`, `recommended_action=null`, `recommendation=null` the entire time
4. `orcho_run_status(child).meta` → `resume_mode=None`, `plan_source="run"`

**Failing step (audit finding):** the obsolete-parent recommendation is
**structurally unreachable through MCP**. Core's active-child detection
(`run_projection._detect_active_followup_child`) only counts a child whose
`meta.resume_mode == "followup"`; but the sole MCP child-spawn path,
`orcho_run_start(from_run_plan=…)`, stamps `plan_source="run"` and leaves
`resume_mode=None`. `resume_mode="followup"` is set only by the CLI follow-up
flow, which has no MCP entry point. So `lineage.has_active_child_followup` /
`recommended_action="resume_child"` (Stage 7C area A4) can never fire for any
MCP-created child — the "resume the active follow-up instead of the parent"
guidance is unobservable from MCP. Exact step: `orcho_run_status(parent).lineage.recommended_action`
stays `null` even with a live MCP-spawned child.

**Prose/guess fallback:** 5a — infer inertness by diffing pre/post status; 5b —
no signal at all (must probe the filesystem out-of-band); 5c — no signal at all.
**Raw-error leaks:** none (the problem in all three is silence, not a leak).

- **MCP score: 3/10** — 5a is a misleading success, 5b is a stale projection that
  never rechecks disk, 5c is structurally unreachable via the only MCP child path.
- **CLI score: 5/10** — the CLI prints a "nothing to resume / already terminal"
  line and drives the `resume_mode="followup"` flow that produces the active-child
  recommendation, so at least 5a and 5c are reachable for a human (still prose).

---

## Scenario 6 — follow-up lineage

**Calls**
1. `orcho_run_start(advanced, mock=True, max_rounds=1)` (parent) → `run_id=20260614_231955_77ea9f` → `done`
2. `orcho_run_start(advanced, mock=True, max_rounds=1, from_run_plan=<parent>)` (child) → `run_id=20260614_231957_819a57`
3. `orcho_run_status(child)` → `lineage.parent_run_id=<parent>, lineage.parent_status="done", resume_mode=None`; `meta.plan_source="run", meta.plan_source_run_id=<parent>`; `meta.halt_reason="commit_delivery_failed"`
4. `orcho_run_status(parent)` → `lineage.has_active_child_followup=False, recommended_action=None`

**Typed-data sufficed:** the child→parent linkage is fully typed —
`lineage.parent_run_id` + `parent_status`, and `meta.plan_source="run"` +
`plan_source_run_id` confirm the plan was inherited rather than re-planned.

**Failing/weak steps (audit findings):**
- The plan-inheritance child reached `halted` with
  `halt_reason="commit_delivery_failed"` under mock (and this is **nondeterministic**
  — a separate pass of the same call reached `done`). The delivery/commit step is
  flaky under the followup mock path; the typed surface reports the halt token but
  not the underlying delivery diagnostic.
- The parent's active-child recommendation (`has_active_child_followup`,
  `recommended_action="resume_child"`, `recommendation`) **never fired** for a
  `from_run_plan` child — it appears scoped to checkpoint-`followup` children, so
  plan-inheritance follow-ups are invisible to the "resume the active child
  instead of the parent" guidance.

**Prose/guess fallback:** to understand *why* `commit_delivery_failed` occurred,
a consumer must read `runner.log`; the typed errors slice carries only the token.
**Raw-error leaks:** none at the tool boundary; root cause is log-only.

- **MCP score: 7/10** — parent/child linkage and plan-source provenance are
  excellent; the active-child recommendation gap and log-only delivery diagnostic
  pull it down.
- **CLI score: 5/10** — the CLI prints follow-up guidance as prose; lineage
  fields must be parsed out and child plan-source provenance is not obvious.

---

## Scenario 7 — no-diff verification

The contract asks MCP to explain that a run delivered no diff **and why that is
or is not expected**. Three runs distinguish the cases: a completed verification
run that legitimately produces no diff (expected), an implementing run that does
produce one (so no-diff would be unexpected), and a halted-before-implement run.

**Calls**
1. **Completed verification, no diff (expected):** `orcho_run_start(profile="review", mock=True, max_rounds=1)` → `run_id=20260614_234703_fbc5ca` → `done`; `orcho_run_diff(run_id, mode="stat")` → `found=False, files=0, message="No diff artifact recorded for this run."`
2. **Completed implementing run (diff expected):** `orcho_run_start(profile="lite", mock=True, max_rounds=1)` → `run_id=20260614_234703_76e580` → `done`; `orcho_run_diff` → `found=True, files=["src/demo_project/implementation.txt"]`
3. **Halted-before-implement (no diff, also expected):** `orcho_run_start(advanced, reject=3)` → `run_id=20260614_231959_c54244` decided `halt` → `halted`; `orcho_run_diff` → `found=False`

**Typed-data sufficed:** "this run changed nothing" is a typed `found=False`
result with an explanatory `message`, **not** a JSON-RPC error. The `review`
profile reaches `done` and writes no diff because it verifies/reviews without an
implement-write phase — so no-diff is **expected** there; `lite`/`advanced` run an
implement phase and do write a diff, so a `found=False` on those **would be
unexpected** and signals a problem; a halted-before-implement run never reaches
the write phase, so no-diff is expected for a different reason. The terminal
`meta.status` plus the run's `profile` give an MCP-only agent enough typed context
to classify which of these applies.

**Prose/guess fallback:** the *reason* "no diff because this profile has no
implement phase" is inferred from `profile` + terminal status; there is no single
typed field that says "no-diff is expected for this run" — the agent composes that
judgement from `profile`, `meta.status`, and the `found=False` message.
**Raw-error leaks:** none.

- **MCP score: 8/10** — no-diff is a typed, non-error result across completed and
  halted runs; the *expected-ness* must be derived from `profile` + status rather
  than read from one field.
- **CLI score: 6/10** — `git diff`/diff output is empty, which is correct but
  indistinguishable from "diff not captured" without extra context.

---

## Scenario 8 — delivery gate (final_acceptance / commit delivery)

The contract requires answering three questions from MCP alone: (a) is the
finished run's diff already applied/committed to the target project, (b) what
files changed, and (c) which findings are resolved vs active.

**Calls** (`run_id=20260614_234701_49a193`, advanced clean → `done`)
1. `orcho_run_status(run_id)` → `meta.commit_delivery` (raw passthrough dict): `{action="approve", status="committed", project_path=<target>, source_path=<worktree>, baseline_ref="59b9dfd…", dirty=true, release_verdict="APPROVED", untracked_paths=[".orcho/mock_changes/last_build.md", "src/checkout/implementation.txt"]}`; `meta.change_handoff="uncommitted"`
2. `orcho_run_diff(run_id, mode="stat")` → `found=True, files=[".orcho/mock_changes/last_build.md", "src/checkout/implementation.txt"]`
3. `orcho_run_evidence(run_id, slice="receipts")` → `3 receipts, all state="done"`; `slice="findings"` → `findings=[]` (clean run)
4. cross-check (out-of-band) `git -C <target> log` → `"Mock release gate: change is ship-ready in mock mode."` — the change was committed, matching `commit_delivery.status="committed"`

**(a) applied/committed — answerable, but only via raw `meta`.** The delivery
state lives in `meta.commit_delivery`: `status="committed"` (vs `"commit_failed"`
on a failed delivery — confirmed against run `20260614_234654_7f113d`,
`halt_reason="commit_delivery_failed"`, `commit_delivery.status="commit_failed"`),
`action="approve"`, `release_verdict="APPROVED"`, `baseline_ref`, and `dirty`.
This **answers the committed question** and even carries the delivery verdict as a
structured `release_verdict` — but `commit_delivery` is a **raw passthrough dict
inside `meta`, not a typed projected field** on `RunStatus` (whose typed fields are
`run_id/run_dir/meta/metrics/sub_runs/lineage/worktree_continuity/next_actions/artefacts`).
An MCP agent must reach into the untyped dict by key; there is no typed
`delivery.status` / `delivery.verdict` projection.

**(b) what files changed — fully typed.** `orcho_run_diff(mode="stat")` returns
the changed files typed; `commit_delivery.untracked_paths` corroborates them.

**(c) findings resolved vs active — not available typed.** For this clean run
`orcho_run_evidence(slice="findings")` is `[]` (review passed). When findings
exist they live on the active handoff (`meta.phase_handoff.artifacts.findings[]`,
Scenario 2) but carry **no resolved/active status** — there is no typed surface
that classifies a run's findings as resolved-by-a-later-round vs still-active. An
agent cannot answer "which findings are resolved vs active" from a typed field.

**Failing/weak steps (audit findings):**
- Delivery state and verdict are present but **only as raw `meta.commit_delivery`
  dict keys**, not typed projections — dict-spelunking, not a typed surface.
- **Findings resolved/active classification is absent** — no typed field.
- The **rejecting** delivery-gate path still has **no mock knob**
  (`mock_validate_plan_reject` is the only one), so a controllable rejecting gate
  can't be exercised via `mock=true`.

**Prose/guess fallback:** reach into untyped `meta.commit_delivery` for
committed/verdict; compose resolved/active findings manually (no field).
**Raw-error leaks:** none on the happy path.

- **MCP score: 6/10** — committed-state, verdict, and changed-files are all
  *present* (raw `commit_delivery` + typed `diff`), but committed/verdict are
  untyped dict keys and resolved/active findings have no surface at all.
- **CLI score: 6/10** — the CLI renders the same release-gate summary as prose;
  no better at typed resolved/active findings.

---

## Scenario 9 — cold-start discovery (deferred dogfood smoke)

Run over a **real stdio MCP session** (`python -m orcho_mcp`) against a
git-initialised copy of `orcho-core/examples/golden-api`.

**Provenance recorded for this scenario:**
- server spawn interpreter: `…/runspace/mcp-local-core/.venv/bin/python`
- `pipeline.__file__`: `/path/to/orcho/orcho-core/pipeline/__init__.py`

**Calls** (`run_id=20260614_232136_7ed501`)
1. `orcho_workspace_info()` → `workspace_dir == $ORCHO_WORKSPACE`, `runs_dir` set
2. `orcho_workflows_list()` → `format_version=2`, recipes `[diagnose_halted_run, inspect_terminal_run, plan_then_implement, resume_failed_run, review_paused_run]`
3. `read_resource("orcho://workflows")` → same `format_version` and recipe set as the tool
4. `orcho_run_start(task="cold-start dogfood", project_dir=<golden-api>, profile="advanced", mock=True, max_rounds=1, mock_validate_plan_reject=3)`
5. `orcho_run_watch(until="handoff_or_terminal", timeout_s=60, interaction_client="claude-code")` → `triggered=True, trigger.kind="handoff"`
6. `HandoffDecisionHint.choices` → actions `[continue, retry_feedback, halt, continue_with_waiver]`; `retry_feedback.requires_feedback=True`; **no** placeholder feedback leaked into any `choice.args`
7. `orcho_run_status(run_id).artefacts` → kinds `[parsed_plan, evidence]`; **every** advertised URI resolves via `read_resource` (`orcho://runs/<id>/parsed_plan.json`, `orcho://runs/<id>/evidence`)

**Typed-data sufficed:** the entire discover → observe → inspect loop is typed
and self-describing: workspace location, machine-readable recipe catalogue
(identical via tool and resource channel), a typed handoff hint whose choices
carry only known actions + ready-to-send args, and artefact URIs that actually
resolve. A cold client needs no out-of-band docs to drive a paused run.

**Failing step (audit finding) — surfaced during this scenario:** the first
attempt ran the golden-api copy **without** `git init`. The run ended
`meta.status="interrupted", halt_reason="interrupted"` and, critically, the typed
surface gave **no root cause** — `orcho_run_evidence(slice="errors")` returned
`error_summary=None, errors=[]`. The actionable message —
`"project_dir is not a git repository … run \`git init\` …"` — existed **only in
`runner.log`**. An MCP-only agent hitting a non-git project gets an opaque
`interrupted` with empty typed errors and must read raw logs to diagnose.

- **MCP score: 9/10** — exemplary self-discovery; tool/resource parity, typed
  handoff choices, resolvable artefact URIs.
- **CLI score: 5/10** — no resource channel; discovery is `--help`/docs text and
  the recipe catalogue is not machine-addressable the same way.

---

## Friction Log

Aggregated places where the MCP surface forced a consumer to parse prose, guess,
or where root causes leaked only to logs. Each is a candidate gap for later
typed-surface work.

| # | Scenario(s) | Friction | Where it bit |
| - | ----------- | -------- | ------------ |
| F1 | S8 delivery gate | **Delivery verdict + committed state live in an untyped raw dict.** `meta.commit_delivery` carries `status` (`committed`/`commit_failed`) and `release_verdict` (`APPROVED`), but it is a raw passthrough dict, not a typed `RunStatus` projection; the `run.end` prose summary (`"… \| final_acceptance=ok"`) is the only other source. | dict-spelunk `meta.commit_delivery` |
| F2 | S8 delivery gate | **No mock knob for a rejecting delivery gate.** Only `mock_validate_plan_reject` exists; the blocking/rejecting `final_acceptance` path can't be exercised deterministically via MCP `mock=true`. | scenario un-runnable via MCP mock |
| F3 | S5a resume-halted | **Terminal resume is a silent no-op shaped as success.** `orcho_run_resume` on a `halted` run returns a fresh-pid success result with `next_actions=[]`; run stays `halted`; no typed obsolete/already-terminal signal. | infer by pre/post status diff |
| F4 | S5c / S6 follow-up | **Obsolete-parent recommendation is unreachable via MCP.** `_detect_active_followup_child` only counts a child with `meta.resume_mode=="followup"`, but the sole MCP child path `from_run_plan` stamps `plan_source="run"` (resume_mode=None); so `lineage.recommended_action="resume_child"` never fires for any MCP-created child. | guidance structurally absent |
| F5 | S6 follow-up | **Delivery diagnostic is log-only + nondeterministic.** `from_run_plan` child halted `commit_delivery_failed` in one pass and `done` in another; the typed errors slice carries the token, not the cause (the raw `commit_delivery.status="commit_failed"` is the only structured signal). | read `runner.log` for the why |
| F6 | S9 cold-start | **Non-git `project_dir` → opaque `interrupted`.** Typed errors slice is empty (`error_summary=None, errors=[]`); the actionable "not a git repository / run `git init`" message lives only in `runner.log`. | read raw log to diagnose |
| F7 | S2/S3 (cross-cutting) | **Resume settling is not directly observable.** After a decision+`orcho_run_resume`, the status can briefly still read the pre-resume `awaiting_phase_handoff`; distinguishing a stale pause from a genuine re-pause requires tracking the prior `handoff_id`. | compare `phase_handoff.id` across polls |
| F8 | S5b resume-stale | **Worktree continuity is not a live staleness check.** After the worktree dir is deleted on disk, `worktree_continuity.blocked` stays `false` and `degraded_reason`/`block_message` stay `null`; the projection reflects the spawn-time decision and never rechecks disk. | filesystem probe out-of-band |
| F9 | S8 delivery gate | **No resolved-vs-active findings classification.** Findings appear on an active handoff (`phase_handoff.artifacts.findings[]`) but carry no typed resolved/active status after a run; "which findings are resolved vs active" has no typed surface. | no field — manual reconstruction |

### What worked well (no friction)

- Typed pause descriptor (`meta.phase_handoff` with `id` + `available_actions`)
  and the decision round-trip (action + verbatim feedback/waiver) — S2, S3.
- Typed `halt_reason` via the errors slice — S4.
- No-diff modelled as a typed `found=False` result, not an error — S7.
- Durable typed waiver (`phase_handoff_waiver.waiver_text`) surviving resume — S3.
- Tool↔resource parity for the workflow catalogue and resolvable artefact URIs — S9.

## Scoring Summary

| Scenario | MCP | CLI | One-line rationale (MCP / CLI) |
| -------- | --- | --- | ------------------------------ |
| S1 start → observe | 9 | 6 | typed terminal+artefacts via watch / scrape stdout |
| S2 retry_feedback | 8 | 5 | typed handoff+next-action / drive a TTY prompt |
| S3 waiver | 8 | 5 | durable typed waiver / prompt + re-read state |
| S4 halt | 9 | 6 | typed halt_reason in one call / parse from prose |
| S5 resume halted + stale/obsolete | 3 | 5 | 5a misleading success, 5b stale worktree never rechecked, 5c obsolete unreachable / CLI drives the follow-up flow |
| S6 follow-up lineage | 7 | 5 | typed linkage; rec-gap + log-only cause / prose lineage |
| S7 no-diff | 8 | 6 | typed found=False across completed+halted; expected-ness derived from profile / empty diff is ambiguous |
| S8 delivery gate | 6 | 6 | delivered artefacts typed but verdict prose-only / same prose |
| S9 cold-start | 9 | 5 | full typed discovery loop / docs-only discovery |

## CLI ↔ MCP Parity Matrix

Each operator-relevant CLI affordance (interactive prompt, banner, status
recommendation, or recovery hint) is listed against its MCP equivalent. Every
row is marked with the **MCP form** (`typed-field` / `prose-only` / `absent`)
and exactly one **coverage** state:

- **covered-now** — MCP exposes a typed equivalent today (verified by a T1 trace
  or a current-surface capture).
- **covered-by-7C** — not fully present today, but inside one of the eight Stage
  7C run-control parity areas, so completing that Stage 7C area closes it.
- **open-gap** — not covered today **and** outside all eight Stage 7C parity
  areas; Stage 7C does not address it.

The eight Stage 7C run-control parity areas (the canonical CLI↔MCP parity set,
read from the canonical Stage 7C parity definition by absolute path) are:
**A1** pending handoff · **A2** handoff decision · **A3** human-retry /
repeated-reject · **A4** follow-up lineage · **A5** worktree continuity ·
**A6** provider-session fallback · **A7** verification receipts ·
**A8** schema / docs. Rows that map to **none** of A1–A8 are explicitly tagged
`OUTSIDE` and are the open-gaps the parity set will not close.

CLI affordances are quoted from canonical `orcho-core` (read-only). MCP forms are
grounded in the scenario traces plus current-surface captures against canonical
core: a paused advanced run `20260614_232913_7acc46` (areas A1/A3/A5/A6), a clean
done run `20260614_232916_6406b0` (area A7), and the Scenario 5/7/8 verification
runs cited inline (`20260614_234651_704173` stale worktree, `20260614_234652_efacbe`
/`20260614_234654_7f113d` obsolete parent/child, `20260614_234701_49a193` committed
delivery, `20260614_234703_fbc5ca` review no-diff).

### A1 — Pending handoff

| CLI affordance (file:line) | CLI string | MCP equivalent | MCP form | Coverage | Evidence |
| --- | --- | --- | --- | --- | --- |
| Handoff banner header (`pipeline/control/handoff_prompt.py:385`) | `"Phase handoff — {label}"` | `events_summary.pending_handoff.phase` / `.round_label` | typed-field | covered-now (A1) | capture `…_7acc46`: `round_label="validate_plan automatic round 2/2"` |
| Handoff context lines: handoff_id / policy / trigger / verdict (`handoff_prompt.py:387-390`) | `"handoff_id : …" / "trigger : …" / "verdict : …"` | `pending_handoff.{handoff_id,trigger,verdict}` and `meta.phase_handoff.{id,type,trigger,verdict}` | typed-field | covered-now (A1) | capture: `trigger="rejected"`, `verdict="REJECTED"`, `type="human_feedback_on_reject"` |
| Reviewer last-output preview (CLI shows full critique prose) | last critique rendered inline | `pending_handoff.last_output_preview` **and** `meta.phase_handoff.artifacts.findings[]` (typed `id/severity/title/body/required_fix`) | typed-field | covered-now (A1) | capture: typed F1/F2/F3 findings — MCP is *more* structured than the CLI prose |
| Pending-handoff status line (`cli/_formatters.py:84`) | `"Pending handoff: {pending_handoff}"` | `meta.status="awaiting_phase_handoff"` + `pending_handoff.decision_artifact_exists` | typed-field | covered-now (A1) | T1 S2; capture: `decision_artifact_exists=false` |
| Non-interactive resume-blocked hint (`pipeline/control/resume_preflight.py:167`) | `"… is paused on an undecided phase handoff … cannot resume until it is decided."` | `orcho_run_resume` returns structured pending-decision (issue + `orcho_phase_handoff_decide` next-action), not a traceback | typed-field | covered-now (A1) | T1 S2 next_actions `[(orcho_run_resume, optional=False)]`; lifecycle `_pending_decision_response` |
| Suggested next action (implicit in CLI prompt flow) | menu guides operator | `pending_handoff.suggested_next_action` | typed-field | covered-now (A1) | capture: `"call orcho_phase_handoff_decide … then orcho_run_resume"` |

### A2 — Handoff decision

| CLI affordance (file:line) | CLI string | MCP equivalent | MCP form | Coverage | Evidence |
| --- | --- | --- | --- | --- | --- |
| Action menu — `continue` (`handoff_prompt.py:64`) | `"1) ✅ continue …"` | `available_actions` ∋ `continue`; `orcho_phase_handoff_decide(action="continue")` | typed-field | covered-now (A2) | T1 S2 `available_actions` |
| Action menu — `retry_feedback` (`handoff_prompt.py:65`) | `"2) 🔁 retry_feedback …"` | `decide(action="retry_feedback", feedback=…)`; choice carries `requires_feedback=True` | typed-field | covered-now (A2) | T1 S2 (→done), S9 `retry_feedback.requires_feedback=True` |
| Action menu — `halt` (`handoff_prompt.py:66`) | `"3) 🛑 halt …"` | `decide(action="halt", note=…)` → `halt_reason="phase_handoff_halt"` | typed-field | covered-now (A2) | T1 S4 |
| Action menu — `continue_with_waiver` (`handoff_prompt.py:67`) | `"4) 📝 continue_with_waiver …"` | `decide(action="continue_with_waiver", feedback=…)` → durable `phase_handoff_waiver.waiver_text` | typed-field | covered-now (A2) | T1 S3 |
| Feedback-required prompt (`handoff_prompt.py:558-564`) | `"Feedback … (required for retry_feedback). End with an empty line:"` | `choice.requires_feedback` + `feedback_field` + `feedback_placeholder` + native elicitation contract | typed-field | covered-now (A2) | T1 S9 (elicitation supplies missing feedback) |
| Bad-input guards (`handoff_prompt.py:524-545`): empty / pasted-feedback / unknown / unavailable action | `"Unknown action …"`, `"Action … is not in this handoff's available_actions …"` | structured validation error (typed `InvalidPlanError`), not opaque string | typed-field | covered-now (A2) | T1 S9 "no placeholder feedback leaked into choice.args"; lifecycle `map_command_errors` |
| Advisory action `advice` (`handoff_prompt.py:102,408`) | `"5) 💡 advice — explain the rejection and recommend …"` | — no MCP action; `available_actions` excludes it | absent | **open-gap · OUTSIDE** | T1 S2/S9 `available_actions=[continue,retry_feedback,halt,continue_with_waiver]` only |
| Advisory action `retry_with_advice` (`handoff_prompt.py:102,412`) | `"6) 🤖 retry_with_advice — generate repair feedback from the findings and retry"` | — no MCP action / no auto-feedback generation | absent | **open-gap · OUTSIDE** | same capture; not in A2's four-verb vocabulary |

### A3 — Human-retry / repeated-reject

| CLI affordance (file:line) | CLI string | MCP equivalent | MCP form | Coverage | Evidence |
| --- | --- | --- | --- | --- | --- |
| Pre-retry banner header + `action`/`round` lines (`handoff_banners.py:197-206`) | `"┌─ retry_feedback …" / "action : retry_feedback ({kind_label})" / "round : {round_label}"` | `retry_state.retry_context` + `retry_attempt_label` | typed-field | covered-now (A3) | capture: `retry_context="automatic_reject"`, `retry_attempt_label="validate_plan automatic round 2/2"` |
| Pre-retry banner `feedback :` line (`handoff_banners.py:206`) | `"feedback : {sanitize_feedback_preview(feedback)}"` | `retry_state.operator_feedback_preview` | typed-field | covered-now (A3) | capture (post-decision): `operator_feedback_preview="address F1"` |
| Post-retry banner — approved (`handoff_banners.py:219`) | `"approved — handoff closed; the run continues …"` | `retry_state.retry_context="retry_accepted_closed"` + `pending_operator_decision=False` | typed-field | covered-now (A3) | capture: `retry_attempt_label="validate_plan human retry accepted; handoff closed"` |
| Post-retry banner — rejected again (`handoff_banners.py:224`) | `"rejected again — the retry did not satisfy the reviewer; the run is paused for a new operator decision."` | `retry_state.retry_context` (`retry_rejected_again` lifecycle state) + `pending_operator_decision=True` | typed-field | covered-now (A3) | schema `RetryState` lifecycle; A3 transition observed automatic_reject→accepted_closed in capture |

### A4 — Follow-up lineage

| CLI affordance (file:line) | CLI string | MCP equivalent | MCP form | Coverage | Evidence |
| --- | --- | --- | --- | --- | --- |
| Active follow-up intro (`resume_prompt.py:157`) | `"Run {id} has an in-progress follow-up: {child} (status: {child_status} …)"` | `lineage.parent_run_id` / `parent_status` / `active_child_run_id` / `active_child_status` | typed-field | covered-now (A4) | T1 S6 child lineage `parent_run_id`, `parent_status="done"` |
| "Resume active follow-up [recommended]" (`resume_prompt.py:168-170`) | `"{n}) Resume active follow-up {child}  [recommended]"` | `lineage.recommended_action="resume_child"` + `recommended_run_id` + `recommendation` | typed-field (schema) | covered-by-7C (A4) | S5c/F4: `_detect_active_followup_child` needs `resume_mode=="followup"`, but the only MCP child path (`from_run_plan`) stamps `plan_source="run"`, so it never fires for an MCP child — residual A4 wiring |
| Plan-inheritance provenance (CLI implicit) | follow-up uses parent context | `meta.plan_source="run"` + `plan_source_run_id` | typed-field | covered-now (A4) | T1 S6 `plan_source="run"`, `plan_source_run_id=<parent>` |

### A5 — Worktree continuity

| CLI affordance (file:line) | CLI string | MCP equivalent | MCP form | Coverage | Evidence |
| --- | --- | --- | --- | --- | --- |
| Worktree mode banner (`isolation_setup.py:416,430`) | `"worktree: {mode_label}"` | `status.worktree_continuity.subject_mode` (+ `mode_label`, `isolation`, `path`) | typed-field | covered-now (A5) | capture `…_7acc46`: `subject_mode="same_run_retained"`, `worktree_preserved=true` |
| Retained-subject / in-place banner (`handoff_banners.py:156-157`) | `"retained retry subject …"` / `"in-place checkout …"` | `worktree_continuity.{has_worktree,isolation,is_followup_continuity}` | typed-field | covered-now (A5) | capture: `has_worktree=true`, `isolation="per_run"`, `is_followup_continuity=false` |
| Blocked-worktree error (`isolation_setup.py:271` `print_error(block_message)`) | core `block_message` printed to stderr | `worktree_continuity.blocked` + `block_message` — typed fields, but **projected from the spawn-time decision, not rechecked against disk** | typed-field (not live) | covered-by-7C (A5) | S5b/F8: after the worktree dir is deleted, `blocked` stays `false`, `block_message`/`degraded_reason` stay `null` — A5 owns surfacing a stale worktree but the live recheck is a residual |

### A6 — Provider-session fallback

| CLI affordance (file:line) | CLI string | MCP equivalent | MCP form | Coverage | Evidence |
| --- | --- | --- | --- | --- | --- |
| Provider-session line (`handoff_banners.py:189-191`) | `"resume (falls back to a fresh session on miss)"` / `"fresh session (persisted run context preserved)"` | `events_summary.provider_session_fallbacks[]` (`phase`, missing id, `fallback_mode`, worktree preserved, phase succeeded) | typed-field | covered-by-7C (A6) | capture: list present but **empty** — mock never triggers a provider miss; end-to-end population needs a real provider session |
| Post-retry fresh-session outcome (`handoff_banners.py:229-232`) | `"provider-session resume was unavailable; the retry ran on a fresh provider session …"` | same `provider_session_fallbacks[]` entry + `retry_state` | typed-field | covered-by-7C (A6) | not observable under mock; schema present |

### A7 — Verification receipts

| CLI affordance (file:line) | CLI string | MCP equivalent | MCP form | Coverage | Evidence |
| --- | --- | --- | --- | --- | --- |
| Verification-environment evidence (CLI surfaces interpreter/checks in transcript) | interpreter / import / command output in run log | `orcho_run_evidence(slice="verification_receipts")` → `VerificationReceiptRecord` (`python`, `cwd`, `checks[]`, `commands[].exit_code`, `all_passed`, `artifact_path`) | typed-field | covered-now (A7) | capture `…_6406b0`: 1 receipt, `python="3.12.9 (<venv>)"`, import check + command `exit_code=0`, `artifact_path` set |

### A8 — Schema / docs

| CLI affordance | MCP equivalent | MCP form | Coverage | Evidence |
| --- | --- | --- | --- | --- |
| (CLI has no schema surface — A8 is an MCP-internal contract) | every field above is a Pydantic model in `src/orcho_mcp/schemas/` mirrored in `docs/mcp_schema.json` | typed-field | covered-now (A8) | T1 surfaces resolved against typed models; `docs/mcp_schema.json` is the snapshotted contract |

### Rows OUTSIDE the eight parity areas (open-gaps)

These operator affordances map to **none** of A1–A8; the run-control parity set
does not close them. Each is grounded in a T1 trace or the CLI source.

| Affordance (CLI file:line) | CLI form | MCP form | Coverage | Evidence |
| --- | --- | --- | --- | --- |
| `advice` advisory action (`handoff_prompt.py:408`) | `"5) 💡 advice — explain the rejection …"` | absent | open-gap · OUTSIDE | A2 vocabulary is four verbs; advice not exposed (T1 S2/S9) |
| `retry_with_advice` advisory action (`handoff_prompt.py:412`) | `"6) 🤖 retry_with_advice — generate repair feedback …"` | absent | open-gap · OUTSIDE | no MCP auto-feedback-from-findings action |
| Non-git project recovery (`project_discovery_prompt.py:163,189`) | `"No git repo found inside. Create a local git repo here?"` / `"Registering as no-git project; worktree isolation unavailable …"` | prose-only (log) — run ends `interrupted` with empty typed errors; "not a git repository / run `git init`" only in `runner.log` | open-gap · OUTSIDE | T1 F6 / S9 first attempt: `error_summary=None, errors=[]` |
| Multiple/nested git-repo picker (`project_discovery_prompt.py:68,199`) | `"Multiple git repos found … Pick the one to use:"` / `"Found nested git repo … Use it as git root?"` | absent | open-gap · OUTSIDE | no MCP project-discovery/disambiguation surface |
| Resume of an already-terminal run (`resume_preflight` covers *undecided*, not *terminal*) | CLI preflight prints a paused/decided guard line | `orcho_run_resume` on a `halted` run returns success-shaped result (fresh pid, `next_actions=[]`); run stays `halted`; no typed obsolete signal | typed-field (misleading) | open-gap · OUTSIDE | S5a / F3 |
| Delivery-gate (`final_acceptance`) verdict + committed state | `run.end` summary prose `"… \| final_acceptance=ok"` | present in **raw** `meta.commit_delivery` (`status`, `release_verdict`), but only as untyped dict keys — no typed `delivery.status`/`verdict` projection on `RunStatus` | raw-dict (untyped) | open-gap · OUTSIDE | S8 / F1 |
| Findings resolved vs active | CLI re-renders findings per round | no typed resolved/active classification — findings live on the active handoff only (`phase_handoff.artifacts.findings[]`) | absent | open-gap · OUTSIDE | S8 / F9 |
| Rejecting delivery-gate exercise | CLI can reach a blocking `final_acceptance` | absent under mock — only `mock_validate_plan_reject` exists; no `mock_final_acceptance_reject` | absent | open-gap · OUTSIDE | S8 / F2 |
| `commit_delivery_failed` diagnostic | core writes the cause to the run log | raw `meta.commit_delivery.status="commit_failed"` is the only structured signal; the granular cause is log-only; nondeterministic | raw-dict + log | open-gap · OUTSIDE | S6 / F5 |

### Reconciliation summary

- **A1, A2 (four-verb), A3, A4 (lineage records), A5 (mode banners), A7, A8 are
  covered-now** — this surface already exposes typed equivalents for the CLI's
  pending-handoff, decision, retry-lifecycle, lineage, worktree-mode,
  verification-receipt, and schema affordances. No CLI affordance in these rows is
  prose-only or absent on the MCP side.
- **covered-by-7C** holds three residuals inside the set: the follow-up
  active-child recommendation never fires for MCP-created children because
  `from_run_plan` sets `plan_source="run"` not `resume_mode="followup"` (A4); the
  blocked-worktree fields are not rechecked against disk, so a stale worktree stays
  `blocked=false` (A5); and provider-session fallback population (A6) is not
  observable under mock (needs a real provider-session miss to confirm end-to-end).
- **The substantive open-gaps are all OUTSIDE A1–A8**: the `advice` /
  `retry_with_advice` advisory actions, non-git project discovery/recovery, the
  terminal-resume silent no-op, findings resolved-vs-active, and the delivery-gate
  verdict / diagnostic / reject-exercise cluster. These are the rows Stage 7C will
  not close and are the input to the gap spec. (Stale-worktree recheck and the
  active-child recommendation are **not** here — they sit inside Stage 7C as the
  A5/A4 residuals above, closed by completing those areas rather than by a new
  contract.)
