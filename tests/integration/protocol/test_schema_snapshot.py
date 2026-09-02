"""Schema snapshot — golden-file diff for the public MCP catalog.

L1+L2 layer in the methodology: drives the same FastMCP introspection
methods Claude Code calls over the wire, compares against the committed
``docs/mcp_schema.json``. Catches schema drift (anti-pattern #5):
Pydantic models change → JSON Schema changes → clients see a different
shape, and without this test nothing fails until a downstream consumer
breaks in production.

Failure path:
  1. Test fails with a diff showing what changed.
  2. Reviewer decides whether the change is intentional.
  3. If yes: ``python tools/dump_mcp_schema.py`` regenerates the file,
     it goes into the same commit as the implementation change.
  4. If no: revert the implementation.

This is the closest thing MCP has to OpenAPI breaking-change linting.
"""
from __future__ import annotations

import json
from pathlib import Path

from orcho_mcp import tools
from orcho_mcp.discovery import collect_catalog

_REPO_ROOT = Path(__file__).parent.parent.parent.parent.resolve()
_SCHEMA_FILE = _REPO_ROOT / "docs" / "mcp_schema.json"


def _load_committed_schema() -> dict:
    assert _SCHEMA_FILE.is_file(), (
        f"{_SCHEMA_FILE} missing — generate it with "
        "`python tools/dump_mcp_schema.py`."
    )
    return json.loads(_SCHEMA_FILE.read_text(encoding="utf-8"))


def test_schema_matches_committed_snapshot():
    """The live catalog matches docs/mcp_schema.json byte-for-byte (logically).

    Compare parsed structures rather than raw text so whitespace
    differences don't trip the test — only structural changes count.
    """
    live = collect_catalog()
    committed = _load_committed_schema()

    if live != committed:
        # Print enough context for the failure message to be actionable.
        live_tools = {t["name"] for t in live["tools"]}
        committed_tools = {t["name"] for t in committed["tools"]}
        live_resources = {r["uri"] for r in live["resources"]}
        committed_resources = {r["uri"] for r in committed["resources"]}
        live_templates = {t["uriTemplate"] for t in live["resourceTemplates"]}
        committed_templates = {
            t["uriTemplate"] for t in committed["resourceTemplates"]
        }
        live_prompts = {p["name"] for p in live["prompts"]}
        committed_prompts = {p["name"] for p in committed["prompts"]}

        diff_lines = []
        for label, live_set, committed_set in (
            ("tools", live_tools, committed_tools),
            ("resources", live_resources, committed_resources),
            ("templates", live_templates, committed_templates),
            ("prompts", live_prompts, committed_prompts),
        ):
            added = live_set - committed_set
            removed = committed_set - live_set
            if added or removed:
                diff_lines.append(f"  {label}:")
                if added:
                    diff_lines.append(f"    + {sorted(added)}")
                if removed:
                    diff_lines.append(f"    - {sorted(removed)}")

        if not diff_lines:
            diff_lines.append(
                "  (set membership identical — schema bodies differ; "
                "regenerate to see the change)"
            )

        raise AssertionError(
            "MCP catalog drift detected — docs/mcp_schema.json is stale.\n"
            "Surface changes:\n"
            + "\n".join(diff_lines)
            + "\n\n"
            + "Run: python tools/dump_mcp_schema.py\n"
            + "If the change is intentional, commit the regenerated file. "
            + "If not, revert the implementation."
        )


def test_committed_snapshot_has_expected_shape():
    """Defensive sanity: file exists, parses, top-level keys are present.

    Cheap canary against accidentally committing an empty / malformed
    snapshot when the dump script breaks silently.
    """
    committed = _load_committed_schema()
    for key in ("tools", "resources", "resourceTemplates", "prompts"):
        assert key in committed, f"missing top-level key: {key}"
        assert isinstance(committed[key], list), f"{key} must be a list"
    # The catalog is non-empty — guard against accidental truncation that
    # would silently pass the structural check above.
    assert len(committed["tools"]) >= 9
    assert len(committed["resources"]) >= 3
    assert len(committed["resourceTemplates"]) >= 5
    assert len(committed["prompts"]) >= 1
    # The focused delivery-gate read-tool is part of the published catalog.
    tool_names = {t["name"] for t in committed["tools"]}
    assert "orcho_delivery_gate" in tool_names
    assert "orcho_delivery_decide" in tool_names


