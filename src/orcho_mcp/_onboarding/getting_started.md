# Getting started with Orcho through MCP

Orcho runs a multi-phase pipeline (plan → build → review → fix → final QA)
against your project. This guide walks you through your first run end to
end. Most steps use one MCP tool — no shell, no log files. One small
shell step lays the workspace rails before MCP can see them.

## 0. Initialise a workspace (one-time)

Orcho needs a workspace directory to store runs and (optionally) an MCP
config snippet. Point it at a folder that holds one or more project
repos side-by-side (for example `~/www/my-org/`):

```bash
orcho workspace init ~/www/my-org
```

The command creates `~/www/my-org/workspace-orchestrator/` (with
`worktree/runs/`, an `orcho-env.sh` to set `ORCHO_WORKSPACE`, and an
empty prompts directory), and prints an MCP config snippet you can
paste into your MCP client's config. To merge it into an existing
`.mcp.json` instead, pass `--mcp-config`:

```bash
ORCHO_MCP_COMMAND="$(command -v orcho-mcp)"

orcho workspace init ~/www/my-org \
  --mcp-config ~/www/my-org/.mcp.json \
  --orcho-mcp-command "$ORCHO_MCP_COMMAND"
```

Existing servers in `.mcp.json` are preserved. Add `--dry-run` to
preview without writing anything.

`ORCHO_MCP_COMMAND` must resolve to the MCP server command the client can
launch. In packaged installs this is normally `orcho-mcp`. If you are
working from a source checkout, use the absolute path to
`.venv/bin/orcho-mcp` inside the Orcho environment.

Whichever path you took, the MCP server process needs
`ORCHO_WORKSPACE` set to the new `workspace-orchestrator/` directory.
The simplest way is the printed `source` line:

```bash
source ~/www/my-org/workspace-orchestrator/orcho-env.sh
```

Then add the server to your MCP client. Codex CLI/app, Claude Code,
Gemini CLI, the Claude app, and Antigravity all use different config locations; see
`docs/mcp_client_setup.md` in the orcho-mcp package for the exact
copy-paste commands. After changing client config, restart the client
so it reloads the MCP catalogue, then proceed with the tools below.

For multiple workspaces, register one Orcho MCP server per workspace with a
distinct name, such as `orcho-demo-mcp` or `orcho-atas-mcp`. First call
`orcho_workspace_info` and verify the reported `workspace_dir` before starting
a run.

## 1. Check the workspace

```text
orcho_workspace_info
```

Returns the workspace directory Orcho is using, the runs directory where
each pipeline run lands, and a list of recent project paths. If
`recent_projects` is empty, you are about to start your first project.

## 2. Choose a profile

```text
orcho_profiles_list
```

A profile is the shape of the pipeline — which phases run and in what
order. Profiles are named by **semantic work kind**, and each carries a
`default_mode` (the operating mode it runs under unless you override it).
Pick by what your task is:

- **`small_task`** — _default_mode `fast`_ — smallest end-to-end change;
  leanest full cycle.
- **`feature`** — _default_mode `fast`_ — a normal feature, full plan →
  build → review → fix → final QA cycle. The default for most work.
- **`complex_feature`** — _default_mode `pro`_ — larger feature needing
  the heavier review envelope.
- **`planning`** — _default_mode `pro`_ — produce and validate a plan
  only (focused; pairs with `--from-run-plan`).
- **`research`** — _default_mode `fast`_ — focused investigation, no
  implementation.
- **`code_review`** — _default_mode `pro`_ — focused review of an
  existing change.
- **`delivery_audit`** — _default_mode `pro`_ — focused acceptance/audit
  pass over delivered work.
- **`refactor`** — _default_mode `pro`_ — full-cycle behaviour-preserving
  restructuring.
- **`migration`** — _default_mode `pro`_ — full-cycle migration work.

Call `orcho_profiles_list` to see the live catalogue. Entries flagged
`internal=true` (e.g. `task`, `correction`) are engine-internal and not a
normal public choice — don't select them directly.

Profile is independent of execution mode. `mock=True` (next step) is a
deterministic dry run for learning the protocol — no real agent calls.
`mock=False` uses the configured real agent providers and may incur
provider costs.

## 3. Start safely on your first run

While learning, prefer a **disposable copy** of your project. Real runs
intentionally edit project files (the build phase writes code, the fix
phase patches it). If you point Orcho at a tree you cannot afford to
have edited, copy it somewhere first.

For your very first call, you can also pass `mock=True` to walk the
protocol without spending money or modifying files.

## 4. Start the run

