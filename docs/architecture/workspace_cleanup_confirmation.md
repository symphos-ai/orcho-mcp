# Workspace-cleanup confirmation

This note fixes the MCP contract for reclaiming disk from retained run
checkouts, and explains why one engine capability is exposed as two tools.

| Surface | Purpose | Mutates state |
|---|---|---|
| `orcho_workspace_cleanup_report(older_than_days?, force?)` | Run the engine's selection and publish it: reclaimable / protected / inert counts, per-reason breakdowns, and a `confirm_token`. | No |
| `orcho_workspace_cleanup_reclaim(tier, disposition, confirm_token, older_than_days?, force?)` | Remove the confirmed selection and return facts from the engine's durable receipt. | Yes |

MCP owns no cleanup policy. Selection, retention, value protection, archival,
and the receipt are all `orcho-core`'s (`sdk.report_workspace_cleanup` /
`sdk.reclaim_workspace_cleanup`). What MCP adds is the confirmation the CLI
gets for free.

## Why the split

On the CLI the operator reads the report on their own screen and then types
the reclaim flags. That sequence *is* the confirmation: a human saw the
selection and acted on it.

Over MCP the caller is a model. The protocol carries no evidence
distinguishing "the operator reviewed this selection and agreed" from "the
model decided to tidy up", and the operation removes checkouts. So the two
halves are separate tools, for two reasons:

- **Confirmation.** The reclaim requires a token only a report can mint.
- **Authorization granularity.** Clients allowlist by tool name. A single
  tool with a `mode` argument would mean permitting the preview also permits
  the removal.

## The token

`confirm_token` fingerprints one selection: the resolved runs directory, the
cutoff and `force` arguments, every bucket count, and every reason with its
count. It is opaque to clients — its only contract is that the reclaim
accepts it.

Reclaim re-derives the token from the **live** workspace before touching
anything and compares. That covers both failure modes with one check:

- a token that was never issued (invented, or copied from documentation);
- a token whose selection has since changed — a run finished, a checkout
  disappeared, another operator swept first.

Either way the call raises `WorkspaceCleanupConfirmationError` and nothing is
removed. The refusal travels the typed-error channel rather than a success
value so an empty sweep and a refused sweep can never read alike.

The token is deliberately not a lease: it does not reserve the selection or
block a concurrent CLI cleanup. It only guarantees that what gets removed is
what somebody reviewed.

## Argument coupling

`older_than_days` and `force` participate in the fingerprint, so they must
match between the report and the reclaim. Passing a different cutoff to the
reclaim is refused rather than silently reinterpreted.

`force` requires an explicit `older_than_days` on both halves, mirroring the
CLI contract: overriding value protections is a deliberate act and must not
ride on the default retention window.

## Reading a result

The report's three buckets answer different questions and must not be summed:

| Bucket | Meaning |
|---|---|
| `reclaimable_*` | What the matching reclaim would remove. |
| `protected_*` | What the engine refuses to touch — dirty, unpushed, or resumable work. The reasons say which. |
| `inert_*` | References with no retained checkout left at all. Not reclaimable space. |

`next_actions` is non-empty only when something is reclaimable, and is always
`operator_input_required`: `tier` and `disposition` are the operator's choice.

The receipt distinguishes `bytes_selected`, `bytes_archived`, and
`bytes_reclaimed`; under `disposition='archive'` nothing is destroyed. A sweep
can partially fail, so `status` plus `error_count` / `errors` is the honest
read, not the mere presence of a receipt.
