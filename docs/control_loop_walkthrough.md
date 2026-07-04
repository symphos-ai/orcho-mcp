# Orcho MCP — Control-Loop Walkthrough

End-to-end tour of the four MCP capability groups: **act**, **observe**,
**decide**, **inspect**. Each section is one tool call; together they
let an agent drive an Orcho run from spawn to terminal-with-audit
without ever reading a raw log line.

For the formal contract (vocabulary, error catalog, idempotency rules),
see [`run_lifecycle.md`](run_lifecycle.md).

---

## The loop

```
        ┌──────────────────────────────────────────────────┐
        │                                                  │
        │     ╔══════════╗   ╔══════════╗   ╔══════════╗   │
        │     ║   ACT    ║──▶║  OBSERVE ║──▶║  INSPECT ║   │
        │     ╚════╤═════╝   ╚════╤═════╝   ╚══════════╝   │
        │          │              │                        │
        │          │     ╔════════▼═════════╗              │
        │          │     ║      DECIDE      ║──┐           │
        │          │     ║  (paused on QA)  ║  │ resume    │
        │          │     ╚══════════════════╝  │           │
        │          │                           │           │
        │          └───────────────────────────┘           │
        │                                                  │
        └──────────────────────────────────────────────────┘
```

| Capability | When you reach for it |
|---|---|
| **Act** | The client wants to *change* run state (spawn, resume, cancel). |
| **Observe** | The client wants to *read* live state without parsing logs. |
| **Decide** | The run paused at the QA gate; client must approve or reject. |
| **Inspect** | The run finished (or halted) and the client wants a typed projection of what happened. |

---

## Act — `orcho_run_start`

Spawn a detached pipeline subprocess. Returns immediately; the run
progresses in the background.

```python
started = await orcho_run_start(
    task="Add a /healthz endpoint that returns build sha",
    project_dir="/abs/path/to/project",
    profile="feature",
    mock=False,           # set True for zero-LLM-cost smoke
    max_rounds=2,
)
run_id = started.run_id           # "20260510_140000_a1b2c3"
run_dir = started.run_dir         # absolute path to <ws>/worktree/runs/<run_id>/
pid = started.pid                 # subprocess pid (== pgid)
```

**Side effects on disk** (immediately):
- `<run_dir>/` exists.
- `<run_dir>/mcp_supervisor.json` is written with the supervisor's
  view of the spawn — `run_id`, `pid`, `pgid`, `status: "running"`,
  `started_at`, `project_dir`, `command`.

Pipeline-side artefacts (`meta.json`, `metrics.json`, `events.jsonl`,
plan/build/QA artefacts) materialise as the pipeline progresses.

---

## Observe — `orcho_run_status` (the primary "is this done yet?" check)

Poll on a tight loop; sub-second is fine. None of these tools mutate
state; none make LLM calls.

```python
import asyncio

TERMINAL = {"done", "failed", "interrupted", "halted", "orphaned"}
PAUSED = {"awaiting_phase_handoff", "awaiting_gate_decision",
          "awaiting_human_review"}

while True:
    snap = orcho_run_status(run_id)
    status = (snap.meta or {}).get("status")
    if status in TERMINAL or status in PAUSED:
        break
    await asyncio.sleep(0.5)

print(f"final status: {status}")
```

The two sets mean different things: a terminal run is over (`orphaned` is
the supervisor's verdict for a dead pid whose `meta.json` still said
`running`); a paused run is waiting for a decision — a phase handoff
(`orcho_phase_handoff_decide`), a delivery/correction gate
(`orcho_delivery_decide`), or a plan-only human review.

For richer streaming progress (per-phase), use `orcho_run_events_tail`
with `since_seq` pagination:

```python
seen = 0
while True:
    batch = orcho_run_events_tail(run_id, since_seq=seen, limit=50)
    for e in batch.events:
        print(f"[{e.kind}] phase={e.phase}")
    seen = batch.next_seq
    if batch.eof:
        snap = orcho_run_status(run_id)
        if (snap.meta or {}).get("status") in {"done", "failed",
                                                "interrupted", "halted",
                                                "orphaned"}:
            break
        await asyncio.sleep(0.5)
```

