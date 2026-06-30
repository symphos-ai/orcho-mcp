# Orcho MCP — UX Vision

> **Status**: living document. Pinned principles + staged roadmap. Each
> Track A item is a sized implementation sketch; Tracks B / C are
> intent-only.
>
> **Last updated**: 2026-05-22.

## 0. Why this document exists

Orcho's positioning thesis is *"agents speak JSON to Orcho; Orcho
speaks structured delivery to humans."* MCP is the literal place
where that translation happens. It is **not a thin re-export of
orcho-core surfaces** — it is a sub-product whose job is to make
orcho-core useful through an LLM client (Claude Code, Codex CLI,
Cursor, Zed). Without intentional UX design the MCP surface
degenerates into a catalog of low-level tools that requires the LLM
to "just know" the workflows — usable for Sonnet-class models,
brittle for everyone else.

This document fixes the principles and roadmap so every future MCP
change can ask: *does this advance the developer's experience of
orcho-core, or just expose another lever?*

## 1. Three audiences

The MCP surface serves three readers simultaneously, and any
proposed change must consider all three:

| Audience | What they consume | What they need |
|---|---|---|
| **LLM** (Claude / Codex / cursor) | tools/list, tool descriptions, return payloads | Discoverable tools, hints in results, low-friction state navigation |
| **Developer at keyboard** | slash prompts, run status, evidence in their IDE | Slash commands, readable status, ergonomic resume/halt control |
| **Orcho run itself** | workflow connections, evidence trail | Stamped metadata (parent_run_id, plan_source), durable artefacts |

When a change benefits one audience at the expense of another (e.g.
hint inflation that bloats payload but helps weak LLMs) we choose
**explicitly**, not by default.

## 2. Personas + rollout tracks

The product matures through three sharply-scoped tracks. We do not
skip ahead — solo polish gates team capability, team capability
gates CI orchestration.

| Track | Persona | Target | "Done" criterion |
|---|---|---|---|
| **A** (now) | Solo dev | Polish the Claude Code experience | A single developer can take an idea → plan → review → implement → ship without consulting docs mid-flow |
| **B** (next) | Small team (2–5) | Shared run visibility | Two devs can hand a paused run to each other without out-of-band coordination |
| **C** (endgame) | CI / orchestration | Headless, programmatic, multi-project | An "openclaw-killer" pipeline: many runs across many projects, machine-driven, with human review only at gates |

Solo polish is **not optional** — without it the LLM experience is
brittle and team / CI inherit the same brittleness multiplied by
participants. Fix the foundation once.

## 3. Primary client target

| Client | Tier | MCP feature coverage |
|---|---|---|
| **Claude Code** | Primary | tools + prompts + resources, full active-discovery |
| **Codex CLI** | Secondary | tools + return-payload hints (no prompts/resources support in cli today) |
| Cursor / Zed | Best-effort | tools + resources, prompts experimental |
| Other MCP clients | Compatible | tools work; no client-specific UX |

We invest first in **tool-descriptor + return-payload hints** because
they reach every client. We add **prompts** as a Claude Code +
Cursor enhancement. Resources sit somewhere in between (Claude Code
+ Cursor + Zed surface them; Codex CLI does not).

This ordering also matches Track A → B → C: solo Claude Code is
the immediate audience; Codex CLI rises in importance for CI
(headless, no human-in-loop prompts needed).

## 4. Principles (load-bearing)

Four rules. Each is a decision filter — any proposed MCP change is
checked against all four before merging.

### Principle 1: Active discovery through payloads

> Every response that leads to a workflow decision **must** carry a
> `next_actions: list[Action]` field — even if empty.

Catalogs degrade for weaker LLMs. The MCP shape that survives
across Sonnet / Codex / Cursor is *"here's what just happened, and
here are the next steps you can take."* The LLM does not need to
remember workflow patterns — they ride in the payload.

Counter-example: `--from-run-plan` was reachable only by reading the
tool docstring. Claude Opus figured it out;
weaker models would not. After Pattern A (§5.1) lands, an
`orcho_run_start(profile="planning")` completion will carry
`next_actions=[{tool: "orcho_run_start", args: {from_run_plan: <id>,
profile: "feature"}, intent: "..."}]` and any LLM sees the
follow-up.

### Principle 2: Human prompts as workflow templates