def test_run_inspection_tools_explain_their_operator_questions():
    """The public MCP catalog tells clients which read tool to choose."""
    committed = _load_committed_schema()
    descriptions = {
        tool["name"]: tool.get("description") or ""
        for tool in committed["tools"]
    }

    # run_status is the durable snapshot and routes live-progress questions to
    # orcho_run_live_status rather than positioning itself as the progress view.
    assert (
        "Durable status snapshot for one run"
        in descriptions["orcho_run_status"]
    )
    assert "orcho_run_live_status" in descriptions["orcho_run_status"]
    assert (
        "What happened / what proves it?"
        in descriptions["orcho_run_evidence"]
    )
    assert "How much did it consume?" in descriptions["orcho_run_metrics"]
    assert "What changed?" in descriptions["orcho_run_diff"]


def test_live_catalog_pins_progress_navigation_and_tool_exports() -> None:
    """Catalog introspection, rather than source docstrings, is the contract.

    The first description line is what clients see while choosing a tool.  Pin
    the short intent labels here, and make ``tools.__all__`` an exact manifest
    of the tools actually registered on the shared FastMCP instance.
    """
    live = collect_catalog()
    descriptions = {
        tool["name"]: tool.get("description") or ""
        for tool in live["tools"]
    }
    first_line = {
        name: description.splitlines()[0]
        for name, description in descriptions.items()
    }

    assert first_line["orcho_run_status"] == (
        "Durable status snapshot for one run — not the live-progress view."
    )
    assert first_line["orcho_run_live_status"] == (
        "Where is this run right now, and what do I do next? "
        "(subtask progress index/total)"
    )
    assert first_line["orcho_run_start"] == (
        "Start a real run (mock or live) in a detached subprocess; "
        "return run_id now."
    )
    assert first_line["orcho_run_project_typed"] == (
        "Mock-only, in-process typed run (blocking); for real runs use "
        "orcho_run_start."
    )
    assert first_line["orcho_run_project_typed_async"] == (
        "Mock-only, in-process typed run (non-blocking); for real runs use "
        "orcho_run_start."
    )
    assert "current_subtask" in descriptions["orcho_run_events_summary"]

    registered = {tool["name"] for tool in live["tools"]}
    exported = set(tools.__all__)
    assert exported == registered, (
        "tools.__all__ must exactly match the FastMCP catalog; "
        f"missing exports: {sorted(registered - exported)}; "
        f"extra exports: {sorted(exported - registered)}"
    )


def test_evidence_schema_publishes_canonical_scheduled_gate_ledger():
    """The evidence tool exposes ledger rows/events, not the retired cockpit."""
    committed = _load_committed_schema()
    evidence = next(
        tool for tool in committed["tools"] if tool["name"] == "orcho_run_evidence"
    )
    defs = evidence["outputSchema"]["$defs"]

    assert {
        "ReceiptEvidenceRecord",
        "ScheduledGateRowRecord",
        "ScheduledGateEventRecord",
        "VerificationTimelineRecord",
    } <= set(defs)
    assert not {
        "VerificationAutorunEventRecord",
        "VerificationCockpit",
        "VerificationGateCockpitRow",
        "VerificationTimelineGateRecord",
    } & set(defs)

    row = defs["ScheduledGateRowRecord"]
    assert set(row["properties"]) == {
        "command",
        "hook",
        "phase",
        "declared",
        "selectable",
        "selected",
        "execution_policy",
        "consequence",
        "disposition",
        "selection_reason",
        "executor",
        "trigger",
        "receipt_evidence",
    }
    assert row["properties"]["execution_policy"]["enum"] == [
        "manual", "suggest", "warn", "require", "unknown",
    ]
    assert row["properties"]["consequence"]["enum"] == [
        "none", "warning", "required_action",
    ]
    assert row["properties"]["disposition"]["anyOf"][0]["enum"] == [
        "not_selected",
        "manual_available",
        "suggested",
        "skipped_fresh",
        "executed_pass",
        "executed_fail",
        "residual_missing",
        "residual_stale",
        "residual_failed",
    ]
    assert row["properties"]["selected"]["anyOf"][0]["type"] == "boolean"
    assert row["properties"]["disposition"]["anyOf"][1]["type"] == "null"

    event = defs["ScheduledGateEventRecord"]
    assert event["properties"]["kind"]["enum"] == [
        "selection", "execution", "reuse", "receipt",
    ]
    timeline = defs["VerificationTimelineRecord"]
    assert set(timeline["properties"]) == {
        "schema_version", "run_id", "project", "finalized", "rows", "events",
    }
    for retired in (
        "status",
        "manual_only",
        "required",
        "gate_class",
        "class_source",
        "policy_summary",
        "autorun_events",
        "scheduled_trail_available",
    ):
        assert retired not in row["properties"]
        assert retired not in event["properties"]