`orcho_run_metrics(run_id)` returns the raw `metrics.json` (token
counts, durations, per-phase breakdown). `orcho_run_history(limit=…)`
lists recent runs newest first.

---

## Decide — `orcho_phase_handoff_decide` (only when paused)

When a phase declares a non-bypass `handoff` policy in the active
profile (e.g. `human_feedback_on_reject` on `validate_plan` in
`feature` / `complex_feature`, `human_feedback_always` in `planning`) and the
runtime trigger fires, the pipeline pauses with
`meta.status="awaiting_phase_handoff"` and exits rc=4. The supervisor
reaps the exit; the process is dead but the run dir is intact and
`meta.phase_handoff` carries the active payload.

```python
# Spawn a profile that declares a non-bypass handoff on validate_plan.
started = await orcho_run_start(
    task="Refactor /api/users — concrete plan needed for QA",
    project_dir="/abs/path/to/project",
    profile="feature",           # declares human_feedback_on_reject
)

# Wait for either terminal or the pause.
await wait_until(started.run_id, {"awaiting_phase_handoff", "done", "failed"})
```

If the handoff fires:

```python
# Read the active payload — handoff_id + available_actions are
# decided by the runtime, not the client.
snap = orcho_run_status(started.run_id)
handoff = (snap.meta or {})["phase_handoff"]
handoff_id = handoff["id"]                      # e.g. validate_plan:plan_round:2
available = set(handoff["available_actions"])    # subset of {continue, retry_feedback, continue_with_waiver, halt}

# Read what the reviewer flagged before deciding.
findings = orcho_run_evidence(
    started.run_id,
    slice="findings",
    severity_min="P1",     # P0 + P1 only
    phases=["validate_plan"],
).findings

# Decide. Four outcomes:
# (a) ``continue`` — manual override, write the artifact, then resume.
result = orcho_phase_handoff_decide(
    started.run_id,
    handoff_id=handoff_id,
    action="continue",
    note="Plan addresses the data-model concern raised in F1.",
)
# meta.status STAYS awaiting_phase_handoff. Follow up with resume:
await orcho_run_resume(started.run_id)  # inherits meta.profile

# (b) ``retry_feedback`` — one extra human-directed plan round.
result = orcho_phase_handoff_decide(
    started.run_id,
    handoff_id=handoff_id,
    action="retry_feedback",
    feedback="Add the auth-migration step before deployment.",
    note="Plan missed auth migration; one more round.",
)
await orcho_run_resume(started.run_id)  # inherits meta.profile (feature)

# (c) ``continue_with_waiver`` — accept the rejected verdict, recording
#     why. ``feedback`` is required (non-empty) and persists the waiver.
result = orcho_phase_handoff_decide(
    started.run_id,
    handoff_id=handoff_id,
    action="continue_with_waiver",
    feedback="F2 is a known false positive on generated code; accepted.",
    note="Waiving validate_plan rejection; see feedback.",
)
# meta.status STAYS awaiting_phase_handoff. Follow up with resume:
await orcho_run_resume(started.run_id)  # inherits meta.profile

# (d) ``halt`` — terminal, no resume.
result = orcho_phase_handoff_decide(
    started.run_id,
    handoff_id=handoff_id,
    action="halt",
    note="Plan misses the auth migration. Restart with revised task.",
)
# meta.status flips to "halted" synchronously and meta.phase_handoff
# is cleared.
```

Phase-handoff decisions are exact-payload idempotent. Replaying the
same `(handoff_id, action, feedback, note)` returns the persisted
record unchanged; a different payload for the same `handoff_id`
raises `InvalidPlanError` with conflict detail.

---

## Act — `orcho_run_resume` (after approve, or after interruption)

Resume re-spawns a fresh pipeline subprocess against the existing
`run_dir`, with `--resume <run_id>` in argv. The pipeline-side resume
loader reads the on-disk checkpoint and continues based on the
profile chosen.

