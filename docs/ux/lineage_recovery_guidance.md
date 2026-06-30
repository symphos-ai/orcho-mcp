# Lineage-Aware Recovery Guidance

When a run chain contains both an original failed/interrupted run with a
resumable checkpoint and a later terminal recovery run that rejected or failed
to deliver, a captain must not guess between `orcho_run_resume`, follow-up, a
delivery decision, and `from_run_plan`. MCP projects the durable lineage and
recommends the safe continuation subject.

Both `orcho_run_diagnose` and `orcho_run_status` carry the same typed
recommendation, projected from one resolver (`services.run_lineage`):

- `orcho_run_diagnose` → `condition` + `continuation_subject` +
  `recommended_next_action` + `recovery_lineage`.
- `orcho_run_status` → `recovery_recommendation` (same
  `continuation_subject` / `recommended_next_action` / `recommended_run_id`),
  `null` for an ordinary run with no terminality and no active child.

The recommendation is built only from `meta.json`, merged status, the worktree
block, the delivery-gate projection, and SDK readers — never from raw output
logs or provider transcripts.

## The four cases

| Case | `continuation_subject` | `recommended_next_action` | Safe action |
|---|---|---|---|
| A — resumable source exists | `source_run_checkpoint` | `resume_source_run` | `orcho_run_resume(run_id=<source>)` — resume the original run's checkpoint. The inspected terminal run is **not** the subject. |
| B — active child exists | `active_child_run` | `resume_active_child` | `orcho_run_resume(run_id=<child>)` — resume the live follow-up child, not this parent. |
| C — plan-only subject | `plan_artifact` | `plan_artifact_continuation` | `orcho_run_start(from_run_plan=<run>, profile=…)` — implement the persisted plan artifact as a **new** run. |
| D — no known subject | `unknown` | `stop_unknown` | Stop and inspect. `recovery_lineage.missing_facts` enumerates exactly which durable facts are absent. **No `from_run_plan`.** |

A pending delivery / correction gate is its own case
(`continuation_subject='delivery_gate'`, `recommended_next_action='delivery_decision'`),
resolved with `orcho_delivery_decide`.

### Case A — resumable source

A run is the source subject when the inspected run is a terminal / rejected
dead-end (terminal success, a terminal halt reason, or a rejected release with
no decidable gate) AND a durably linked source run (via `meta.parent_run_id`
or `meta.plan_source_run_id`) is itself **resumable** — not a
terminal-resume-parent, and still owning retained work (a preserved worktree
or a persisted plan). MCP recommends resuming the source's checkpoint, because
the source still owns the retained diff.

`orcho_run_resume` enforces this as a pre-flight guard: resuming the terminal
recovery run does **not** spawn — it returns `ResumeBlockedResult`
(`resume_outcome='recover_via_source_run'`) pointing at the source. Enforced by
`tests/unit/run_control/test_resume_outcome.py`.

### Case C — plan-only continuation

The plan-only subject requires BOTH a `meta.plan_source` stamp (`local` /
`run` / `cross`) AND a durable, readable `parsed_plan.json` artifact on disk,
with no undelivered diff / retained worktree, on a `planning` / `research`
profile. A `plan_source` stamp without a persisted plan artifact is **not** a
plan subject (it degrades to `unknown`).

## `from_run_plan` is not "finish the last diff"

`from_run_plan` means **"implement this plan artifact from scratch as a new
run"**. It is recommended ONLY for Case C
(`recommended_next_action='plan_artifact_continuation'`).

For a retained diff or checkpoint the correct primitive is `orcho_run_resume`
of the original run (Case A) or the active child (Case B). MCP never offers
`from_run_plan` as a generic way to finish a retained diff, and never as a
fallback for Case D.

## Dogfood trace

```
20260620_083233_fc2da4  (source)   failed in review_changes, retained worktree
        │  parent_run_id
        ▼
20260622_012155_474b85  (recovery) terminal, final acceptance rejected, no gate

diagnose(474b85):
  condition               = recover_via_source_run
  continuation_subject    = source_run_checkpoint
  recommended_next_action = resume_source_run
  recommended_run_id      = 20260620_083233_fc2da4
  next_actions            = [ ready_call orcho_run_resume(run_id=…fc2da4) ]   # no from_run_plan

correct action:  orcho_run_resume(run_id=20260620_083233_fc2da4)
wrong action:    orcho_run_start(from_run_plan=474b85)   # a new impl from a plan, not a continuation
```

The incident's mistake was starting a new `from_run_plan` run against the
terminal recovery run. The correct action was to diagnose `474b85` and resume
the source `fc2da4`.

## Enforcing tests

- `tests/unit/services/test_run_lineage_recovery.py` — the resolver's four
  cases plus the degrade-to-unknown behaviour.
- `tests/unit/services/test_run_diagnosis.py` — the `recover_via_source_run`
  condition, plan-only / stop-unknown enrichment, and the regression that an
  ordinary resumable failed child resumes itself.
- `tests/unit/run_control/test_resume_outcome.py` — the resume pre-flight does
  not spawn a terminal recovery run and points at the source.
- `tests/unit/services/test_run_reads.py` — `recovery_recommendation` on
  `orcho_run_status` and its agreement with `orcho_run_diagnose`.
