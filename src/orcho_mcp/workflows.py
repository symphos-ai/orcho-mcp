"""orcho_mcp.workflows — workflow prompt templates (MCP UX A2).

Implements Principle 2 of the orcho-mcp UX vision
(``docs/ux_vision.md`` §4): workflows that span multiple tool calls
are surfaced as **named prompt templates** so clients (Claude Code,
Cursor) can offer them as slash commands. The LLM does not have to
infer the workflow from individual tool descriptions — the operator
explicitly invokes the workflow they want.

Separate from ``orcho_mcp.prompts`` (which dynamically registers one
prompt per ``_prompts/*.md`` core template). Those expose the **methodology**
(plan-reviewer / code-reviewer / etc.). This module exposes the
**operations**: multi-step workflows that orcho-core composes via
its CLI flags.

Seven workflows ship:

* :func:`orcho_plan_then_implement` — full two-step pipeline (planning
  profile then ``--from-run-plan`` child run on feature).
* :func:`orcho_followup_from_plan` — only the child-run side, when
  the operator already has a parent run id.
* :func:`orcho_review_paused_run` — guide the LLM to inspect a
  paused run's handoff payload and propose a decide action.
* :func:`orcho_halt_with_reason` — generate a structured halt
  request with audit-trail note.
* :func:`orcho_resume_failed_run` — guide the LLM through
  ``--resume`` semantics and parent-state classification.
* :func:`orcho_observe_active_run` — drive the resilient short-watch
  + summary-fallback observation loop for an in-flight run.
* :func:`orcho_inspect_delivery_gate` — classify an Orcho-managed run's
  post-release delivery / correction gate and resolve it through
  ``orcho_delivery_decide``, never by applying the retained diff by hand.

Registration is via the standard ``@mcp.prompt`` decorator on the
canonical FastMCP instance from :mod:`orcho_mcp.instance` (per the
dual-import anti-pattern documented in
``orcho-mcp/CLAUDE.md`` §"MCP anti-patterns" #1).

Each prompt's return value is the templated message body sent to
the LLM. The body explicitly names which tools to call and in what
order — workflow guidance lives in the prompt, not in the tool
descriptions (Principle 4: single intent per tool, workflow per
prompt).
"""
from __future__ import annotations

from orcho_mcp.instance import mcp

# ── 1. Plan-then-implement (full two-step) ──────────────────────────────────


@mcp.prompt(
    name="orcho_plan_then_implement",
    description=(
        "Two-step workflow: spawn a planning-profile run, wait for it to "
        "pause at the planning handoff, inspect the plan, then spawn a "
        "child run with --from-run-plan that uses the feature profile "
        "for actual implementation. Use this when you have a task and "
        "want explicit operator review of the plan before agents touch "
        "code."
    ),
)
def orcho_plan_then_implement(
    task: str,
    project_dir: str,
) -> str:
    """Two-step workflow guidance: plan → review → implement."""
    return f"""I want to execute this task through Orcho's two-step plan-then-implement workflow:

**Task**: {task}
**Project directory**: {project_dir}

Please drive the workflow:

1. Start a planning-profile run via `orcho_run_start` with:
   - task = "{task}"
   - project_dir = "{project_dir}"
   - profile = "planning"

2. The planning profile produces a parsed plan and pauses on
   awaiting_phase_handoff. Poll `orcho_run_status(run_id)` until that
   status appears. The status payload's `next_actions` field will
   surface the decide options + a from-run-plan suggestion.

3. Read the parsed plan via the `orcho://runs/<run_id>/parsed_plan.json`
   resource (or via `orcho_run_evidence`). Show me the plan summary
   plus the per-task decomposition. Pause for my review before
   continuing.

4. After I approve the plan, spawn the implementation run via
   `orcho_run_start` with:
   - from_run_plan = "<the parent run id from step 1>"
   - profile = "feature"
   - task = "{task}" (inherits from parent meta automatically if
     omitted, but supplying it keeps the child run self-describing)
   - project_dir = "{project_dir}" (same)

5. Poll the child run's `orcho_run_status` until it reaches a
   terminal state. Surface evidence + any handoff decisions to me.

If the plan looks wrong before step 4, suggest one of the
`orcho_phase_handoff_decide` actions from the parent run's
`next_actions` (continue / retry_feedback / continue_with_waiver /
halt) instead of spawning the child run.
"""