```python
handle = await orcho_run_resume(run_id)
# handle.run_id == run_id (same run, same dir)
# handle.pid    is fresh (new subprocess)
# inherits meta.profile from the original run by default
```

This is **rerun-with-checkpoint**, not "smart skip-completed-phases".
The resumed subprocess defaults to the original run's profile so
review and final-acceptance prompt envelopes stay coherent across
the pause. Explicit ``profile="<name>"`` deliberately switches —
for example to a scoped/internal continuation when the caller owns that
contract; pass a different profile only when you intentionally want a different
scope on resume.

---

## Act — `orcho_run_cancel` (race-aware)

Two modes. Both signal the run's process group; the wire shape returns
one of `signal_sent(graceful)` / `signal_sent(hard)` / `already_done`
/ `already_dead`.

```python
# Polite: SIGTERM, pipeline catches and flushes checkpoint.
result = await orcho_run_cancel(run_id, mode="graceful")

# Hard: SIGKILL, drops in-flight LLM HTTP sockets.
result = await orcho_run_cancel(run_id, mode="hard")
```

Cancel returns immediately. The supervisor's reap task observes the
subprocess exit asynchronously and updates `mcp_supervisor.json`.
`orcho_run_status` reads through both `meta.json` (pipeline-owned)
and `mcp_supervisor.json` (supervisor-owned), so cancelled-early runs
that never wrote their own meta status surface a terminal status to
the wire (`interrupted` for signal-induced exits).

---

## Inspect — `orcho_run_evidence`

After a run reaches terminal state, surface a typed projection of
what happened. No raw log scraping required.

```python
# One-shot: every slice in one call.
ev = orcho_run_evidence(run_id, slice="all")

print(ev.plan.short_summary)            # plan summary
print(len(ev.findings), "findings")     # all severities, all phases
print(ev.errors.halt_reason)            # "plan_rejected" / None
for sub in ev.sub_runs:                 # cross-project child aliases
    print(f"  {sub.name}: {sub.status}")
```

Or one slice at a time:

```python
# Just the P0 / P1 findings from the review phase.
result = orcho_run_evidence(
    run_id,
    slice="findings",
    severity_min="P1",
    phases=["review"],
)
for f in result.findings:
    print(f"  [{f.severity}] {f.title}")
    if f.file:
        print(f"    {f.file}:{f.line}")
```

| Slice | What it surfaces |
|---|---|
| `plan` | `PlanSliceRecord` — short_summary, planning_context, subtask_count, has_contract, goal, acceptance_criteria, owned_files, `allowed_modifications` globs, commands_to_run, risks, review_focus |
| `findings` | `list[FindingRecord]` — flattened reviewer findings (severity P0..P3, phase, attempt, optional file+line). Each carries an `advisory` flag: findings from the latest non-approved `validate_plan` attempt forwarded into a successful whole-plan implement are advisory — visible, not an active release blocker. |
| `commands` | `list[EvidenceCommandSliceRecord]` — pipeline shell-outs (argv summary, cwd, exit_code, duration_s, outcome) |
| `artifacts` | `list[EvidenceArtifactSliceRecord]` — files written (path, kind, size_bytes) |
| `errors` | `ErrorsHaltSliceRecord` — status, errors, halt_reason, halted_at, error_summary |
| `sub_runs` | `list[SubRunLinkRecord]` — cross-run child aliases |
| `receipts` | `list[SubtaskReceiptRecord]` — per-subtask delivery receipts (`subtask_dag`): state (`done`/`incomplete`/`failed`/`skipped`), done-criteria self-attestation (`criteria_report`, `attestation_summary`, `attestation_error`). An `incomplete` subtask did not close its done-criteria. |
| `verification_receipts` | durable verification-environment receipts — interpreter, cwd, import checks, commands, clean-tree note, artifact_path |
| `verification_timeline` | per-gate status (`PASS`/`FAIL`/`MISSING`/`STALE`/`SKIPPED`/`FRESH`), rerun hints, aggregates, auto-run events |
| `verification_cockpit` | the typed cockpit view of the same gates — header + rows with hook/phase, trigger, policy, required, gate_class, status, env, receipt evidence |
| `handoff_advice` | phase-handoff advisor records — handoff_id, phase, recommended vs applied action, confidence, resolved, outcome, token usage + cost, summary |
| `scope_expansion` | scope-expansion audit — classification, category, evidence, has_blocker flag |
| `delivery` | post-release commit-delivery outcome — release_verdict, decision_status, action, applied/committed/skipped/failed, commit_sha, halt_reason |
| `correction` | correction fixed-point outcome — non_converging, repeated blockers, parent/child run_ids, suggested_actions |
| `all` (default) | every slice in one response |