def test_run_status_schema_publishes_lossless_cross_execution_graph() -> None:
    """Pin the additive cross-graph contract at the public catalog boundary."""
    committed = _load_committed_schema()
    status = next(
        tool for tool in committed["tools"] if tool["name"] == "orcho_run_status"
    )
    defs = status["outputSchema"]["$defs"]
    status_graph = status["outputSchema"]["properties"]["cross_execution_graph"]

    assert status_graph["anyOf"][0]["$ref"] == "#/$defs/CrossExecutionGraphRecord"
    assert status_graph["anyOf"][1] == {"type": "null"}
    assert status_graph["default"] is None

    graph = defs["CrossExecutionGraphRecord"]
    assert set(graph["properties"]) == {"compile_identity", "nodes"}
    assert graph["properties"]["compile_identity"] == {
        "$ref": "#/$defs/CrossExecutionGraphCompileIdentityRecord",
    }
    assert graph["properties"]["nodes"]["items"] == {
        "$ref": "#/$defs/CrossExecutionGraphNodeRecord",
    }

    node = defs["CrossExecutionGraphNodeRecord"]
    assert set(node["properties"]) == {
        "identity", "kind", "dependencies", "owner", "executor", "required",
        "status", "reason", "alias", "operations",
    }
    assert node["properties"]["executor"] == {
        "$ref": "#/$defs/CrossExecutionGraphExecutorPolicyRecord",
    }
    assert node["properties"]["operations"]["items"] == {
        "$ref": "#/$defs/CrossExecutionGraphOperationRecord",
    }

    operation = defs["CrossExecutionGraphOperationRecord"]
    assert set(operation["properties"]) == {
        "alias", "executor", "phase", "hook", "command",
    }
    policy = defs["CrossExecutionGraphExecutorPolicyRecord"]
    assert set(policy["properties"]) == {
        "executor", "handler", "enabled", "run", "on_skip", "mode",
    }

    assert node["properties"]["kind"]["enum"] == [
        "global_phase", "project", "contract_check", "cross_final_acceptance",
    ]
    assert node["properties"]["owner"]["enum"] == ["global", "project", "runner"]
    assert policy["properties"]["executor"]["enum"] == [
        "global_handler", "project_pipeline", "runner_gate",
    ]
    assert node["properties"]["status"]["enum"] == [
        "pending", "ready", "running", "blocked", "completed", "skipped",
    ]
    assert node["properties"]["reason"]["enum"] == [
        "global_already_completed", "child_completed", "child_running",
        "child_paused", "child_failed", "child_inconsistent",
        "optional_project_not_run", "dependency_pending", "dependency_blocked",
        "runner_gate_completed", "runner_gate_skipped", "runner_gate_running",
        "policy_disabled", "policy_never", "fact_mismatch",
    ]
    assert operation["properties"]["executor"]["enum"] == [
        "child_phase", "child_scheduled_gate",
    ]


