# Orcho MCP — Run Lifecycle

For the complete state and decision graph across lifecycle, supervisor,
handoff, delivery, lineage, and control-authority axes, see
[MCP Control State Machine](architecture/control_state_machine.md). This
document remains the per-tool wire contract.

Canonical contract for how runs are started, observed, resumed, and
cancelled through the MCP wire. The naming convention is
**`orcho_run_<verb>`** for everything that operates on a run. The
phase-handoff state transition
(`orcho_phase_handoff_decide`) sits beside this surface with its own
name.

---

## Tool naming

The seven process-control tools share a `orcho_run_` prefix:

| Tool | Verb | Purpose |
|---|---|---|
| `orcho_run_start` | start | Spawn a new pipeline subprocess. Returns immediately. |
| `orcho_run_resume` | resume | Re-spawn a subprocess against an existing run dir, loading checkpoint. |
| `orcho_run_cancel` | cancel | Signal the run's process group (graceful or hard). |
| `orcho_run_status` | status | What is happening / what should I do next? Summary snapshot for one run (status / phase progress / metrics summary / lineage / attention signals / next actions). Phase bodies elided by default; `include` opts back in. |
| `orcho_run_history` | history | List recent runs, newest first. |
| `orcho_run_metrics` | metrics | How much did it consume? Raw `metrics.json` for tokens, durations, phase breakdown, attempts / retries, and cost-reference fields when available. |
| `orcho_run_events_tail` | events_tail | Stream events with seq-based pagination. |

Observation tools share the same prefix but never touch the process:

| Tool | Purpose |
|---|---|
| `orcho_run_watch` | Long-poll a live run; emits `notifications/progress` when the request carries a `progressToken`. A watch timeout is observer loss, not run failure. |
| `orcho_run_live_status` | Lightweight live-progress projection for a running pipeline. |
| `orcho_run_events_summary` | Aggregated event-stream projection (counts per kind/phase). |
| `orcho_run_diff` | What changed? File stats, bounded preview, or full patch from the run's retained diff, optionally scoped to a path or phase. |

The phase-handoff decision is a separate concern — it's a state
transition, not a process action — so it sits beside the run-lifecycle
group with its own name:

| Tool | Purpose |
|---|---|
| `orcho_phase_handoff_decide` | Resolve a paused phase handoff (`awaiting_phase_handoff`). Writes a decision artifact under `<run_dir>/phase_handoff_decisions/{safe_handoff_id}.json`. `continue` / `retry_feedback` / `continue_with_waiver` leave `meta.status` paused for a follow-up `orcho_run_resume`; `halt` flips `meta.status` to `halted` synchronously. Pure state transition; never spawns. |
| `orcho_handoff_advice` | LLM advisor for a paused handoff: recommends the smallest honest next action and writes a durable advice artifact. Advice, never a decision. |
| `orcho_delivery_decide` | Resolve a parked post-release delivery / correction gate. Delegates to orcho-core's SDK decision entrypoint; the SDK owns all delivery guards and state mutation. Pure state transition; never spawns. |

The inspection surface is also a separate concern — it reads
existing artifacts and projects them into typed slices, no process
involved:

| Tool | Purpose |
|---|---|
| `orcho_run_evidence` | What happened / what proves it? Typed slices for plan summary, findings (severity-filterable), commands, artifacts, errors+halt reason, cross-run sub-aliases, per-subtask delivery receipts (`subtask_dag`), verification-environment receipts, and the canonical scheduled-gate ledger (`verification_timeline` / `verification_cockpit`). Ledger rows retain identity, declaration, selection, execution, disposition, and receipt-evidence facts; events stay identity-scoped. Replaces raw log/event-jsonl reads for control-loop clients. |
| `orcho_run_diagnose` | Read-only resume-situation verdict: a typed `condition`, a one-line `reason`, and call-readiness-typed `next_actions`. Call before any risky `orcho_run_resume`. See [Diagnosing a run](#diagnosing-a-run--orcho_run_diagnose). |
| `orcho_delivery_gate` | Read-only projection of a post-release delivery / correction gate: SDK-derived kind, available actions, blocked actions, diff summary, and ready calls to `orcho_delivery_decide`. |

Non-run tools keep their independent names — they don't operate on a
specific run:

`orcho_workspace_info`, `orcho_plan_validate`, `orcho_skills_list`,
`orcho_prompts_resolve`, `orcho_profiles_list`.

## Inspection Questions

The read tools deliberately answer different questions:

| Question | MCP tool | Leads with |
|---|---|---|
| What is happening / what should I do next? | `orcho_run_status` | Current state, phase progress, attention signals, lineage, artifacts, and ready next actions. |
| What happened / what proves it? | `orcho_run_evidence` | Typed proof slices: plan, findings, commands, artifacts, errors, receipts, verification, delivery, correction, and child runs. |
| How much did it consume? | `orcho_run_metrics` | Tokens, duration, per-phase breakdown, attempts / retries, and cost-reference fields when available. |
| What changed? | `orcho_run_diff` | File stats, bounded preview, or full patch from the retained diff. |

Use `orcho_run_status` for the live operator loop. Use
`orcho_run_evidence` when explaining or auditing a completed / halted run. Use
`orcho_run_metrics` for consumption analysis. Use `orcho_run_diff` when the
question is specifically about patch content.

---

## Vocabulary

| Term | Meaning |
|---|---|
| **Run** | One attempt to execute a pipeline against a project, identified by a `run_id` and pinned to a single `run_dir` on disk. |
| **Spawn** | Create a detached subprocess that executes the pipeline. The MCP server returns *immediately* — the run progresses in the background. |
| **Lifecycle observation** | Reading the run's evolving state through `orcho_run_status` / `orcho_run_events_tail` / `orcho_run_metrics` / `orcho_run_history`. None of these mutates state. |
| **Resume** | Re-spawn a pipeline subprocess against an existing `run_dir`, loading the on-disk checkpoint. *Not* a smart "skip already-done phases" — it is a fresh process that respects whichever profile the caller picks. |
| **Cancel** | Signal the run's process group. Graceful (`SIGTERM`) lets the pipeline flush the checkpoint and emit `run.interrupted`; hard (`SIGKILL`) drops in-flight work. |

### Decision, continuation, and settlement

A recorded phase-handoff decision is idempotent: replay returns the original
decision timestamp, action, and UTF-8 feedback without rewriting the artifact.
Status, live status, diagnosis, the workspace inbox, and a reconnecting watch
then offer only resume; a decision-read failure is explicitly degraded and
offers read-only diagnosis. Resume and follow-up are preflighted by core before
any subprocess launch: a finalized same-run parent is refused without a spawn,
while an explicit retained-change follow-up creates a distinct child with
parent lineage.
When a launch exits, MCP writes the matching terminal status to both
`mcp_supervisor.json` and core's `run_supervisor.json`; reconnecting after a
watch/transport loss reads that durable settlement rather than assuming
running. This includes abnormal exits: an rc=1 is settled as `failed` with
`halt_reason="abnormal_exit:1"` across status, live status, diagnosis, and
errors evidence.

Deliberately **not** vocabulary:

- "Continue from where it stopped, automatically picking the next phase" — this is *not* what resume does.
- "Pause and unpause" — there is no generic process-pause toggle. Runs park at
  explicit durable decision points: a phase handoff, a runner-owned cross
  gate, a plan-review tail, or a post-release delivery/correction gate. Each
  has its own typed read and command contract.

---

## Starting a run — `orcho_run_start`

### Contract

- **Detached subprocess spawn.** The pipeline runs in its own process
  group (`start_new_session=True`); the MCP server is never blocked on
  pipeline progress.
- **Returns immediately** with a `RunStartedResult`:

  | Field | Type | Meaning |
  |---|---|---|
  | `run_id` | `str` | Server-minted ID `YYYYMMDD_HHMMSS_xxxxxx` (timestamp + 6 random hex chars). The caller cannot supply one. |
  | `run_dir` | `str` | Absolute path to the run's directory under `<workspace>/worktree/runs/<run_id>/`. All artefacts land here. |
  | `pid` | `int` | OS pid of the pipeline subprocess at the moment of spawn. Equals `pgid` because of `start_new_session=True`. |
  | `started_at` | `str` | ISO timestamp recorded at spawn (server clock). |
  | `project_dir` | `str` | Echo of the `project_dir` argument. Subprocess's `cwd`. |
  | `command` | `list[str]` | The full subprocess argv (for diagnostics). |
  | `next_actions` | `list[NextActionRecord]` | Ready follow-ups. After a successful spawn this carries exactly one `ready_call` to `orcho_run_watch` with `args={run_id}` — forward it verbatim to enter the observation loop immediately. |

- **Lifecycle is not implied by start.** Spawning a run does **not**
  bind the caller to anything — observe via the read tools, cancel
  via `orcho_run_cancel`, resume after interruption via
  `orcho_run_resume`.
- **`next_actions` points straight at the watch loop.** The spawn
  response's single `ready_call` `orcho_run_watch` is pre-filled with the
  new `run_id`, so the client can begin
  [resilient observation](#observing-a-run) without re-deriving the call.

### Profile selector — `profile="auto-detect"`

`profile` normally names an executable semantic profile (`feature`
default, `small_task`, `complex_feature`, `planning`, `code_review`,
…). It also accepts the **`auto-detect` selector**: a token core
resolves into a concrete profile rather than an executable recipe.
Core classifies the task, picks the matching semantic work kind, and
on this non-interactive surface proceeds with the chosen profile when
confidence is sufficient (else it falls back to the default profile).

The selector threads to the subprocess via argv (`--profile
auto-detect`) only — never through `ORCHO_PIPELINE`, which is a
concrete-profile override that would pre-resolve the selector. The
default stays `feature`.

The resolved decision is read back typed via
`orcho_run_status` → `auto_detect` (see below). `orcho_profiles_list`
lists selectors under a separate `selectors` field, disjoint from the
executable `profiles`.

### `run_id` collisions

`mint_run_id()` produces `YYYYMMDD_HHMMSS_xxxxxx` with 24 bits of
randomness on top of a 1-second timestamp. Collision within the same
second requires ~16M concurrent spawns; the supervisor caps
concurrency at `ORCHO_MCP_MAX_RUNS` (default 4). Practical collision
probability: zero. There is no collision-handling code path because no
collision is reachable.

### What spawn writes to `run_dir`

By the time `orcho_run_start` returns:

- `<run_dir>/` exists (fresh directory).
- `<run_dir>/mcp_supervisor.json` is written immediately with
  `{run_id, pid, pgid, status: "running", started_at, project_dir, command}`.
- `<run_dir>/runner.log` is opened for the subprocess's combined
  stdout+stderr.

Pipeline-side artefacts (`meta.json`, `metrics.json`, `events.jsonl`,
`evidence.json`, plan/build/QA artefacts) materialise asynchronously
as the pipeline progresses. Don't expect them at spawn time.

### Backpressure

If `len(active_runs) >= ORCHO_MCP_MAX_RUNS`, `orcho_run_start` raises
`PipelineSpawnError`. The caller's options: cancel an existing run,
or raise the cap via the env var.

### Concurrency on the same `project_dir`

Two `orcho_run_start` calls against the same `project_dir` serialise
on a per-project `asyncio.Lock` to avoid checkpoint races. Different
projects spawn in parallel up to the cap.

---

## Observing a run

None of these tools mutate state. They are safe to poll on a tight
loop (sub-second is fine — they read JSON files; no LLM calls).
The reliable observation path is cursor-based replay from durable
run state; progress and resource notifications are delivery
accelerators, not the source of truth. A long `orcho_run_watch` is a
convenience, not a requirement: clients whose transport caps tool-call
duration should prefer a short bounded watch and reconnect via the
`next_seq` / `trigger.seq` cursor, and treat a dropped or timed-out
watch as observer loss rather than a run failure. See
[MCP Observation Delivery](architecture/observation_delivery.md), in
particular [Resilient observation loop](architecture/observation_delivery.md#resilient-observation-loop).

### `orcho_run_status(run_id, include=None)`

Summary snapshot. Use as the primary "what is happening / what should I do
next?" check:

- `meta.status` walks through the pipeline-defined values
  (`running` → `done` / `failed` / `interrupted` / `halted` /
  `awaiting_phase_handoff`; cross runs also use
  `awaiting_gate_decision` / `awaiting_human_review`).
- `metrics` is `null` until the pipeline writes its first metrics
  flush; populated incrementally per phase.
- `sub_runs` is empty for single-project runs; populated for cross-
  project orchestration with one entry per sub-project alias.
- `auto_detect` is a typed projection of the profile-selector decision,
  present only when the run started via `profile="auto-detect"` (else
  `null`). It carries `requested_selector` (the `auto-detect` token —
  set whenever the decision block exists), the selected / recommended
  profile + mode, `detection_state` / `disposition` / `trusted`, and a
  deterministic `next_action`: `null` for a trusted recommendation,
  otherwise an `operator_input_required` pointer to re-run with an
  explicit profile (never an empty `args.profile`).
- `recovery_recommendation` is a lineage-aware continuation recommendation
  projected from the same `services.run_lineage` resolver as
  `orcho_run_diagnose` (so the two surfaces agree), present only when the run
  has a non-trivial recommendation (`null` for an ordinary run with no
  terminality and no active child). It carries the typed
  `continuation_subject` / `recommended_next_action`, the `recommended_run_id`
  to act on, and the durable `lineage` facts. See
  [Diagnosing a run](#diagnosing-a-run--orcho_run_diagnose) and
  [lineage recovery guidance](ux/lineage_recovery_guidance.md).

**Summary-only by default.** This is the supervisor's
highest-frequency poll, so the wire adapter projects `meta` to a
bounded shape rather than dumping `meta.json` verbatim (on a real run
the raw payload was ~150k chars, ~95% of it phase bodies). Top-level
scalars and gate verdicts pass through; the heavy `meta.phases.*`
bodies are replaced by markers:

| Raw body | Summary marker |
|---|---|
| `task` (full text) | first 280 chars + `task_chars` |
| `phases.plan[i].output` (plan markdown) | `output_chars`; file lists → `*_count` |
| `phases.implement.output` (agent text) | `output_chars` |
| `phases.implement.implementation_receipts[i]` | `subtask_id` + `state` (+ cheap scalars) |
| `phases.validate_plan[i].critique` / `.findings` | `verdict` + `critique_chars` + `findings_count` |
| `phases.rounds[i].critique` / `.repair_output` | `*_chars` |
| `phases.final_acceptance.critique` | `verdict` / `ship_ready` / `critique_chars` |
| per-attempt `prompt_render` / `context_*` | dropped |

Full bodies stay on disk under `run_dir` and are reachable via
`orcho_run_evidence` (plan / findings / receipts slices),
`orcho_run_metrics` (per-phase tokens / duration), or a direct read of
`runspace/runs/<run_id>/`. Pass `include` to opt back in:
`["task"]`, `["plan"]`, `["output"]`, `["critiques"]`, `["receipts"]`,
or `["all"]` (the pre-summary payload verbatim). Unrecognised tokens
are ignored.

**Supervisor-truth merge for cancelled-early runs.** The
pipeline owns `meta.json` and uses an `atexit` handler to flip
`status="running"` → `"interrupted"` on abnormal exit. `atexit`
doesn't fire under SIGKILL or when cancel arrives before the handler
is registered, so a naïve read would report `running` forever. The
wire adapter merges the supervisor's authoritative post-mortem
status (`mcp_supervisor.json`) when:

| Condition | Wire result |
|---|---|
| `meta.json` absent (process killed before pipeline wrote it) | supervisor's status |
| `meta.status == "running"` but supervisor recorded a terminal status | supervisor's status |
| Supervisor recorded `failed` with negative `exit_code` (signal-induced) | remapped to `interrupted` |
| `meta.status` already terminal | meta value preserved |

The supervisor never writes `meta.json` itself — that file stays
pipeline-owned. The merge is a wire-time projection, not a state
mutation.

### `orcho_run_events_tail(run_id, since_seq, limit)`

Events stream (`events.jsonl`). Designed for catch-up after a client
reconnect: persist the last-seen `seq`, pass `since_seq=last_seen`,
get everything missed plus `next_seq`. `eof=True` means no events
remain past `next_seq` at this snapshot — the caller can throttle
polling until the run state changes.

### `orcho_run_metrics(run_id)`

Raw `metrics.json` for the "how much did it consume?" question: tokens,
duration, per-phase breakdown, attempts / retries, and cost-reference fields
when available.
Returns `RunNotFoundError` when the run dir doesn't exist *or* when
metrics haven't been written yet. Use `orcho_run_status` first if you
need a "metrics may not exist" branch.

### `orcho_run_history(limit, project_dir?)`

Most recent runs newest-first. The ordering is lexicographic
descending on `run_id`; for runs that follow the
`YYYYMMDD_HHMMSS_xxxxxx` format that equals chronological order. Run
dirs renamed to label-style IDs (`SMOKE_*`, `GOLDEN_*`) sort by
byte order — uppercase letters outrank digits, so labels appear above
timestamp IDs. This is intentionally not "fixed" here — workspaces
that mix formats opt into the byte ordering.

`project_dir` filter compares the meta's `project` field exactly. Pass
the same absolute path that orcho recorded.

### Direct artefact access via resources

For each run:

| URI | Mime |
|---|---|
| `orcho://runs/{run_id}/meta` | `application/json` |
| `orcho://runs/{run_id}/metrics` | `application/json` |
| `orcho://runs/{run_id}/events` | `application/x-ndjson` |
| `orcho://runs/{run_id}/parsed_plan.json` | `application/json` |
| `orcho://runs/{run_id}/evidence` | `application/json` |
| `orcho://runs/{run_id}/diff.patch` | `text/x-patch` |

Use these when you want a durable artefact by name (e.g. a parsed plan,
the composed evidence bundle, or the captured patch). Run lookup goes
through the same SDK path as the tools, so error shapes match.

### Recovering pending decisions — `orcho_workspace_pending_decisions`

`orcho_workspace_pending_decisions(limit=None, include_stale=False)` is the
typed way to re-find runs that are waiting on an operator after a watcher or
session loss — without chat memory. It scans the workspace runs directory and
returns bounded rows for runs currently on `status=awaiting_phase_handoff`.

- **Default view = the operator's decision inbox.** By default it answers
  "what needs my decision *now*", not "what historical artifact is paused
  somewhere". Only `actionable` rows are returned — runs whose recorded
  `meta.project` path still exists **and** lives under the resolved workspace
  scope. In the standard `workspace-orchestrator` layout this scope includes
  both the orchestrator directory and its parent project group, where sibling
  project repos live. Non-actionable paused runs are hidden but never silently
  dropped: each is tallied in `hidden_count` and its per-reason breakdown
  (`hidden_missing_project_count` — no/missing project,
  `hidden_temp_project_count` — temp/scratch project path,
  `hidden_out_of_workspace_count` — project outside the workspace scope).
- **Workspace-valid beats temp.** A run whose project exists under the
  workspace scope is `actionable` even when that scope itself lives in a
  temp/test directory (e.g. `/private/var/folders` or a `pytest-*` path).
  The temp/scratch heuristic is applied **only after** the workspace-valid
  check, so a legitimate workspace launched from a temporary directory is
  never mis-hidden as a throwaway demo run.
- **Counters are bounded and view-independent.** `hidden_count` and its
  breakdown are computed by classification over the scan window only (the
  same internal run-directory ceiling as `scanned_count`), so they are a
  window-local tally, not a global count of every non-actionable run that
  could exist. They are computed identically whether or not `include_stale`
  returns the hidden rows: flipping `include_stale` changes which rows
  appear, never the counter values for the same set of scanned runs. The
  breakdown always sums to `hidden_count`.
- **Forensic escape — `include_stale=True`.** Returns the hidden rows too —
  every paused run, not just the actionable inbox. Each returned row then
  carries its real `classification` (`actionable` / `missing_project` /
  `temp_project` / `out_of_workspace`) so you can see *why* a run would
  otherwise be hidden. Default-view rows are always `actionable`. The
  `limit`-only signature stays backward compatible — `include_stale` is a new
  optional argument defaulting to `False`.
- **Artifact projector, not advisory.** Each row is built from the run's
  durable `meta.json` (merged with supervisor truth) and the
  `phase_handoff_decisions/` directory — **not** the advisory
  `mcp/state.json` cache that `orcho_workspace_state` reads. A run appears
  here iff its on-disk state says it is paused.
- **Bounded.** The scan stops at an internal run-directory ceiling and the
  visible rows are capped by `limit` (default 50, newest run id first);
  `truncated` / `scanned_count` / `returned_count` expose the bound. Rows
  carry no raw findings, reviewer output, event payloads, or logs — only
  the compact handoff read-model (`handoff_id` / `phase` / `trigger` /
  `verdict` / `round_label` / `available_actions` /
  `decision_artifact_exists` / `suggested_next_action`) plus the row
  `classification`.
- **Decision-coherent `next_actions`** (matching `orcho_run_diagnose`):
  - `decision_artifact_exists=false` → `operator_input_required`
    `orcho_phase_handoff_decide` carrying the `available_actions` in
    `choices`;
  - `decision_artifact_exists=true` → `ready_call` `orcho_run_resume` —
    the decision is recorded, so the next step is resume, never a second
    decide.

A corrupt or unreadable single run is skipped, never fatal to the scan.

---

## Resuming a run — `orcho_run_resume`

### What it actually does

`orcho_run_resume(run_id, profile=None)` spawns a **brand-new pipeline
subprocess** against the existing `run_dir`, with `--resume <run_id>`
in argv. The pipeline-side resume loader reads the on-disk checkpoint
and continues based on the effective profile.

This is **rerun-with-checkpoint-context**, not "automatic
skip-completed-deterministic-phases". The semantics are:

1. Effective profile resolves to `meta.profile` (inherit from original
   run) when the caller does not pass `profile`. Explicit `profile`
   is a deliberate switch.
2. The pipeline loads checkpoint state for phases that *do* run under
   the effective profile.
3. Phases the profile doesn't include are simply not in this spawn's
   plan — they're not "skipped because already done", they're "not
   part of this profile".

### Profile choice

| Caller intent | Pass | What runs |
|---|---|---|
| Continue the original workflow coherently (most common) | nothing — `profile=None` defaults to inherit | Same profile the run started with; review/final see the same prompt envelope they did the first time. |
| Deliberately switch to a small scoped continuation | `profile="small_task"` or an internal continuation profile when the caller owns that contract | Per selected profile; use only when changing the workflow is intentional. |
| Full semantic rerun from checkpoint | `profile="feature"` | plan / validate_plan / implement / review_changes / repair_changes / final_acceptance. |
| Let Orcho choose the work kind again | `profile="auto-detect"` | Resolves to a concrete semantic profile, then runs that profile. |
| Other deliberate switch | any other installed semantic profile name | Per profile. |

Legacy runs whose `meta.json` predates the `profile` field fall back
to `"feature"` so review/final still see the canonical prompt
envelope.

### Pre-flight guard — typed `resume_outcome`

Before the supervisor spawns anything, `orcho_run_resume` classifies the
run (the same shared classifier `orcho_run_diagnose` uses) and returns a
**typed `resume_outcome`**. A resume that would be a no-op or wrong is
caught *before* spawn, so it is never success-shaped — a terminal resume
never returns a `pid`.

| `resume_outcome` | Wire model | Spawns? | When |
|---|---|---|---|
| `applied` | `RunResumeResult` | yes | The run is genuinely resumable (`running` restart, `failed`, `interrupted`, or a non-terminal `halted`). Carries the fresh spawn handle (`pid` / `run_dir` / `started_at` / `command`). |
| `pending_decision` | `ResumePendingDecisionResult` | no | Paused on `awaiting_phase_handoff` with no recorded decision. Resolve with `orcho_phase_handoff_decide` first, then resume. |
| `superseded_by_child` | `ResumeBlockedResult` | no | A newer unfinished follow-up child is continuing this run. `recommended_run_id` names the child to resume instead of this parent. |
| `recover_via_source_run` | `ResumeBlockedResult` | no | This run is a terminal / rejected recovery run, but durable lineage points at a *resumable source* run that still owns the retained checkpoint / worktree. `recommended_run_id` names that source. **Not success-shaped — no `pid`.** Carries a `ready_call` `orcho_run_resume` on the source, not a `from_run_plan`. |
| `rejected_terminal` | `ResumeBlockedResult` | no | The run is terminal (terminal success or a terminal halt reason) with no resumable lineage subject; resuming is inert. **Not success-shaped — no `pid`.** Points at read-only inspection, never a resume. `recommended_run_id` stays `None`. |

`ResumeBlockedResult` carries **no spawn fields** (no `pid` / `run_dir` /
`command` / `started_at`) — the supervisor was never asked to resume. Its
`next_actions` are typed by call-readiness (`kind`, see
[Diagnosing a run](#diagnosing-a-run--orcho_run_diagnose)): the
`superseded_by_child` outcome carries a `ready_call` `orcho_run_resume`
on the child; `recover_via_source_run` carries a `ready_call`
`orcho_run_resume` on the source; `rejected_terminal` carries only
read-only inspection calls.

The guard is defensive: a run that cannot be classified (unresolvable /
corrupt) falls through to the supervisor, which keeps its own resolution
and error contract (a missing run still raises `RunNotFoundError`). So the
pre-flight never adds a new failure surface.

### Controllability boundary — `mcp_controllable` vs `inspect_only`

Whether *this* MCP server can **mutate** a run (resume it, decide its phase
handoff) is a durable, on-disk fact, classified **orthogonally** to the
`condition` axis above. The only signal is `<run_dir>/mcp_supervisor.json`:

| `control` | When | What MCP may do |
|---|---|---|
| `mcp_controllable` | `mcp_supervisor.json` is present and carries a resolvable `project_dir` (the run was started by this MCP server) | Inspect **and** mutate — `orcho_run_resume` / `orcho_phase_handoff_decide` proceed as above. |
| `inspect_only` | No `mcp_supervisor.json` (a foreign / CLI-started run dir — only `meta.json`), or the state file lacks a resolvable `project_dir` | Inspect only — MCP has no durable supervisor metadata to respawn or advance the run. |

`orcho_run_diagnose` surfaces this as the typed `control`
(`mcp_controllable` | `inspect_only`) and `control_reason` fields. The axis
never collapses into `condition`: a run can be `active` / `needs_decision` /
terminal **and** `inspect_only` at the same time.

On an `inspect_only` run, the mutation tools refuse **before** touching the
supervisor or SDK by **raising** `InspectOnlyControlError`. The refusal travels
the typed-error channel (like `RunNotFoundError`) instead of a success return,
so the success `outputSchema` of `orcho_run_resume` /
`orcho_phase_handoff_decide` is unchanged — an `mcp_controllable` run still
returns exactly the same shape as before. The error carries the typed
`InspectOnlyControlResult` payload (`kind='inspect_only'`) on its `result`
attribute:

- `orcho_run_resume` → `InspectOnlyControlError` carrying
  `InspectOnlyControlResult(attempted='resume')` — no subprocess spawns (no
  `pid` / `run_dir` / `command` / `started_at`).
- `orcho_phase_handoff_decide` → `InspectOnlyControlError` carrying
  `InspectOnlyControlResult(attempted='phase_handoff_decide')` — no decision
  artifact is written and the SDK decide is never called, on both the
  synchronous and the native-elicitation entry paths.

The carried `InspectOnlyControlResult.next_actions` carry **only** read-only MCP
inspection — a `ready_call` `orcho_run_status` and a `ready_call`
`orcho_run_evidence` (`slice='errors'`). They never re-invoke
`orcho_run_resume` on the same run and never name a non-MCP tool. The
instruction to *manage the run through the CLI that started it* lives in the
free-text `message` and `suggested_next_action` — it is deliberately **not**
serialized as a `next_actions` record (those stay MCP-tool-only). The same
classification is also available read-only on `orcho_run_diagnose` via the
`control` / `control_reason` fields, so a caller can branch before ever
attempting a mutation.

**On the wire the refusal is structured, not an opaque string.** FastMCP
otherwise collapses a raised exception to `str(exc)`, dropping the typed
payload. `orcho_mcp.tool_error_delivery.register_inspect_only_error_delivery`
wraps the `CallToolRequest` handler so an `InspectOnlyControlError` is returned
to the client as a `CallToolResult` with `isError=true` **and**
`structuredContent` carrying the full `InspectOnlyControlResult`
(`kind` / `attempted` / `control` / read-only `next_actions`). Because a
returned `CallToolResult` is passed through verbatim, this never validates
against or widens the success `outputSchema`. The wire contract is pinned by
`tests/integration/protocol/test_stdio_inspect_only_refusal.py` and the L4
`tests/acceptance/mock_pipeline/test_foreign_run_control_boundary.py`.

Deferred / out of scope:

- Reconstructing supervisor state from `meta.json` so MCP can fully control a
  foreign / CLI-started run.
- Recording an MCP-side decision artifact for a later CLI-driven resume.

### `applied` wire shape

`RunResumeResult` extends `orcho_run_start`'s `RunStartedResult`
(`run_id` / `run_dir` / `pid` / `started_at` / `project_dir` / `command`)
and adds `resume_outcome="applied"`, a human-readable `message`, and a
`suggested_next_action`. On an applied resume that pointer is a `ready_call`
`orcho_run_watch` pre-filled with `run_id` — forward it verbatim to
re-enter the observation loop on the freshly-spawned subprocess. The new
subprocess's `pid` is fresh — `orcho_run_status(run_id)` reflects the
*latest* spawn's state, but the run_dir's history (events, metrics,
per-phase artefacts) accretes across spawns.

> **Call `orcho_run_diagnose` before a risky resume.** When you are not
> certain a run is plainly resumable, diagnose first: it returns the same
> typed `condition` plus ready-to-forward next steps, so you resume the
> right `run_id` (the live child, the recoverable parent) instead of a
> terminal no-op or a superseded parent.

### Not in scope for v0.1.0a

- *Smart* resume that picks the right profile automatically based on
  checkpoint state.
- Resume / decide on a foreign or CLI-started run dir. Such a run is
  classified `inspect_only` (see
  [Controllability boundary](#controllability-boundary--mcp_controllable-vs-inspect_only))
  and the mutation tools raise `InspectOnlyControlError` before any spawn or
  decision write; restart-recovery still re-binds runs whose
  `mcp_supervisor.json` is intact on server start. Reconstructing supervisor
  state from `meta.json` for full foreign-run control, and recording an
  MCP-side decision for a later CLI resume, are deferred.

---

## Phase handoff — `orcho_phase_handoff_decide`

**Principle:** *Phase-handoff decisions are not process lifecycle.
They are state transitions.*

Run lifecycle (start / resume / cancel) drives subprocesses.
Phase-handoff decisions don't — they read meta state, write a decision
artifact, and (for `halt`) flip a status. Pauses are declarative:
each `PhaseStep` carries an optional `handoff` policy
(`human_bypass`, `human_feedback_on_reject`, `human_feedback_always`)
in the active profile, and the runtime decides when to pause based on
the verdict + remaining loop budget. Built-in full-cycle semantic profiles
(`feature`, `complex_feature`, `refactor`, `migration`) declare
`human_feedback_on_reject` on `validate_plan`; plan-only profiles
(`planning`, `research`) declare `human_feedback_always`; `small_task`
keeps the lightweight bypass posture.

### Flow

```
orcho_run_start(profile="feature", …)
       ↓
[ pipeline spawns; plan loop exhausts max_rounds with rejected verdicts ]
       ↓
[ runtime emits phase.handoff_requested; orchestrator writes
  meta.status="awaiting_phase_handoff" + meta.phase_handoff payload;
  exit rc=4 ]
       ↓
orcho_run_status(run_id)   ← caller reads meta.phase_handoff
       │                     (id, type, trigger, round, available_actions,
       │                     artifacts, last_output, …)
       ↓
orcho_phase_handoff_decide(run_id, handoff_id, action, feedback?, note?)
       │
       ├── action="continue"          → when present in available_actions,
       │                                writes the decision artifact;
       │                                meta.status STAYS awaiting_phase_handoff.
       │                                Caller follows up with:
       │                                    orcho_run_resume(run_id)
       │                                which re-spawns past the handoff
       │                                without mutating the machine verdict,
       │                                inheriting meta.profile by default.
       │
       ├── action="retry_feedback"    → writes the artifact with the
       │                                ``feedback`` string; resume runs ONE
       │                                extra human-directed plan → validate_plan
       │                                round (``LoopStep.max_rounds`` is not
       │                                mutated; ``human_directed_rounds`` is
       │                                incremented). On rejection a fresh
       │                                handoff fires with handoff_id round+1.
       │
       ├── action="continue_with_waiver" → writes the artifact with the
       │                                (required, non-empty) ``feedback``
       │                                recording why the rejected verdict is
       │                                waived. meta.status STAYS
       │                                awaiting_phase_handoff; resume advances
       │                                past the handoff like ``continue``,
       │                                but the waiver rationale is persisted.
       │
       └── action="halt"              → writes the artifact, flips
                                        meta.status to "halted"
                                        synchronously and clears
                                        meta.phase_handoff. Run is terminal;
                                        resume should NOT follow.
```

### `orcho_phase_handoff_decide(run_id, handoff_id, action, feedback?, note?)` contract

| Field | Type | Meaning |
|---|---|---|
| `run_id` | `str` | Run to decide on. |
| `handoff_id` | `str` | Must equal `meta.phase_handoff.id` (e.g. `"validate_plan:plan_round:2"`). Stale UI cannot decide on a fresh handoff. |
| `action` | `"continue"` \| `"retry_feedback"` \| `"continue_with_waiver"` \| `"halt"` | Must be in the active handoff's `available_actions`. Bare `continue` is not universal; incomplete implement offers only retry, explicit waiver, or halt. |
| `feedback` | `str \| None` | Human direction injected into the next round. Required (non-empty) for `retry_feedback` and `continue_with_waiver`; rejected for the other actions. |
| `note` | `str \| None` | Free-form audit comment. Persisted in the artifact, never required. |

Returns `PhaseHandoffDecideResult`: `run_id`, `handoff_id`, `phase`,
`action`, `feedback`, `note`, `decided_at` (ISO 8601 UTC).

### Decision artifacts — `<run_dir>/phase_handoff_decisions/{safe_handoff_id}.json`

```json
{
    "run_id":     "20260510_120000_abcdef",
    "handoff_id": "validate_plan:plan_round:2",
    "phase":      "validate_plan",
    "action":     "continue",
    "feedback":   null,
    "note":       "plan addresses the architecture review concern",
    "decided_at": "2026-05-10T12:01:30+00:00"
}
```

Each handoff in a run lives at its own `safe_handoff_id` path —
deterministic, collision-resistant (slug + short SHA-256 hash). Audit
consumers read them to understand *why* each paused handoff was
resolved.

### Unattended runs never reach this tool

The pause-for-decision model above assumes someone is there to decide. A
CLI run started with `--no-interactive` sets the engine-level `unattended`
signal (a CLI-only flag — MCP-started runs never set it): advisory
handoffs auto-continue with a recorded decision, and authoritative or
safety handoffs become terminal halts with
`halt_reason="phase_handoff_unattended_halt"` plus a compact
`phase_handoff_unattended` block in `meta.json`. When an MCP client
inspects such a run, there is no pending decision to make — the halt
record explains what an operator would have been asked.

MCP-started runs are the opposite: they run with TTY prompts suppressed
but are NOT unattended — they park on `awaiting_phase_handoff` and wait
for this tool. Do not infer auto-decide behavior from the absence of a
terminal.

### Idempotency and transition discipline

- **Exact-payload idempotency.** Replaying the same
  `(handoff_id, action, feedback, note)` returns the persisted record
  unchanged — the artifact is **not** rewritten and `decided_at` is
  **not** refreshed. MCP retries / UI double-submits cannot silently
  drift the audit text.
- **Conflict.** Any field divergence for the same `handoff_id`
  (different action, different feedback, different note) raises
  `InvalidPlanError` with conflict detail. Decisions are not toggles.
- **Halt-after-halt** is idempotent against the persisted artifact
  even after `meta.phase_handoff` has been cleared (e.g. the run
  already halted): same payload → success replay, different payload →
  conflict.
- **Decide on a run not in `awaiting_phase_handoff`** (with no
  matching prior artifact) raises `InvalidPlanError`. The pause must
  already be in effect.
- **Action not in `available_actions`** raises `InvalidPlanError`.
  Action availability is runtime-decided from the verdict, not by the
  client.

A run MCP did not start (`control='inspect_only'`) is refused before any SDK
call or decision-artifact write by raising `InspectOnlyControlError`, which
carries the typed `InspectOnlyControlResult` (`attempted='phase_handoff_decide'`)
on its `result` attribute — see
[Controllability boundary](#controllability-boundary--mcp_controllable-vs-inspect_only).
Raising keeps this tool's success `outputSchema` unchanged.

### Errors

- `RunNotFoundError` — `run_id` doesn't resolve.
- `InvalidPlanError` — invalid `action` string, `retry_feedback` or
  `continue_with_waiver` without `feedback`, `handoff_id` mismatch with
  active payload,
  wrong run state, action not in `available_actions`, or a different
  decision was already recorded for the same `handoff_id`.
- `WorkspaceNotResolvedError` — `$ORCHO_WORKSPACE` /
  `$ORCHO_WORKTREE` not set.

---

## Inspection — `orcho_run_evidence`

**Goal:** *Control-loop clients should answer "what happened / what proves it?"
without reading raw logs.* The full evidence bundle
(`collect_evidence`) is exhaustive and audit-grade; `orcho_run_evidence`
exposes narrow typed slices over the same data so an LLM client can
fit the answer in its context window.

### Slices

| `slice` | Returns |
|---|---|
| `"all"` (default) | every slice in one response |
| `"plan"` | `PlanSliceRecord` — source, short_summary, planning_context, subtask_count, has_contract, goal, acceptance_criteria, owned_files, commands_to_run, risks, review_focus |
| `"findings"` | `list[FindingRecord]` — flattened reviewer findings across `plan_qa` / `review` / `final_qa` / `compliance_check`. Each carries `severity` (P0..P3), `phase`, `attempt`, optional `file` + `line`. |
| `"commands"` | `list[EvidenceCommandSliceRecord]` — pipeline shell-outs (argv summary, cwd, exit_code, duration, outcome). |
| `"artifacts"` | `list[EvidenceArtifactSliceRecord]` — files the run wrote (path, kind, size_bytes). |
| `"errors"` | `ErrorsHaltSliceRecord` — status, errors[], halt_reason, halted_at, error_summary, and `implement_delivery` (typed delivery/waiver audit; `None` for a clean delivery). See [Delivery / waiver audit](#delivery--waiver-audit-implement_delivery). |
| `"sub_runs"` | `list[SubRunLinkRecord]` — cross-run child aliases (name, status, run_dir). Empty for single-project runs. |
| `"receipts"` | `list[SubtaskReceiptRecord]` — per-subtask delivery receipts for a `subtask_dag` run. Each carries `subtask_id`, `state` (`done` / `incomplete` / `failed` / `skipped`), `runtime`, `model`, `skill`, `depends_on`, `done_criteria`, `duration`, `error`, and the done-criteria self-attestation: `criteria_report` (`list[CriterionReportRecord]` of index/criterion/met/evidence), `attestation_summary`, `attestation_error`. An `incomplete` subtask executed but did not close its done-criteria; the reason is in `attestation_error`. Empty for `whole_plan` runs. |
| `"verification_receipts"` | `list[VerificationReceiptRecord]` — durable verification-environment receipts: interpreter, cwd, import checks, commands, clean-tree note, and artifact path. |
| `"verification_timeline"` | `VerificationTimelineRecord` — canonical scheduled-gate ledger rows and identity-scoped events. Rows preserve `(command, hook, phase)`, declaration/selection facts, execution policy/consequence/executor/trigger, nullable disposition, and `receipt_evidence`. |
| `"verification_cockpit"` | `VerificationTimelineRecord` — the same canonical scheduled-gate ledger projection under the cockpit view name; its rows and events are identical to `verification_timeline`. |

### Filters

`severity_min` (only used when findings are returned):
*minimum-criticality-inclusive* cutoff — `"P0"` returns P0 only;
`"P1"` returns P0 + P1; `"P2"` returns P0 + P1 + P2; etc. `None`
returns all severities.

`phases` (only used when findings are returned): restrict to a
subset of finding-bearing phases. `None` returns all four.

### Delivery / waiver audit — `implement_delivery`

The `"errors"` slice carries an optional `implement_delivery`
(`ImplementDeliveryRecord | None`) — a first-class typed projection of
how the `implement` phase delivered. It is built from the **same**
errors-rollup the raw `errors[]` field exposes (no second meta read), so
the typed record can never drift from the raw breadcrumbs.
`orcho_run_status` carries the same scalar audit fields in its summary
projection (`meta.phases.implement` + `meta.phase_handoff_waiver`);
`include=["all"]` restores the full persisted meta when a caller needs it.
This record does not replace it. The provenance and field semantics follow
orcho-core ADR-0073.

`None` means a clean delivery (the rollup carries no `implement_delivery`
breadcrumb). When present, the record merges two rollup breadcrumbs —
`kind == "implement_delivery"` and `kind == "phase_handoff_waiver"`:

| Field | Type | Meaning |
|---|---|---|
| `delivery_status` | `str` | `"clean"` \| `"repaired"` \| `"waived"` \| `"incomplete"`. |
| `delivery_waived` | `bool` | `True` when the delivery proceeded under an operator/auto waiver. |
| `waiver_id` | `str \| None` | Identifier of the recorded waiver, when one applies. |
| `action` | `str \| None` | Decision that advanced the phase: `"continue"` \| `"continue_with_waiver"`. |
| `decided_by` | `str \| None` | Provenance of the waiver decision: `"operator"` (a human resolved `continue_with_waiver` via `orcho_phase_handoff_decide`) \| `"auto:on_exhausted"` (core auto-waived after exhausting rounds — an internal core path that does not pass through `orcho_phase_handoff_decide`). Sourced from the `phase_handoff_waiver` breadcrumb. |
| `incomplete_subtasks` | `list[str]` | Subtasks that did not reach a `done` receipt. |
| `missing_subtask_receipts` | `list[str]` | Subtasks that emitted no terminal receipt at all. |

### What this replaces

| Old client pattern | Replaced by |
|---|---|
| `orcho_run_events_tail` + parse `plan.parsed` event payload | `orcho_run_evidence(slice="plan")` |
| Read meta.json, walk phases, extract review/qa findings | `orcho_run_evidence(slice="findings", severity_min=…)` |
| Tail runner.log to see what commands ran | `orcho_run_evidence(slice="commands")` |
| Walk `<run_dir>` for non-meta/metrics files | `orcho_run_evidence(slice="artifacts")` |
| Read meta.halt_reason + scan events for errors | `orcho_run_evidence(slice="errors")` |
| `iterdir(run_dir)` for cross-run alias names | `orcho_run_evidence(slice="sub_runs")` |

The raw resources (`orcho://runs/{id}/events`, `orcho://runs/{id}/meta`)
stay available — they're the right tool when an audit trail or
post-mortem deep-dive is needed. `orcho_run_evidence` is the
right tool for "I'm an agent and I need to know what happened".

### Errors

- `RunNotFoundError` — unknown run_id.
- `InvalidPlanError` — invalid `slice` string or invalid `severity_min`
  string. (Re-uses the phase-handoff error class; a future neutral
  rename to `InvalidStateError` preserves wire shape.)

---

## Diagnosing a run — `orcho_run_diagnose`

**Goal:** *Know whether resuming is safe, wasted, or wrong — before you
act.* `orcho_run_diagnose(run_id)` is read-only (spawns no process,
mutates no state) and returns a typed `RunDiagnosis`: a deterministic
`condition`, a one-line `reason` assembled from persisted state (never
parsed from log prose), and `next_actions` that are unambiguously typed
by call-readiness. Call it **before any risky `orcho_run_resume`** so you
forward the right call instead of guessing. The classifier is the same
one the resume pre-flight guard uses, so a diagnosis and a resume agree.

### Conditions

First-match priority order — the first matching branch wins, so a paused
run reads as `needs_decision` even when a stale terminal halt sits
underneath, and a live follow-up child supersedes an otherwise-inert
terminal parent.

| `condition` | Meaning | `next_actions` |
|---|---|---|
| `active` | Running. | `ready_call` `orcho_run_watch` + `orcho_run_status` — no resume needed. |
| `needs_decision` | Paused on `awaiting_phase_handoff`; an operator must record a decision first. | Typed decide calls (see below); `available_actions` carries the verbs. |
| `needs_delivery_decision` | Parked at a post-release delivery / correction gate. | Inspect `orcho_delivery_gate`; choose one of its ready `orcho_delivery_decide` calls. |
| `recover_via_source_run` | This run is a terminal / rejected recovery run, but durable lineage points at a *resumable source* run that still owns the retained checkpoint / worktree. | `ready_call` `orcho_run_resume(run_id=recommended_run_id)` — resume the source, **not** a `from_run_plan` against this inert run. |
| `resume_inert_terminal` | Terminal (terminal success or a terminal halt reason); resuming is inert. | `ready_call` `orcho_run_evidence(slice="errors")` + `orcho_run_status` — never a resume. The typed `recommended_next_action` distinguishes a `plan_artifact_continuation`, a clean `start_followup`, and a `stop_unknown` dead-end (see below). |
| `superseded_by_child` | A newer unfinished follow-up child continues this run. | `ready_call` `orcho_run_resume(run_id=recommended_run_id)` — resume the child, not this parent. |
| `blocked_worktree` | A follow-up blocked because the parent's undelivered diff is not replayable here. | See [blocked_worktree shape](#blocked_worktree-next-actions). |
| `provider_pressure` | A residual `halted` / `failed` / `interrupted` stop that core typed as a provider runtime/access failure (rate-limit, transient runtime fault, access loss) — **not** a rejected review / failed acceptance / operator halt. The typed `provider_pressure` field carries the core facts and conservative resume-later/inspect actions. See [provider pressure](#provider-pressure). | `ready_call` `orcho_run_evidence(slice="errors")` + `orcho_run_resume(run_id)` (+ `orcho_run_status`) from the shared helper; never a feedback verb. |
| `halted` / `failed` / `interrupted` | A resumable non-terminal stop. | `ready_call` `orcho_run_resume(run_id)` + `orcho_run_evidence(slice="errors")`. |

`recommended_run_id` names the run to resume instead of this one (the
active child for `superseded_by_child`; the resumable source for
`recover_via_source_run`; the known parent for `blocked_worktree`); it is
`None` otherwise.

### Provider pressure

`provider_pressure` is a residual `halted` / `failed` / `interrupted`
stop that orcho-core typed as a *provider* runtime/access failure — a
rate-limit, a transient provider runtime fault, or a loss of provider
access — rather than a generic code/test/review failure. It reads as a
resume-later / inspect situation, never as a rejected review, a failed
final acceptance, or an operator halt.

The classification comes ONLY from the core-typed source
(`sdk.get_errors_halt` → `ErrorsAndHalt.provider_runtime` /
`ErrorsAndHalt.recovery`, keyed off `meta.failure.failure_kind`); MCP
never parses raw provider output or logs. The same typed
`ProviderPressure` payload — `failure_kind`, `recoverable`, `phase`, the
sanitized `provider_message`, and conservative `next_actions` from one
shared helper — rides on four surfaces from one projection:
`orcho_run_status` (`RunStatus.provider_pressure`, built in
`services/run_reads.py:get_run_status`), `orcho_run_evidence`
(`ErrorsHaltSliceRecord.provider_pressure` on the `errors` slice),
`orcho_run_diagnose` (`RunDiagnosis.provider_pressure`), and
`orcho_run_events_summary` (`RunEventsSummary.provider_pressure`). A
generic failure with no core-typed provider source reports
`provider_pressure == None` on all of them.

**Additive-passthrough only.** Today's core source carries the shipped
`ProviderRuntimeFailure` fields. Durable *parked* status and the finer
`pressure_kind` / `retry_state` / `reset_at` / `wait_hint` fields are a
core blocker (Stage-0 T2): until core persists a parked provider source,
MCP reads those fields defensively and passes them through only when
present — it never fabricates a reset time, invents a `retry_state`, or
parses logs to synthesise one. When core does supply them, the shared
helper already yields the parked path (`wait_until_reset` →
`resume_after_reset` → inspect); an exhausted source yields a
conservative inspect + resume with no reset time. The full
parked-captain-state (wait/resume-after-reset as a normal lifecycle state
rather than a terminal failure) is enabled only once core ships the
parked source. See
[`docs/architecture/mcp_boundaries.md`](architecture/mcp_boundaries.md)
(*Provider-pressure projection*) for the projection contract and the
executable cross-surface guard.

### Typed continuation subject — `continuation_subject` / `recommended_next_action`

`RunDiagnosis` also carries a typed continuation vocabulary so an agent can
pick the next MCP call without reading output logs. It is projected from the
same `services.run_lineage` resolver that backs
`orcho_run_status.recovery_recommendation`, so the two surfaces never drift.

| `continuation_subject` | `recommended_next_action` | Meaning |
|---|---|---|
| `source_run_checkpoint` | `resume_source_run` | Resume the resumable source run's checkpoint (the inert recovery run is **not** the subject). |
| `active_child_run` | `resume_active_child` | Resume the live follow-up child. |
| `delivery_gate` | `delivery_decision` | Resolve the pending delivery / correction gate. |
| `plan_artifact` | `plan_artifact_continuation` | Implement the persisted plan artifact as a **new** run via `from_run_plan` (see warning below). |
| `none` | `start_followup` | Clean terminal-success; start a fresh follow-up. |
| `unknown` | `stop_unknown` | Terminal dead-end with no durable continuation subject; `recovery_lineage.missing_facts` enumerates exactly which durable facts are absent. No `from_run_plan` is offered. |

`recovery_lineage` carries the durable facts behind the choice (the source
pointer + resumability, the active child, plan-subject availability, and the
missing facts for a dead-end). `continuation_subject` /
`recommended_next_action` are `None` for conditions that carry no lineage
subject (e.g. `needs_decision`).

> **`from_run_plan` means "implement this plan artifact", not "finish the last
> diff".** It is recommended ONLY for `recommended_next_action='plan_artifact_continuation'`
> (a durable plan-only / research subject). For a retained diff or checkpoint
> the correct primitive is `orcho_run_resume` of the original run — never a
> fresh `from_run_plan`. See [lineage recovery guidance](ux/lineage_recovery_guidance.md).

`orcho_run_status` surfaces the same recommendation additively as
`recovery_recommendation` (`None` for an ordinary run with no terminality and
no active child), so a captain need not call `orcho_run_diagnose` separately.
The consistency invariant is enforced by
`tests/unit/services/test_run_reads.py::test_status_and_diagnose_recovery_agree_dogfood`.

### Typed `next_actions` — `ready_call` vs `operator_input_required`

Every `NextActionRecord` carries a typed `kind`. **Clients branch on
`kind`, never on the human-readable `intent` prose.**

| `kind` | Contract |
|---|---|
| `ready_call` | `args` already hold every required parameter of the target tool's actual signature — safe to forward verbatim (e.g. `orcho_run_resume` with `run_id`). |
| `operator_input_required` | A final decision argument is intentionally omitted; `requires_operator_input=true` and `choices` and/or `input_schema` describe the operator input still needed. Never directly forwardable. |

No public-tool argument ever uses `parent_run_id` as a parameter name —
when a parent is the resume target it rides as the `run_id` arg of
`orcho_run_resume`.

#### `needs_decision` next actions

`RunDiagnosis` carries a `decision_recorded` bool for this condition. It
branches the next actions:

- **No decision recorded yet** (`decision_recorded=false`) — surface the
  decide verbs, per available verb:
  - `continue` / `halt` (no extra input) → `ready_call`
    `orcho_phase_handoff_decide` with `args={run_id, handoff_id, action}` —
    the full required-arg set is present.
  - `retry_feedback` / `continue_with_waiver` (feedback required) →
    `operator_input_required` `orcho_phase_handoff_decide` carrying
    `choices` and an `input_schema` for the required `feedback`. The
    operator's `feedback` is **never** pre-substituted into `args`.

  No `orcho_phase_handoff_decide` record is ever a `ready_call` without a
  valid substituted `action`.
- **Decision already recorded** (`decision_recorded=true`) — the run stays
  `awaiting_phase_handoff` (a `continue` / `retry_feedback` /
  `continue_with_waiver` decision writes the artifact but does not advance
  the run), so the next step is resume, not a second decide. `next_actions`
  is a single `ready_call` `orcho_run_resume(run_id)` plus a read-only
  `orcho_run_status`; **no decide records are emitted.** `orcho_run_live_status`
  and `orcho_run_events_summary.pending_handoff` route to resume on the same
  flag, so every surface agrees.

#### `blocked_worktree` next actions

- **Parent known** → `ready_call` `orcho_run_resume(run_id=parent_run_id)`
  (the parent recovers its undelivered diff).
- **Parent unknown** → read-only diagnostic fallback: `ready_call`
  `orcho_run_status` + `orcho_run_evidence(slice="errors")`. Explicitly
  **not** a resume.

### Errors

- `RunNotFoundError` — unknown `run_id`.

---

## Delivery gate — `orcho_delivery_gate` and `orcho_delivery_decide`

`orcho_delivery_gate(run_id)` is the read side for Orcho-managed
post-release delivery / correction. It is safe to poll and mutates no state.
The projection's authority comes from
`sdk.delivery_decision_state(run_id, cwd=None)`:

| Field | Meaning |
|---|---|
| `kind` | `delivery_decision_required` (approved release, diff retained and waiting to ship), `correction_decision_required` (rejected release, choose fix/skip/halt), `delivery_completed` (an Orcho-managed delivery already landed — terminal, no decision; carries `published` / `pr_url` / `delivery_notices`), or `direct_checkout_or_running` (nothing was delivered — no retained worktree gate, a direct checkout edit, or the run is still live). |
| `available_actions` | Actions core currently allows. Each has `action`, `effect`, and `creates_commit` — only `approve` sets `creates_commit=true`. |
| `blocked_actions` | Actions core currently refuses, such as `approve` / `apply` on a rejected release or blocked verification. |
| `default_action` | Core's recommended default when one exists. |
| `diff` | Retained change summary. Secondary artifact failures set `diff.degraded=true` but do not hide a decidable gate. |
| `delivery_branch` | The published / publishable delivery branch. |
| `pr_intent` | Durable pull-request intent — `branch` / `base` / `title` / `suggested_command` — for the client to turn into an actual PR. |
| `next_actions` | One `ready_call` to `orcho_delivery_decide` per available action. |

The branch mechanics behind those last two fields: `approve` never
auto-commits onto the project's default branch. Under the default
`worktree_branch` policy the commit lands on a local delivery branch
(rebased onto the target; conflicts are non-fatal and reported), and
`pr_intent` records how to publish it. Alternative policy modes
(`protect_default`, `named`, `bypass`) change where that commit is
allowed to land; the engine emits the intent, and pushing or opening the
PR stays with the client or a git-provider plugin.

`orcho_delivery_decide(run_id, action, note?)` resolves the gate through the
SDK. It never spawns a process and MCP never applies a patch directly.

Actions:

| Action | Effect |
|---|---|
| `approve` | Commit the retained worktree diff into the target checkout. |
| `apply` | Apply the retained diff without committing it. |
| `fix` | Mark the run correction-ready after a rejected release verdict. |
| `skip` | Close the gate without changing the target checkout. |
| `halt` | Leave the retained worktree in place for manual inspection. |

The result is typed. Business refusals return `accepted=false` with a
`blocker` such as `no_pending_delivery_gate`, `release_blocked`, or
`verification_blocked`; missing runs and invalid arguments use MCP errors.

---

## Cancelling a run — `orcho_run_cancel`

`orcho_run_cancel(run_id, mode)` signals the run's process group:

| `mode` | Signal | Effect |
|---|---|---|
| `graceful` (default) | `SIGTERM` to pgid | Pipeline catches, flushes checkpoint, emits `run.interrupted`, exits cleanly. |
| `hard` | `SIGKILL` to pgid | Process group dies immediately; in-flight LLM sockets drop; checkpoint reflects only fully-committed phases. |

Returns `CancelResult` with one of:

- `signal_sent(graceful)` / `signal_sent(hard)` — signal delivered.
- `already_dead` — process exited not initiated by us; already gone.
- `already_done` — process exited cleanly before the cancel reached
  it (race-window outcome — observe, don't retry).

Cancel works against runs spawned by *this* server lifetime *and*
orphans picked up via `mcp_supervisor.json` restart recovery.

---

## Examples

### Start → poll status → read metrics → confirm in history

```python
# 1. Spawn — returns immediately, pipeline runs in background.
started = await orcho_run_start(
    task="Add foo to bar",
    project_dir="/abs/path/to/project",
    mock=True,           # zero LLM cost; sub-second wall time
    max_rounds=1,
)
run_id = started.run_id

# 2. Poll until terminal status.
import time
while True:
    snap = orcho_run_status(run_id)
    status = (snap.meta or {}).get("status")
    if status in {"done", "failed", "interrupted", "halted",
                  "awaiting_phase_handoff"}:
        break
    time.sleep(0.5)

# 3. Read metrics for the completed run.
m = orcho_run_metrics(run_id)
total_tokens = m.metrics["total_tokens"]
phases = m.metrics["phases"]

# 4. Confirm the run round-tripped into history.
hist = orcho_run_history(limit=50)
match = next((r for r in hist.runs if r.run_id == run_id), None)
assert match is not None, "spawn did not appear in history"
```

### Spawn, observe via events, cancel mid-flight

```python
started = await orcho_run_start(task="long task", project_dir="...")
run_id = started.run_id

seen = 0
while True:
    batch = orcho_run_events_tail(run_id, since_seq=seen, limit=50)
    seen = batch.next_seq
    for e in batch.events:
        print(f"[{e.kind}] phase={e.phase} payload={e.payload}")

    snap = orcho_run_status(run_id)
    if (snap.meta or {}).get("status") in {"done", "failed",
                                            "interrupted", "halted"}:
        break

    if user_wants_to_stop():
        await orcho_run_cancel(run_id, mode="graceful")

    if batch.eof:
        time.sleep(0.5)
```

### Resume after interruption

```python
# Earlier: orcho_run_start(...) spawned run X, then was cancelled or
# crashed. Resume re-spawns; the run_id stays the same; profile
# defaults to meta.profile (inherit) unless explicitly overridden.
handle = await orcho_run_resume(run_id="X")
```

---

## What this document is not

- **Not a wire schema.** The authoritative wire contract is
  [`docs/mcp_schema.json`](mcp_schema.json), regenerated whenever a
  Pydantic model changes and snapshot-tested in CI.
- **Not the SDK contract.** The producer-side run-state model lives
  in `orcho-core/sdk/`; see `docs/reference/sdk_api.md` and
  `docs/sdk_schema.json` in `orcho-core`. MCP adapts that contract to
  the wire — it does not own it.
- **Not the pipeline phase reference.** Phase shape, ordering, and
  artefact format live in `orcho-core/docs/architecture/`.