# ── 2. Followup from existing plan ──────────────────────────────────────────


@mcp.prompt(
    name="orcho_followup_from_plan",
    description=(
        "Spawn an implementation run that inherits the parsed plan from "
        "a parent run (--from-run-plan). Use this when you already have "
        "a paused or completed planning-profile run and want to move it "
        "to implementation."
    ),
)
def orcho_followup_from_plan(
    parent_run_id: str,
    profile: str = "feature",
) -> str:
    """Single-step: spawn child run from an existing parent's plan."""
    return f"""Spawn a new Orcho run that inherits the parsed plan from a parent run.

**Parent run id**: {parent_run_id}
**Child profile**: {profile}

Workflow:

1. Verify the parent has a persisted plan: call
   `orcho_run_status("{parent_run_id}")`. The status payload's
   `next_actions` should include an `orcho_run_start` suggestion
   with `from_run_plan = "{parent_run_id}"`. If it does not, the
   parent did not persist a `parsed_plan.json` (older run, dry-run,
   or planning failed) and you cannot use --from-run-plan against it.

2. If the parent has a plan, spawn the child via `orcho_run_start`:
   - from_run_plan = "{parent_run_id}"
   - profile = "{profile}"
   - Omit `task` and `project_dir` unless you want to override
     them; both inherit from parent meta automatically.

3. Poll the child run's `orcho_run_status` until terminal. The
   child run skips the parent's planning block (plan + validate_plan
   phases) and starts at implement with the parent's plan already
   hydrated as state.parsed_plan.

If the profile is incompatible (e.g. planning, code_review), the
orchestrator fails fast before any work runs — pick a profile that
has phases downstream of planning, such as feature or complex_feature.
(The internal `task` profile also has downstream phases but is not a
public work-kind choice.)
"""


# ── 3. Review paused run ────────────────────────────────────────────────────


@mcp.prompt(
    name="orcho_review_paused_run",
    description=(
        "Inspect a run that is paused on a phase handoff (typically "
        "validate_plan rejected or human-feedback policy fired) and "
        "propose a decide action: continue, retry_feedback, "
        "continue_with_waiver, or halt. "
        "Use this when `orcho_run_status` returns status = "
        "awaiting_phase_handoff and you want a structured review "
        "before committing to a decision."
    ),
)
def orcho_review_paused_run(run_id: str) -> str:
    """Guide the LLM through inspecting + deciding a paused run."""
    return f"""Review the paused Orcho run {run_id!r} and propose a phase-handoff decision.

If you do not have the run id (the watcher or session dropped), recover it
first with `orcho_workspace_pending_decisions` — it lists the runs paused on
`awaiting_phase_handoff` straight from the workspace artifacts. By default it
is the operator's decision inbox: it returns only `actionable` rows (project
exists under the workspace scope) and hides missing/temp/out-of-workspace runs,
tallying them in `hidden_count` + a per-reason breakdown. In the standard
`workspace-orchestrator` layout the scope includes the parent project group,
so sibling project repos stay actionable. Workspace-valid beats the temp
heuristic — a project under the workspace scope stays actionable even when
that scope itself lives in a temp/test directory. Pass `include_stale=true`
for a forensic view of *every* paused run, each carrying its real
`classification`; the `hidden_*` counters are window-bounded and read the same
in both modes. A row whose `decision_artifact_exists` is true already has a
recorded decision: skip the review and call `orcho_run_resume` directly — do
NOT decide again.

Workflow:

1. Call `orcho_run_status("{run_id}")`. Confirm status is
   `awaiting_phase_handoff`; if not, the run is not actually paused
   on a handoff and this workflow does not apply.

2. The status payload carries:
   - `meta.phase_handoff` — the active payload with `handoff_id`,
     `phase`, `verdict`, `available_actions`, and (for rejected
     verdicts) findings.
   - `next_actions` — pre-filled decide actions for each available
     verb (continue, retry_feedback, continue_with_waiver, halt).

3. Read the reviewer's findings via either:
   - the `meta.phase_handoff` payload's `findings` field, OR
   - `orcho_run_evidence("{run_id}", slice="findings")` for the
     structured slice.

4. Surface a decision recommendation to me with:
   - **Verdict summary**: was the verdict APPROVED or REJECTED?
   - **Key findings** (max 5): list the specific defects the
     reviewer flagged.
   - **Recommended action**: which of `continue` / `retry_feedback` /
     `continue_with_waiver` / `halt` makes sense, and why.
   - If `retry_feedback` or `continue_with_waiver`: draft the feedback
     text addressing the findings (operator-grade, no fluff).
     `continue_with_waiver` requires non-empty feedback — it records
     why the findings are being accepted rather than fixed.

5. Wait for my explicit confirmation before calling
   `orcho_phase_handoff_decide`. The decision API is a one-way state
   transition; double-check before pulling the trigger.

6. After my confirmation, call `orcho_phase_handoff_decide` with
   the args from `next_actions`. For `continue` / `retry_feedback` /
   `continue_with_waiver`, immediately follow up with
   `orcho_run_resume("{run_id}")` —
   the decision API only writes the artifact; resume advances the
   run.
"""