def test_criterion_contract_is_published_on_the_read_and_action_surface():
    """M8: the ADR 0188 contract clients see is the one MCP promises.

    Pins the shapes a client branches on — the discriminated ``method``, the
    state enum in core's canonical order, the summary keys — and the two
    absence rules that a Pydantic default would silently violate:
    ``criterion_matrix`` and the unused human-decision keys are published as
    optional NON-NULLABLE properties, so no client is told to expect ``null``.
    """
    committed = _load_committed_schema()
    evidence = next(
        t for t in committed["tools"] if t["name"] == "orcho_run_evidence"
    )
    defs = evidence["outputSchema"]["$defs"]

    assert {
        "CriterionMatrixRecord",
        "CriterionMatrixSummaryRecord",
        "CriterionRowRecord",
        "CriterionGateRefRecord",
        "CriterionProofRefRecord",
        "CriterionMethodGates",
        "CriterionMethodInspection",
        "CriterionMethodManual",
        "PlanCriterionRecord",
        "TaskAcceptanceRefsRecord",
    } <= set(defs)

    # The snapshot key-sorts schema objects, so this pins the property SET;
    # payload key ORDER is pinned by the byte-equivalence tests against the
    # core SDK, which is where it is actually load-bearing.
    row = defs["CriterionRowRecord"]["properties"]
    assert set(row) == {
        "criterion_id", "intent", "verify", "executors", "method",
        "proof_refs", "state", "reason", "blocking",
    }
    assert row["verify"]["enum"] == ["executable", "agent_assertion", "human"]
    assert row["state"]["enum"] == [
        "proven", "failed", "stale", "missing", "not_selected",
        "advisory", "accepted", "rejected", "pending",
    ]
    assert row["method"]["discriminator"] == {
        "propertyName": "kind",
        "mapping": {
            "gates": "#/$defs/CriterionMethodGates",
            "inspection": "#/$defs/CriterionMethodInspection",
            "manual": "#/$defs/CriterionMethodManual",
        },
    }
    # Each arm declares only its own keys — no null placeholders.
    assert set(defs["CriterionMethodGates"]["properties"]) == {"kind", "gate_refs"}
    assert set(defs["CriterionMethodInspection"]["properties"]) == {"kind"}
    assert set(defs["CriterionMethodManual"]["properties"]) == {
        "kind", "instructions",
    }
    assert defs["CriterionProofRefRecord"]["properties"]["kind"]["enum"] == [
        "receipt", "finding", "claim", "human_decision",
    ]
    assert set(defs["CriterionMatrixSummaryRecord"]["properties"]) == {
        "total", "blocking_open", "ready", "counts_by_state",
        "pending_human_ids",
    }

    # Absent, not null: a plain ``$ref`` with no ``anyOf: [..., null]`` arm,
    # and not required.
    matrix = evidence["outputSchema"]["properties"]["criterion_matrix"]
    assert matrix["$ref"] == "#/$defs/CriterionMatrixRecord"
    assert "anyOf" not in matrix
    assert "criterion_matrix" not in evidence["outputSchema"].get("required", [])

    # Typed criteria on the plan slice — never a list of strings.
    criteria = defs["PlanSliceRecord"]["properties"]["acceptance_criteria"]
    assert criteria["items"] == {"$ref": "#/$defs/PlanCriterionRecord"}

    # The durable decision log is readable from the same tool, so a client
    # that reconnects can resolve a ``human_decision`` proof ref.
    decisions = evidence["outputSchema"]["properties"]["criterion_decisions"]
    assert {"$ref": "#/$defs/HumanCriterionDecisionRecord"} in [
        arm.get("items", {}) for arm in decisions["anyOf"] if "items" in arm
    ]


def test_published_criterion_schema_is_as_strict_as_the_contract():
    """The schema clients validate against must not be weaker than the contract.

    A permissive schema is worse than no schema: it invites a client to trust
    a shape MCP does not actually guarantee, and it lets a truncated payload
    pass as valid. Each assertion below is a rule the interface contract
    states — required keys, non-empty collections, the id grammar — expressed
    where a client can actually see it.
    """
    committed = _load_committed_schema()
    evidence = next(
        t for t in committed["tools"] if t["name"] == "orcho_run_evidence"
    )
    defs = evidence["outputSchema"]["$defs"]

    # Every row key is mandatory — none may be defaulted in by a reader.
    assert set(defs["CriterionRowRecord"]["required"]) == {
        "criterion_id", "intent", "verify", "executors", "method",
        "proof_refs", "state", "reason", "blocking",
    }
    # A criterion always has an owner.
    assert defs["CriterionRowRecord"]["properties"]["executors"]["minItems"] == 1
    # Row and criterion ids follow the ADR 0188 grammar.
    assert defs["CriterionRowRecord"]["properties"]["criterion_id"]["pattern"] == (
        "^C[1-9][0-9]*$"
    )
    assert defs["PlanCriterionRecord"]["properties"]["id"]["pattern"] == (
        "^C[1-9][0-9]*$"
    )
    # A ``gates`` method names at least one gate; a ``manual`` one carries text.
    assert defs["CriterionMethodGates"]["properties"]["gate_refs"]["minItems"] == 1
    assert defs["CriterionMethodManual"]["properties"]["instructions"][
        "minLength"
    ] == 1
    # A gate identity needs a real command and hook (``phase`` may be empty for
    # the non-phase-anchored hooks).
    gate = defs["CriterionGateRefRecord"]["properties"]
    assert gate["command"]["minLength"] == 1
    assert gate["hook"]["minLength"] == 1
    assert "minLength" not in gate["phase"]
    # Every summary key is mandatory and the counters are non-negative.
    assert set(defs["CriterionMatrixSummaryRecord"]["required"]) == {
        "total", "blocking_open", "ready", "counts_by_state", "pending_human_ids",
    }
    summary = defs["CriterionMatrixSummaryRecord"]["properties"]
    assert summary["total"]["minimum"] == 0
    assert summary["blocking_open"]["minimum"] == 0
    # The matrix cannot be conjured from a partial payload.
    assert set(defs["CriterionMatrixRecord"]["required"]) == {"rows", "summary"}

    # ``counts_by_state`` is a closed contract, not a free mapping: only known
    # states, only positive counts.
    counts = defs["CriterionMatrixSummaryRecord"]["properties"]["counts_by_state"]
    assert counts["propertyNames"]["enum"] == [
        "proven", "failed", "stale", "missing", "not_selected",
        "advisory", "accepted", "rejected", "pending",
    ]
    assert counts["additionalProperties"] == {
        "exclusiveMinimum": 0, "type": "integer",
    }

    # No criterion model accepts an unknown key. Pydantic's default is to DROP
    # one, which would let MCP repair a malformed core payload into a
    # plausible-looking wire payload; the published schema says otherwise.
    for name in (
        "CriterionGateRefRecord",
        "PlanCriterionRecord",
        "TaskAcceptanceRefsRecord",
        "CriterionMethodGates",
        "CriterionMethodInspection",
        "CriterionMethodManual",
        "CriterionProofRefRecord",
        "CriterionRowRecord",
        "CriterionMatrixSummaryRecord",
        "CriterionMatrixRecord",
    ):
        assert defs[name]["additionalProperties"] is False, name