---

## End-to-end: spawn → pause → decide → resume → inspect

The canonical paused-run workflow, all four capabilities in one
sequence:

```python
# 1. ACT — spawn under a profile that declares non-bypass handoff
started = await orcho_run_start(
    task="Migrate /api/users from raw SQL to typed query layer",
    project_dir="/abs/path/to/project",
    profile="feature",           # human_feedback_on_reject on validate_plan
)

# 2. OBSERVE — poll until terminal or pause
status = await wait_until_terminal(started.run_id)

if status == "awaiting_phase_handoff":
    # 3a. INSPECT — read the active handoff + what blocked it
    snap = orcho_run_status(started.run_id)
    handoff = (snap.meta or {})["phase_handoff"]
    handoff_id = handoff["id"]
    findings = orcho_run_evidence(
        started.run_id, slice="findings", severity_min="P1",
    ).findings
    # ...client/agent/human reasoning...

    # 3b. DECIDE — continue / retry_feedback / continue_with_waiver / halt
    if reasoning_says_proceed(findings):
        orcho_phase_handoff_decide(
            started.run_id, handoff_id=handoff_id,
            action="continue", note="…",
        )
        # 3c. ACT — resume with checkpoint context (inherits meta.profile)
        await orcho_run_resume(started.run_id)
        status = await wait_until_terminal(started.run_id)
    else:
        orcho_phase_handoff_decide(
            started.run_id, handoff_id=handoff_id,
            action="halt", note="…",
        )
        status = "halted"

# 4. INSPECT — final audit, no log reading
ev = orcho_run_evidence(started.run_id, slice="all")
print(f"final: {ev.errors.status}, halt_reason={ev.errors.halt_reason}")
print(f"findings P0+P1: "
      f"{[f for f in ev.findings if f.severity in ('P0','P1')]}")
print(f"commands run: {len(ev.commands)}")
print(f"artifacts: {[a.path for a in ev.artifacts]}")
```

---

## The second decision loop: the delivery gate

A run that reaches its release state is not finished — the retained diff
still needs a delivery decision. This is a separate pause from the phase
handoff, with its own tools:

```python
gate = orcho_delivery_gate(run_id)   # read-only projection

if gate.kind == "delivery_decision_required":
    # approved release: diff retained, waiting to ship
    diff = orcho_run_diff(run_id)
    orcho_delivery_decide(run_id, action="apply",   # or approve/skip/halt
                          note="draft into the checkout for review")
elif gate.kind == "correction_decision_required":
    # rejected release: choose fix (correction-ready) / skip / halt
    orcho_delivery_decide(run_id, action="fix")
# gate.kind == "direct_checkout_or_running" → nothing to decide here
```

What to know before wiring this loop:

- `available_actions` / `blocked_actions` on the gate are authoritative —
  a hard guard (`target_dirty`, `verification_blocked`,
  `delivery_scope_violation`, `patch_invalid`) blocks only the shipping
  actions (`approve` / `apply`); `fix`, `skip`, `halt` stay available.
- Only `approve` creates a commit. It never auto-commits onto the default
  branch: the branch policy (default `worktree_branch`) publishes a
  delivery branch, and the gate projection carries `delivery_branch` plus
  a durable `pr_intent` (branch / base / title / suggested_command) for
  the client to turn into a pull request.
