# DEMO-1B — Single-project MCP walkthrough

An MCP-aware client drives the same single-project mock loop that
DEMO-1A drove from the CLI — but through the public MCP tool surface.
No raw log scraping, no new tools, no schema changes.

This demo proves the full control loop:

  start → observe handoff → inspect findings → decide → resume → done →
  inspect evidence / metrics / history

## What this demo proves

1. `orcho_run_start` spawns a single-project mock pipeline under the
   `feature` profile (which declares `human_feedback_on_reject` on
   `validate_plan`); a forced plan rejection pauses the run.
2. `orcho_run_status` shows `awaiting_phase_handoff` and surfaces the
   active `meta.phase_handoff` payload (`id`, `available_actions`).
3. `orcho_run_evidence(slice="findings", phases=["validate_plan"])`
   returns typed reviewer findings (severity, title, body, required_fix)
   — the same surface `sdk.evidence_slices.list_findings()` exposes.
4. `orcho_phase_handoff_decide(..., action="continue")` records the
   decision artifact.
5. `orcho_run_resume()` inherits the original profile and continues past the
   handoff.
6. The run reaches terminal `done`.
7. `orcho_run_evidence(slice="all")`, `orcho_run_metrics`, and
   `orcho_run_history` close the loop with no log scraping.

## Setup — disposable project + workspace

The mock pipeline writes inside the project tree it's pointed at
(e.g. `.agent/mock_changes/last_build.md`). Run against a *copy* of
the fixture, not the source. Then let `orcho workspace init` create
the Orcho workspace state:

```bash
cd /path/to/orcho-core
rm -rf /tmp/orcho_demo_1b
mkdir -p /tmp/orcho_demo_1b
cp -R examples/golden-api /tmp/orcho_demo_1b/project

orcho workspace init /tmp/orcho_demo_1b \
  --mcp-config /tmp/orcho_demo_1b/.mcp.json \
  --mcp-server-name orcho-demo-1b \
  --orcho-mcp-command "$(command -v orcho-mcp)"
```

If `command -v orcho-mcp` prints nothing, use the absolute path to the
server command from your Orcho environment, for example
`/Users/me/orcho-preview/orcho-core/.venv/bin/orcho-mcp`.

This creates:

```
/tmp/orcho_demo_1b/project    # disposable copy of examples/golden-api
/tmp/orcho_demo_1b/workspace-orchestrator  # disposable Orcho workspace
/tmp/orcho_demo_1b/.mcp.json  # optional project-local MCP config
```

## Configure the MCP environment

`orcho_run_start` does not take a `workspace` argument. The MCP server
resolves the runs directory from `ORCHO_WORKSPACE`. The process that
launches the MCP server must inherit it:

```bash
export ORCHO_WORKSPACE=/tmp/orcho_demo_1b/workspace-orchestrator
```

Whatever MCP-aware client is driving the loop (any MCP host) must have
this env var visible to the MCP-server process it spawns. Configure it
in the client's MCP-server config block when `orcho-mcp` is a managed
process. The `.mcp.json` created above already contains the matching
`orcho-demo-1b` entry; restart the client/server after adding it.

## MCP call sequence

The full proof loop in pseudo-Python. Each step uses one published
tool; the assertions show what an MCP-aware client would inspect at
each stage.

```python
# 1. ACT — spawn a mock run that will pause on the validate_plan handoff.
# The feature profile declares ``human_feedback_on_reject`` on
# ``validate_plan`` (max_rounds=2 in the profile's plan loop), so a
# mock provider rejecting every round drives the run to the final
# rejected round and the pause fires.
started = await orcho_run_start(
    task="Fix validation bug in sample API",
    project_dir="/tmp/orcho_demo_1b/project",
    profile="feature",
    mock=True,
    mock_validate_plan_reject=3,  # ≥ profile's plan-loop max_rounds
    max_rounds=1,
)
# → started.run_id, started.run_dir, started.pid

# 2. OBSERVE — poll until the handoff pauses.
while True:
    snap = orcho_run_status(started.run_id)
    if (snap.meta or {}).get("status") == "awaiting_phase_handoff":
        break
    await asyncio.sleep(0.2)
# Read the active handoff payload — it carries the canonical
# ``handoff_id`` plus ``available_actions``.
handoff = (snap.meta or {})["phase_handoff"]
handoff_id = handoff["id"]                  # e.g. validate_plan:plan_round:2
available = set(handoff["available_actions"])  # {"continue", "retry_feedback", "continue_with_waiver", "halt"}

# 3. INSPECT — typed findings, no log scraping.
bundle = orcho_run_evidence(
    started.run_id, slice="findings", phases=["validate_plan"],
)
for f in bundle.findings:
    print(f"[{f.severity}] {f.title} (phase={f.phase}, attempt={f.attempt})")
    print(f"  {f.body}")
    print(f"  Required fix: {f.required_fix}")
```