# ── 4. Halt with reason ─────────────────────────────────────────────────────


@mcp.prompt(
    name="orcho_halt_with_reason",
    description=(
        "Terminate a run via phase-handoff halt with a structured "
        "audit-trail note explaining the operator's reason. Use this "
        "when a paused run should NOT proceed — e.g. the task scope "
        "changed, the plan is fundamentally wrong, or the run hit an "
        "unrecoverable defect."
    ),
)
def orcho_halt_with_reason(run_id: str, reason: str) -> str:
    """Guide the LLM through a structured halt request."""
    return f"""Halt the Orcho run {run_id!r} via phase-handoff decision.

**Reason**: {reason}

Workflow:

1. Confirm the run is in a halt-able state: call
   `orcho_run_status("{run_id}")` and verify status =
   `awaiting_phase_handoff`. (Other terminal states like `halted`,
   `failed`, or `done` are not halt-able — they are already
   terminal. If the run is `running`, use `orcho_run_cancel`
   instead; halt is for paused runs.)

2. The status's `next_actions` will include an `orcho_phase_handoff_decide`
   with `action = "halt"`. Use those args directly — they include
   the correct `handoff_id`.

3. Call `orcho_phase_handoff_decide` with:
   - run_id = "{run_id}"
   - handoff_id = <from next_actions>
   - action = "halt"
   - note = "{reason}"

4. Halt is terminal: meta.status flips to `halted`, the active
   handoff payload clears, and no further phases will execute. The
   decision artifact under `runs/{run_id}/phase_handoff_decisions/`
   records the halt with the supplied note for audit.

5. After halt, the post-decide `next_actions` will surface
   resume + from-run-plan suggestions for the operator's next
   move. If the plan was retained, `from-run-plan` is a clean way
   to fork a fresh implementation run from the halted run's plan.

Do NOT halt without confirming the reason matches what I asked
for — halt is irreversible (you cannot un-halt; you'd have to
spawn a fresh run).
"""


# ── 5. Resume a failed/halted run ───────────────────────────────────────────


@mcp.prompt(
    name="orcho_resume_failed_run",
    description=(
        "Continue an interrupted or failed run from its last "
        "checkpoint via --resume. Classifies the parent run's state "
        "first (terminal-success runs can't checkpoint-resume; "
        "halted-by-handoff runs can't resume at all and need "
        "--from-run-plan instead)."
    ),
)
def orcho_resume_failed_run(run_id: str) -> str:
    """Guide the LLM through --resume parent-state classification."""
    return f"""Resume the Orcho run {run_id!r} from its checkpoint, if possible.

Workflow:

1. Call `orcho_run_status("{run_id}")`. Read `meta.status`:

   - `running` — the run is alive; resume is not applicable. Tell
     me and stop.
   - `done` / `success` / `completed` — the run finished successfully.
     `--resume` cannot reset a successful run. If I want a new run
     with the same task / plan, suggest `orcho_run_start` with
     `from_run_plan = "{run_id}"` instead.
   - `failed` / `interrupted` — the run crashed mid-flight. These
     ARE resumable from checkpoint — proceed to step 2.
   - `halted` — the run was halted via phase-handoff decision. Check
     `meta.halt_reason`:
       * `phase_handoff_halt` — operator chose halt. Resume is
         refused for this terminal state. Suggest spawning a fresh
         run via `orcho_run_start` (with `from_run_plan = "{run_id}"`
         if a plan was retained).
       * Other halt reasons — case-by-case; surface the reason and
         ask me before deciding.
   - `awaiting_phase_handoff` — the run is paused waiting for a
     decide. Use `orcho_review_paused_run` workflow instead of
     resume.

2. For resumable states (`failed`, `interrupted`), call
   `orcho_run_resume("{run_id}")`. The supervisor spawns a new
   subprocess that loads the existing checkpoint and continues from
   the last successful phase boundary.

3. Poll `orcho_run_status` after resume to confirm the run is
   alive and progressing. If it pauses again on a handoff, that's
   normal — switch to `orcho_review_paused_run`.

Resume reuses the parent's provider session (Claude / Codex
session id) when available. If the session has expired (long-paused
run, key rotation, cross-machine), the agent may degrade silently
— the run completes but loses context. See the over-run follow-up
plan §6 "Reactive Session-Expiry Recovery" for the future fix.
"""