def test_criterion_decide_publishes_an_optional_verdict_and_both_outcomes():
    """M8: the elicitation path is reachable through the generated schema.

    ``decision`` must be optional on the INPUT schema — that is what lets a
    client call the tool without a verdict and reach the elicitation /
    operator-input branch instead of failing validation at the boundary.
    """
    committed = _load_committed_schema()
    tool = next(
        t for t in committed["tools"] if t["name"] == "orcho_criterion_decide"
    )

    props = tool["inputSchema"]["properties"]
    assert tool["inputSchema"]["required"] == ["run_id", "criterion_id"]
    assert props["decision"]["anyOf"][0]["enum"] == ["accept", "reject"]

    defs = tool["outputSchema"]["$defs"]
    assert {
        "CriterionDecisionRecordedResult",
        "CriterionDecisionInputRequiredResult",
        "HumanCriterionDecisionRecord",
    } <= set(defs)

    decision = defs["HumanCriterionDecisionRecord"]
    assert set(decision["required"]) == {
        "decision_id", "run_id", "criterion_id", "decision", "recorded_at",
    }
    # The optional audit keys are optional, non-nullable, and non-empty when
    # present — the three properties together are what make "absent" the only
    # legitimate spelling of "unused".
    for key in ("note", "actor", "supersedes"):
        assert decision["properties"][key] == {
            "minLength": 1,
            "title": key.replace("_", " ").title(),
            "type": "string",
        }, key
    # ``recorded_at`` is an opaque string, not a format-coerced datetime.
    assert decision["properties"]["recorded_at"]["type"] == "string"
    assert "format" not in decision["properties"]["recorded_at"]
    assert decision["additionalProperties"] is False

    # A recorded decision may report an unavailable readback WITHOUT
    # retracting the decision — both keys absent-not-null.
    recorded = defs["CriterionDecisionRecordedResult"]["properties"]
    assert recorded["matrix"]["$ref"] == "#/$defs/CriterionMatrixRecord"
    assert "anyOf" not in recorded["matrix"]
    assert recorded["matrix_error"]["type"] == "string"
    assert set(defs["CriterionDecisionRecordedResult"]["required"]) == {
        "run_id", "criterion_id", "decision",
    }


def test_criterion_descriptions_teach_the_trust_discipline():
    """M11: the published descriptions tell captains what counts as proof.

    The M11 claim is an ``agent_assertion`` — a reviewer reads the tool text
    and judges whether it teaches the right instinct. This test does not
    replace that judgement; it pins the specific load-bearing sentences so a
    later edit cannot quietly delete them and leave the reviewed claim
    describing text that no longer exists.

    The instinct being taught: proof comes from official receipts and typed
    human decisions. Prose does not become proof by being confident.
    """
    committed = _load_committed_schema()
    descriptions = {
        tool["name"]: tool.get("description") or "" for tool in committed["tools"]
    }

    evidence = descriptions["orcho_run_evidence"]
    assert "Trust discipline" in evidence
    assert "only a fresh official gate receipt" in evidence
    assert "only a typed human decision" in evidence
    assert (
        "a developer claim, a\n        reviewer finding, a transcript command, "
        "or a command-name-only match\n        is NEVER proof" in evidence
    )
    assert "nothing here is\n        recomputed by the MCP server" in evidence

    decide = descriptions["orcho_criterion_decide"]
    assert "**Never decide on the operator's behalf.**" in decide
    assert (
        "Do not infer ``accept`` from a\n    passing test, a reviewer finding, "
        "a transcript line, a phase-handoff\n    decision, or an approving "
        "remark in chat." in decide
    )
    assert "Nothing is written." in decide