Curated finding excerpt — what the mock plan_qa gate produces:

```
[P2] Missing test coverage for edge case A (phase=plan_qa, attempt=1)
  The plan does not specify how edge case A will be covered.
  Required fix: Add a concrete test case for edge case A with acceptance criteria.

[P3] Module boundary unclear in section 3 (phase=plan_qa, attempt=1)
  Section 3 does not state which module owns the new behavior.
  Required fix: Name the owning module and list the files it touches.

[P2] Verification step lacks rollback plan (phase=plan_qa, attempt=1)
  Verification mentions running tests but does not describe rollback if they fail.
  Required fix: Document the rollback path and how to revert the change safely.
```

```python
# 4. DECIDE — record a manual continue override.
assert "continue" in available
decision = orcho_phase_handoff_decide(
    started.run_id,
    handoff_id=handoff_id,
    action="continue",
    note="DEMO-1B accepts the rejected plan and continues.",
)
# → decision.action == "continue", decision.decided_at is ISO 8601

# 5. ACT — resume from checkpoint; inherit the original profile.
resumed = await orcho_run_resume(started.run_id)
# resumed.run_id == started.run_id
# Supervisor preserves the original `--mock` flag through resume, so
# the resumed subprocess uses the same mock provider for build /
# review / fix / final_qa.

# 6. OBSERVE — poll for terminal.
while True:
    snap = orcho_run_status(started.run_id)
    if (snap.meta or {}).get("status") in {"done", "failed", "halted", "interrupted"}:
        break
    await asyncio.sleep(0.2)
assert (snap.meta or {})["status"] == "done"

# 7. INSPECT — final evidence / metrics / history.
full = orcho_run_evidence(started.run_id, slice="all")
# full.plan, full.findings, full.commands, full.artifacts,
# full.errors, full.sub_runs all populated.

metrics = orcho_run_metrics(started.run_id).metrics
# dict of total_tokens / total_duration_s / per-phase rollups.

history = orcho_run_history(limit=10)
assert any(r.run_id == started.run_id for r in history.runs)
```

## What this demo does not cover

- Web dashboard — see DEMO-1C.
- Cross-project orchestration — see DEMO-1D / DEMO-1E.
- Push progress notifications over `progressToken` — available via
  `orcho_run_watch`; this demo focuses on the polling path.
- Real-provider billing.
- New MCP tools or schema changes.
- Filtering UX (`severity_min` filtering is exercised by
  `tests/acceptance/mock_pipeline/test_orcho_run_evidence.py` and is
  not in scope of this demo's narrative).
- Per-subtask progress and delivery receipts for `subtask_dag` runs
  (`orcho_run_watch(until="subtask")` + `orcho_run_evidence(slice="receipts")`)
  — exercised in `tests/acceptance/mock_pipeline/` but not in this narrative.

## Resume contract scope

DEMO-1B includes the minimal supervisor fix needed for the proof loop:
when an original `orcho_run_start` was a mock run, `orcho_run_resume`
re-threads `--mock` into the resumed subprocess so the same mock
provider stays in effect for build / review / fix / final_qa.

DEMO-1B does **not** claim that every start-time option is preserved
through resume. A full audit of the resume launch contract — covering
`max_rounds`, attachments, and per-phase model/provider overrides — is
tracked as a separate hardening task.
The proof loop above does not depend on those options drifting.

## Pointers

- Full reference walkthrough:
  [docs/control_loop_walkthrough.md](../control_loop_walkthrough.md)
- Wire contract for every tool used here:
  [docs/run_lifecycle.md](../run_lifecycle.md)
- CLI counterpart (same flow without MCP):
  [orcho-core/docs/demos/demo-1a-single-project-cli.md](../../../orcho-core/docs/demos/demo-1a-single-project-cli.md)
- Pinned automated proof:
  [tests/acceptance/mock_pipeline/test_demo_1b_single_project_mcp.py](../../tests/acceptance/mock_pipeline/test_demo_1b_single_project_mcp.py)