# ── 6. Observe an active run (resilient watch loop) ─────────────────────────


@mcp.prompt(
    name="orcho_observe_active_run",
    description=(
        "Follow an in-flight run with a resilient observation loop: a short "
        "bounded orcho_run_watch, then orcho_run_events_summary as the "
        "reconnect fallback when the watch times out or the client transport "
        "drops the long-poll, then watch again from the reconnect cursor. "
        "Use this when you want live progress on a running run without "
        "treating a dropped watch as a failed run. If you have lost the run "
        "id (watcher/session dropped), recover it first with "
        "orcho_workspace_pending_decisions, whose default view is the "
        "operator inbox of actionable paused runs (noise hidden but counted "
        "in hidden_*); pass include_stale=true for a forensic view of every "
        "paused run with per-row classification."
    ),
)
def orcho_observe_active_run(run_id: str) -> str:
    """Guide the LLM through the short-watch + summary-fallback loop."""
    return f"""Observe the in-flight Orcho run {run_id!r} with a resilient watch loop.

The pattern is a short bounded watch, a summary fallback on
timeout/disconnect, and a reconnect from the cursor — repeated until the
run reaches a handoff or terminal state.

**Recovering attention after a watcher/session loss.** If the watch loop
or the whole session dropped and you no longer hold the run id (or are not
sure which runs are waiting on you), do NOT guess from chat memory. Call
`orcho_workspace_pending_decisions` first — it scans the workspace
artifacts and, by default, returns the operator inbox: only `actionable`
paused runs (project exists under the workspace scope), hiding
missing/temp/out-of-workspace runs while counting them in `hidden_count` +
breakdown. In the standard `workspace-orchestrator` layout the scope includes
the parent project group, so sibling project repos stay actionable.
Workspace-valid beats the temp heuristic, so a project under the workspace
scope stays actionable even when that scope is under a temp/test directory.
Pass `include_stale=true` for a forensic view of every paused run with its
real per-row `classification`; the window-bounded `hidden_*` counters read the
same in both modes. Each returned row carries the typed next step:

- a row whose `decision_artifact_exists` is false carries an
  `operator_input_required` `orcho_phase_handoff_decide` (choose a verb,
  supply feedback where required), then `orcho_run_resume`;
- a row whose `decision_artifact_exists` is **true** already has a recorded
  decision, so its `next_actions` is a ready `orcho_run_resume` —
  **the next step is resume, not a second `orcho_phase_handoff_decide`.**

Pick the run you care about from that list, then either re-enter the watch
loop below (if it is still running) or follow the row's `next_actions`
(decide → resume, or resume directly when a decision is already recorded).

Loop:

1. Call `orcho_run_watch` with:
   - run_id = "{run_id}"
   - until = "subtask" (or "handoff_or_terminal" if you only care about
     pauses and completion)
   - timeout_s = 240 (keep it short — 120-240s — so the call returns
     promptly with a fresh reconnect cursor instead of holding one
     long-lived request open for an hour)

2. Inspect the result:
   - If `result.triggered` is true, act on `result.trigger.kind`
     (`subtask` / `phase_change` / `handoff` / `terminal`). For a
     `handoff` switch to `orcho_review_paused_run`; for `terminal` stop
     and report the outcome.
   - If `result.trigger.kind == "timeout"` (or the watch call dropped /
     the transport severed the long-poll), this is **observer loss, not a
     failed run** — the run keeps executing in its worktree.

3. On timeout/disconnect, reconnect rather than re-deciding the run:
   - Call `orcho_run_events_summary(run_id="{run_id}", since_seq=<cursor>)`
     where `<cursor>` is `result.summary.next_seq` (or
     `result.trigger.seq` when you called with summary=False) to catch up
     on what happened while disconnected.
   - Read `status` / `pending_handoff` from that summary before continuing.

4. Resume the loop: call `orcho_run_watch` again with `since_seq` set to
   the summary's `next_seq`. Repeat steps 1-4.

**A disconnected watch is not a failed run.** Never treat a timed-out or
dropped watch as a reason to retry, cancel, or halt the run. Inspect the
summary / status first, and take any continue / retry / halt decision only
from the typed `status` / `pending_handoff` / terminal / evidence signals —
never from the fact that a watch call ended.
"""


