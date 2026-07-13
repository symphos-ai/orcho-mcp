# Delivery-gate projection and decision

This note fixes the MCP contract for Orcho-managed post-release delivery /
correction gates.

MCP has two surfaces:

| Surface | Purpose | Mutates state |
|---|---|---|
| `orcho_delivery_gate(run_id)` | Read the gate kind, diff summary, SDK-available actions, SDK-blocked actions, and ready follow-up calls. | No |
| `orcho_delivery_decide(run_id, action, note?)` | Resolve a parked delivery / correction gate through orcho-core's SDK decision entrypoint. | Yes |

MCP never applies a patch, commits directly, or re-implements delivery policy.
The SDK owns the state transition and all hard guards.

## Authority

`orcho_delivery_gate` reads gate authority from
`sdk.delivery_decision_state(run_id, cwd=None)`.

That SDK state is the only source for:

- gate decidability;
- gate kind: `delivery`, `correction`, or `none`;
- `available_actions`;
- `blocked_actions`;
- `default_action`;
- guard reason text.

Durable artifacts still enrich the projection:

| Source | What MCP reads it for |
|---|---|
| `{run_dir}/meta.json` → `commit_delivery` | Release verdict, target checkout, retained worktree, changed and untracked path hints. |
| `{run_dir}/commit_decisions/{decision_id}.json` | Secondary changed-file fallback only. |
| `{run_dir}/diff.patch` | Secondary changed-file fallback and corruption signal only. |

Secondary artifact failures never hide a decidable gate. If
`commit_decisions` or `diff.patch` is missing / unreadable, the projection keeps
the SDK-derived kind, sets `diff.degraded=true`, and names the degraded
artifact in `message`.

## Projection shape

`DeliveryGateProjection.kind` maps SDK state as:

| SDK state | MCP kind |
|---|---|
| `kind="delivery", decidable=true` | `delivery_decision_required` |
| `kind="correction", decidable=true` | `correction_decision_required` |
| `decidable=false`, `commit_delivery.status` in `committed` / `applied_uncommitted` (not a superseded parent) | `delivery_completed` |
| `decidable=false`, any other status | `direct_checkout_or_running` |

A `delivery_completed` gate is terminal: the Orcho-managed delivery already
landed, so `available_actions` is empty and there is no decision to make. Its
payload carries the delivery facts read from `commit_delivery`: `published`
(true when a pull request is open), `pr_url` (that PR's live link), and
`delivery_notices` (the human-readable delivery lines). On a published
`delivery_completed` gate `pr_intent.suggested_command` is `None` — the durable
"run this to open a PR" command is stale once the push happened, so `pr_url` is
the authoritative link. This kind is distinct from
`direct_checkout_or_running`, which means nothing was delivered (a direct
checkout edit, a still-running run, or a skipped / halted / failed terminal).

`available_actions` contains only SDK-available actions. `blocked_actions`
contains actions core currently refuses, commonly `approve` / `apply` on a
rejected release or incomplete required verification.

`next_actions` carries one `ready_call` per available action:

```json
{
  "tool": "orcho_delivery_decide",
  "args": {"run_id": "<run_id>", "action": "<available_action>"},
  "kind": "ready_call"
}
```

The ready-call contract matters: each record contains every required argument
for the target tool. The caller may forward one selected record verbatim.

## Decision tool

`orcho_delivery_decide` delegates to:

```python
sdk.decide_delivery(run_id, action, note=note, cwd=None)
```

The result mirrors core's `DeliveryDecisionResult`:

| Field | Meaning |
|---|---|
| `accepted` | `true` only when the SDK executed the requested action. |
| `status` | Resulting commit-delivery status. |
| `terminal_outcome` | Strictly `done` or `halted`. |
| `blocker` | Typed refusal cause when `accepted=false`. |
| `halt_reason` | Structured halt reason for halted outcomes. |
| `artifact_paths` | Decision artifacts written by core. |
| `commit_sha` | Commit id when `approve` created one. |
| `followup_run_id` | Correction follow-up id when core starts one; otherwise `null`. |

Business refusals are data, not transport errors. For example, a stale direct
checkout returns `accepted=false, blocker="no_pending_delivery_gate"`. Missing
workspace, missing run, and invalid arguments still use the normal MCP error
taxonomy.