- A cross-project run can also pause on `status="awaiting_gate_decision"`
  (a cross gate awaiting operator override); resolve it through the same
  decide-then-resume discipline, never a blind resume.

---

## Beyond the core loop

Surfaces a production client should know exist, in one line each:

- **`orcho_run_watch`** — the preferred long-poll when your MCP request
  carries a `progressToken`; a watch timeout means *observer* loss, never
  run failure.
- **`orcho_handoff_advice`** — LLM advisor for a paused handoff; writes a
  durable advice record (see the `handoff_advice` evidence slice), never
  a decision.
- **`orcho_run_resume(runtime_override={phase, runtime, model})`** —
  operator-decided provider replacement after a terminal access failure.
- **`orcho_run_start(profile="auto-detect")`** — pipeline classifies the
  task and picks the profile; `orcho_run_status` exposes the typed
  `auto_detect` projection (requested_selector, selected_profile,
  detection_state, next_action).
- **`orcho_run_start(from_run_plan=<parent_run_id>)`** — new run inherits
  the parent's parsed plan and skips re-planning (distinct from resume).
- **Attachments** — `attach`, `attach_text`, `attach_image`,
  `attach_binary` inject context into the run.
- **`session_mode`** (`auto` / `stateless` / `chain` / `hybrid`) —
  controls implement → repair provider-session continuation.
- **Unattended runs** — a CLI `--no-interactive` run auto-continues
  advisory handoffs and turns authoritative ones into typed halts
  (`halt_reason="phase_handoff_unattended_halt"`); MCP-started runs are
  NOT unattended — they park on handoffs and wait for this loop.

---

## What the loop deliberately doesn't do

- **Push-style progress notifications are opt-in.** When the MCP
  request to `orcho_run_watch` carries a `progressToken`, the server
  emits ordered `notifications/progress` on event-sequence advance.
  Clients that don't carry a token poll `orcho_run_status` (cheap —
  JSON read, no LLM) or `orcho_run_events_tail` (richer — event stream
  with seq pagination). Both paths work against the same run state;
  the loop's functional core is polling-friendly, and push is the
  optional UX layer on top.
- **Multi-run batch operations**: each tool acts on a single
  `run_id`. Batch helpers (e.g. cancel-all) are an embedder concern,
  not part of the wire surface.
- **Cross-MCP-server orchestration**: the loop here is *Orcho* as MCP
  *server*. Orcho-as-MCP-*client* (pipeline agents calling external
  GitHub / Linear / Slack MCP servers) is a separate roadmap, tracked
  in `orcho-core/docs/plans/2026-05-06-cross-mcp-orchestration.md`.

---

## Architectural invariants

The loop preserves four ownership boundaries that no tool crosses:

| Layer | Owns |
|---|---|
| pipeline | `meta.json` — terminal write authority |
| supervisor | `mcp_supervisor.json` — pid, pgid, exit_code, supervisor view |
| SDK | sanctioned read projections + the one sanctioned `meta.json` write (`validate_plan_decide(rejected)` flipping `status` to `halted`) |
| MCP | wire adaptation — Pydantic schemas, error mapping, no business logic |

Every other write to a run dir flows through the SDK. Every read goes
through the SDK or its evidence collector. MCP tools never reach into
`pipeline/` or `core.observability/` directly — see
`tests/unit/architecture/test_no_direct_run_state.py` for the
structural gate that enforces this.

---

## Pointers

- `docs/run_lifecycle.md` — formal contract, vocabulary, error catalog
- `docs/mcp_schema.json` — the JSON-Schema snapshot of every tool
- `docs/demos/demo-1b-single-project-mcp.md` — runnable single-project MCP proof
- `../orcho-core/docs/reference/sdk_api.md` — SDK API reference
- `../orcho-core/docs/sdk_schema.json` — SDK schema snapshot
- `../orcho-core/docs/adr/0021-public-sdk-boundary.md` — why the SDK is the protocol API