# ── 7. Inspect an Orcho-managed delivery / correction gate ──────────────────


@mcp.prompt(
    name="orcho_inspect_delivery_gate",
    description=(
        "Decide what to do with a stopped Orcho-managed run by inspecting its "
        "post-release delivery / correction gate (orcho_delivery_gate), then "
        "resolving the decision with the gate's ready orcho_delivery_decide "
        "calls. Use this when a run stopped after release and you must choose "
        "approve / apply / fix / skip / halt — it keeps you from applying the "
        "retained worktree diff by hand or confusing an Orcho-managed gate "
        "with a plain direct checkout edit."
    ),
)
def orcho_inspect_delivery_gate(run_id: str) -> str:
    """Guide the LLM through classifying + resolving a delivery / correction gate."""
    return f"""Inspect the post-release delivery gate for Orcho run {run_id!r} and decide what to do — without touching the retained diff by hand.

Workflow:

1. Call `orcho_run_status("{run_id}")` and `orcho_run_diagnose("{run_id}")`.
   A `condition` of `needs_delivery_decision` means the run is parked at an
   Orcho-managed delivery / correction gate.

2. Call `orcho_delivery_gate("{run_id}")` for the typed projection. Branch on
   `kind` (this is the authoritative signal — never parse terminal log prose):

   - **`delivery_decision_required`** — a pending delivery on an APPROVED
     release. Review the retained change read-only via
     `orcho_run_diff("{run_id}")` and the gate's `diff` summary
     (`files_changed` / `changed_paths`), then choose one of the gate's
     `next_actions` ready calls to `orcho_delivery_decide`: approve / apply /
     skip / halt. Only `approve` creates a commit; `apply` lands the diff
     uncommitted.

   - **`correction_decision_required`** — the release was REJECTED (or the run
     is `fix_requested`). This is an available correction-flow state, NOT a
     finished delivery. Surface the rejection and the gate's `blocked_actions`,
     then choose one of the gate's ready `orcho_delivery_decide` calls. Typical
     available actions for a current rejected release are `fix` (default) and
     `halt`; shipping actions and `skip` may be blocked by core guards.

   - **`direct_checkout_or_running`** — there is NO Orcho delivery gate (a
     direct checkout edit, or a terminal / still-running run). Nothing is
     delivered through Orcho here: test and commit directly in the checkout.

3. **Never apply the retained run diff yourself for an Orcho-managed run.** Do
   not `git apply` / copy / commit the retained worktree diff, and do not
   resume the run to force delivery. Use exactly one ready
   `orcho_delivery_decide` call from `orcho_delivery_gate.next_actions`.

4. If `orcho_delivery_gate` reports `diff.degraded = true`, a secondary
   artifact (commit_decisions / diff.patch) was missing or unreadable; the gate
   `kind` is still authoritative — relay the `message` and proceed from the
   meta-derived `changed_paths`.

The delivery / correction decision itself is the operator's choice; MCP carries
it through the SDK-backed `orcho_delivery_decide` tool.
"""


__all__ = [
    "orcho_followup_from_plan",
    "orcho_halt_with_reason",
    "orcho_inspect_delivery_gate",
    "orcho_observe_active_run",
    "orcho_plan_then_implement",
    "orcho_resume_failed_run",
    "orcho_review_paused_run",
]