> Workflows that involve a human-in-the-loop decision get a **named
> prompt template** in the MCP `prompts/list` catalog.

Tools are atomic operations; **workflows** (plan → review → resume,
or paused-run → triage → continue) are sequences. Exposing them as
named prompts gives the developer slash commands in Claude Code:

```
/orcho_followup_from_plan <parent_id>
/orcho_review_paused_run <run_id>
/orcho_halt_with_reason <run_id> <reason>
```

This is the **active human surface**. The LLM does not have to
infer "the user probably wants to follow up on the paused run" —
the developer says so explicitly.

### Principle 3: Durable artefacts are resources

> Any file under `runs/<id>/` that has read-meaning without a full
> pipeline run is exposed as an MCP resource (`orcho://...`).

Resources are the read path for the LLM and (in some clients) for
the developer too. They turn `parsed_plan.json`, `meta.json`,
evidence bundles, `events.jsonl` into addressable, lazily-loaded
content. The LLM can `read_resource(uri)` directly when reasoning
needs the actual bytes — without re-running anything.

### Principle 4: Single intent per tool; workflow per prompt

> Tools are atomic, named after the action. Workflows are named
> prompts. Do not conflate them.

We keep `orcho_run_start` as one tool (one entry point with rich
params: `from_run_plan`, `profile`, `task`, etc.) but do **not**
overload it with workflow guidance. Workflow guidance lives in
prompts (§Principle 2) or in return-payload hints (§Principle 1).

This rule prevents the slippery slope where each "the dev probably
wants X next" assumption gets baked into the tool itself and the
tool becomes unprincipled.

## 5. Roadmap: Track A (Solo Dev Polish)

Four implementation patterns. Order is chosen for **incremental value**
— each one ships value standalone, and later patterns build on
earlier ones.

### A1. `next_actions` surface in response payloads

**Goal**: implement Principle 1 across all response types that
follow a workflow decision.

**Scope**:

- **orcho-core SDK**: add `Action` dataclass + `next_actions: list[Action]
  = []` field on the SDK-visible result shapes:
  - `RunStartedResult` (`orcho_run_start` return)
  - `RunStatus` (`orcho_run_status` return)
  - `PhaseHandoffDecideResult` (`orcho_phase_handoff_decide` return)
- **orcho-core logic**: compute `next_actions` based on run state.
  Rules:
  - run completed with `parsed_plan.json` + `status=awaiting_phase_handoff`
    → `[{tool: "orcho_run_start", args: {from_run_plan: <run_id>,
      profile: "feature"}, intent: "Spawn implementation run from this plan"}]`
  - run paused on handoff → action per `available_actions` from the
    phase-handoff payload (`continue` / `retry_feedback` /
    `continue_with_waiver` / `halt`),
    each as a separate Action
  - run done (terminal) → `[]`
  - run failed → action for `--resume`
- **orcho-mcp**: pass through `next_actions` in the Pydantic response
  models. No transformation — the field rides through cleanly.

**Action shape**:

```python
class Action(BaseModel):
    intent: str            # human-readable: "Spawn implementation run from this plan"
    tool: str              # "orcho_run_start"
    args: dict[str, Any]   # {"from_run_plan": "...", "profile": "feature"}
    optional: bool = True  # True = alternative; False = required to continue workflow
```

**Sizing**: ~300 LOC core (logic + dataclass + tests) + ~50 LOC
MCP pass-through. SDK schema regen. MCP schema regen.

**Tests**:
- unit: `compute_next_actions(run_state) -> list[Action]` for each
  branch (no plan, plan + paused, handoff + actions, done, failed)
- integration: drive a mock pipeline → assert `next_actions` content
  matches expected workflow
- L2/L3 MCP smoke: `next_actions` field appears in `tools/list`
  schema and survives a real `tools/call` round-trip

**Why first**: closes the discoverability gap from `--from-run-plan`
that motivated this entire vision exercise. Every weaker LLM
benefits immediately.

### A2. Workflow prompts catalog

**Goal**: implement Principle 2. Surface named prompts for
common solo-dev workflows.

**Scope**:

- `src/orcho_mcp/prompts.py` with `@mcp.prompt`
  registrations:
  - `orcho_plan_then_implement(task: str, project_dir: str)` —
    full two-step workflow: plan run, then `--from-run-plan` child
  - `orcho_followup_from_plan(parent_run_id: str, profile: str = "feature")`
    — spawn implementation from existing plan
  - `orcho_review_paused_run(run_id: str)` — guide LLM to inspect a
    paused run's handoff payload and propose a decide action
  - `orcho_halt_with_reason(run_id: str, reason: str)` — generate
    a halt request with structured reason
  - `orcho_resume_failed_run(run_id: str)` — guide LLM through
    `--resume` semantics and parent-state classification

**MCP plumbing**: imports `from .prompts import *` in `server.py`
to trigger registration. Per orcho-mcp CLAUDE.md anti-pattern #1
(dual-import FastMCP), use the canonical instance from `instance.py`.

**Sizing**: ~150 LOC + ~5 prompt templates of 10-30 lines each.

**Tests**:
- L2 (server registration): `await mcp.list_prompts()` returns all
  five names with correct argument schemas
- L3 (stdio E2E): subprocess MCP server, real `prompts/get` call
  returns templated content
- Snapshot of `mcp_schema.json` includes the prompts catalog

**Why second**: gives Claude Code users immediate slash-command
ergonomics. Builds on A1 (the prompts can reference the same
workflow patterns Pattern A computes).

### A3. Resource discoverability audit + fill gaps

**Goal**: implement Principle 3. Make sure every durable artefact
under `runs/<id>/` is reachable via an `orcho://` URI.

**Scope**:

- Audit existing `src/orcho_mcp/resources/` handlers against the
  artefact set produced by current pipeline runs:
  - `runs/<id>/meta.json`
  - `runs/<id>/parsed_plan.json`
  - `runs/<id>/plan_<id>_r<n>.md` (human)
  - `runs/<id>/plan_<id>_r<n>.json` (per-attempt machine)
  - `runs/<id>/events.jsonl`
  - `runs/<id>/evidence/*` (bundles)
  - `runs/<id>/runner.log`, `output.log`, `progress.log`
  - `runs/<id>/phase_handoff_decisions/*.json`
- Add MCP resource registrations for gaps. Each gets a stable
  `orcho://runs/{run_id}/<artefact>` URI with appropriate mime
  type (`application/json` for json, `text/markdown` for md, etc).
- Resource list and reads must respect the workspace resolution
  (find_runs_dir).

**Sizing**: ~200 LOC. Mostly catalog work.

**Tests**:
- L2: `await mcp.list_resources()` enumerates expected URIs given
  a synthetic runs dir
- L3: `resources/read` against a real fake_workspace returns
  byte-correct content
- Mock smoke: `orcho_run_start --mock`, then `read_resource(parsed_plan.json)`
  returns the JSON we just wrote

**Why third**: makes the system **inspectable**. LLM can dig into
specifics without re-running. But less urgent than A1/A2 because
the LLM can already read files via filesystem tools in most clients.

### A4. RunStatus enrichment

**Goal**: implement Principle 1 + 3 in the polling surface.

**Scope**:

- Extend `RunStatus` (the `orcho_run_status` return type) with:
  - `available_followups: list[Action]` — same Action shape as A1
  - `artefacts: list[ArtefactRef]` — list of `{uri, kind, size_bytes}`
    pointing at the resources A3 exposed
  - `parent_run_id: str | None` — surfaces follow-up linkage
    from run metadata
  - `children_run_ids: list[str]` — reverse link: which runs were
    spawned from this one (requires a small index in the runs dir
    or scan)

**Sizing**: ~250 LOC. Core logic + SDK shape + MCP pass-through.

**Tests**:
- unit: artefact enumeration; followup computation
- integration: parent → child via `--from-run-plan` → parent's
  status now lists child in `children_run_ids`
- MCP L3: status payload includes all new fields

**Why fourth**: polling surface is less hot path than tool returns
(A1) or slash commands (A2). But essential for monitoring and
team-track work.

## 6. Track B (Team) — intent only

Track B starts once Track A polish is sufficient that a solo
dev can complete real work without out-of-band knowledge. Then
the focus shifts to **handing runs between people**:

- **Notifications when run state changes** — webhooks /
  WebSocket for paused runs (dev A → dev B "this needs your
  review")
- **Shared evidence reading** — multi-user resources, read-only
  perspectives on someone else's run
- **Collaborative review surfaces** — a run can be "claimed" by
  one reviewer; status visible to others; handoff between
  reviewers
- **Audit trail of who decided what** — handoff decisions
  carry actor identity beyond the existing `note` field

Out of scope for this document beyond high-level intent. Detailed
design notes will be drafted when Track A completes.

## 7. Track C (CI Orchestration) — intent only

The endgame Orcho leverages: **headless multi-run orchestration**
that beats general-purpose dev-agent tools (OpenHands / Aider /
Cursor agent) in CI by being fundamentally workflow-aware:

- **Headless defaults** — `--no-interactive` is the default for
  programmatic clients; human-in-loop prompts return structured
  pending-decision payloads instead of blocking on stdin
- **Batch run primitives** — spawn N runs across N projects, wait
  for all, aggregate
- **Multi-project pipelines** — cross-project handoff bundle
  (already partially exists) becomes a first-class CI primitive
- **Programmatic state inspection** — every run state observable
  via structured API, no transcript scraping
- **Run dependency graphs** — "run B starts only after run A's
  handoff is approved" expressible declaratively

Detailed design notes will be drafted after Track B foundations are in
place.

## 8. Non-goals for Track A

Explicit cuts to keep Track A focused:

- **Live progress notifications** — Track A is polling-based.
  Live progress over `progressToken` is reserved for Track B
  (multi-user has more need of it).
- **Authentication / authorization** — solo dev runs on local
  trust. Track B introduces actor identity.
- **Multi-workspace orchestration in one MCP server** — solo dev
  has one active workspace. Track C handles multi.
- **A11y / theming / human-friendly run summaries** — solo dev
  reads LLM-summarized runs through the client; no separate
  human dashboard. Web dashboard (orcho-web) is parallel work.
- **Cost projection per planned action** — `next_actions` carries
  intent + args, not cost estimate. Estimating cost of a not-yet-
  run pipeline is itself a Track C problem.

## 9. Sequencing of Track A changes

Recommended order:

1. **A1** (next_actions) — highest leverage, smallest schema
   change. Ship first; everything else can compose on top.
2. **A2** (prompts catalog) — builds on A1 (prompts can reference
   the same workflow patterns). Solo-dev visible improvement.
3. **A4** (RunStatus enrichment) — uses A1's Action shape +
   A3's resource URIs. Sits between A2 and A3 in dependency.
4. **A3** (resources audit) — independent but lowest urgency
   (existing filesystem tools cover ad-hoc reads).

Realistic alternative: ship A1 → A3 → A4 → A2 if we want resources
in place before the prompts catalog references them. Either works;
A1 is non-negotiably first.

## 10. Cross-repo coordination

Per `orcho-mcp/CLAUDE.md` "Per-phase validation rule": every
orcho-core wire-format change ships with an MCP E2E smoke in the
same commit. The patterns in §5 mostly satisfy this:

- A1: orcho-core SDK shape change → orcho-mcp passes through. MCP
  schema regen + L3 smoke.
- A2: orcho-mcp-only addition. No orcho-core change.
- A3: orcho-mcp-only catalog. No orcho-core change.
- A4: orcho-core SDK shape change → orcho-mcp passes through. Same
  as A1.

Each Track A change should explicitly call out whether orcho-core
changes are needed and ship the matched MCP smoke.

## 11. Open questions

To resolve before A1 starts:

1. **Action validation** — does MCP validate `next_actions[].tool` is
   a registered tool name + `args` matches that tool's schema? Or
   trust the producer? Recommend strict validation in MCP layer so
   a bad Action surfaces immediately.
2. **Action persistence** — does `next_actions` get persisted to
   `meta.json` for evidence? Or recomputed on every request?
   Recommend recompute (state-derived, no drift risk).
3. **Intent text language** — `Action.intent` in English only, or
   honors `AppConfig.task_language`? Recommend English-only for now
  (workflow semantics); locale-aware text is Track B.

## 12. Status pointers

- Solo dev surface today: `tools` (full coverage), `resources`
  (partial — A3 audit needed), `prompts` (empty — A2 to fill).
- Closest existing pattern to imitate for A1: `phase_handoff` payload
  already carries `available_actions: list[str]` — A1 is the same
  idea generalized + richer (intent + args).