```text
orcho_run_start(
    task="<one-line description of what you want done>",
    project_dir="<absolute path to your project>",
    profile="feature",           # declares human_feedback_on_reject on validate_plan
    mock=False,                  # set True for the protocol dry run
    max_rounds=2,
)
```

You get back a `run_id`. The pipeline runs in the background.

Pause semantics come from the active profile — `feature` declares
`human_feedback_on_reject` on `validate_plan`, so a fully-rejected plan
loop pauses for human direction; `planning` pauses for review on every
plan; a lean profile like `small_task` keeps interruptions minimal.

## 5. Watch progress

Poll periodically:

```text
orcho_run_status(run_id="<run_id>")
```

`meta.status` is the lifecycle field. Common values you will see:

- `running` — pipeline is working.
- `awaiting_phase_handoff` — pipeline paused on a phase's declared
  handoff policy, waiting for you to decide.
- `done` / `failed` / `halted` / `interrupted` — terminal states.

Real runs can take minutes. Mock runs finish in seconds.

## 6. If the run pauses on a phase handoff

When `meta.status == "awaiting_phase_handoff"`, the pipeline reached a
phase whose declared handoff policy fired and is asking you to decide.

### Inspect what the reviewer flagged

```text
orcho_run_evidence(run_id="<run_id>", slice="findings", phases=["validate_plan"])
```

Returns a list of typed findings — each with `severity`, `title`,
`body`, `required_fix`, `phase`, `attempt`. No log scraping.

### Read the active handoff payload

`orcho_run_status` exposes `meta.phase_handoff`, including `id`
(the `handoff_id` to pass back) and `available_actions` (the canonical
subset of `continue` / `retry_feedback` / `continue_with_waiver` /
`halt` the runtime allows for this pause).

### Decide

```text
orcho_phase_handoff_decide(
    run_id="<run_id>",
    handoff_id="<meta.phase_handoff.id>",
    action="continue",          # or "retry_feedback" / "continue_with_waiver" / "halt"
    feedback="<human direction>",  # required for retry_feedback and continue_with_waiver
    note="<why, for audit>",
)
```

- `continue` — manual override; records the decision. Status stays
  `awaiting_phase_handoff` until you resume.
- `retry_feedback` — one extra human-directed `plan → validate_plan`
  round; `feedback` is injected as the critique.
- `continue_with_waiver` — accept the rejected verdict as-is and move
  on; `feedback` (required, non-empty) records why the findings are
  waived rather than fixed.
- `halt` — terminal; `meta.status` flips to `halted` synchronously.

### Resume after continue / retry_feedback / continue_with_waiver

```text
orcho_run_resume(run_id="<run_id>")
```

By default the resumed subprocess inherits `meta.profile` from the
original run — review and final-acceptance prompt envelopes depend
on the profile and silently changing it drops context. Pass an explicit
semantic profile only as a deliberate switch — e.g. `profile="small_task"`
for a lean scoped continuation, `profile="planning"` to refine the plan
only, or `profile="feature"` to re-run the full cycle from checkpoint.

Then poll `orcho_run_status` again until terminal.

## 7. Inspect the final result

Once `meta.status == "done"`:

```text
orcho_run_evidence(run_id="<run_id>", slice="all")
```

Populates every projection at once: plan, findings, commands, artifacts,
errors. Use narrower slices (`"plan"`, `"findings"`, `"commands"`,
`"artifacts"`, `"errors"`) when you want only one.

```text
orcho_run_metrics(run_id="<run_id>")
```

Token counts, durations, per-phase breakdown.

```text
orcho_run_history(limit=10)
```

Recent runs across the workspace.

## 8. Verify the changes in your project

The MCP surface tells you what Orcho thinks happened. To confirm what
**actually** changed in your tree:

- Run your project's test command in the project checkout.
- Inspect your VCS diff (e.g. `git status` and `git diff`) in the
  project checkout.

If the run modified files you did not expect, you can revert them
through your VCS. This is why a disposable copy is recommended for the
first few runs.

## Quick reference

| Goal | Tool |
|---|---|
| Where does Orcho live? | `orcho_workspace_info` |
| Which profiles exist? | `orcho_profiles_list` |
| Start a run | `orcho_run_start` |
| Check progress | `orcho_run_status` |
| See reviewer findings | `orcho_run_evidence(slice="findings")` |
| Decide a paused handoff | `orcho_phase_handoff_decide` |
| Continue after approve | `orcho_run_resume` |
| Final inspection | `orcho_run_evidence(slice="all")` |
| Token / duration rollup | `orcho_run_metrics` |
| Recent runs | `orcho_run_history` |
| Stop a run | `orcho_run_cancel` |
