# SPDX-License-Identifier: Apache-2.0

from pathlib import Path

from pipeline.plugins import load_plugin
from pipeline.verification_contract import VerificationContract
from pipeline.verification_selection import (
    SelectionContext,
    build_scheduled_gate_plan,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_mcp_plugin_blocks_failed_provenance_and_lint() -> None:
    plugin = load_plugin(PROJECT_ROOT)
    contract = VerificationContract.from_plugin(plugin)

    assert contract is not None
    assert contract.delivery_policy == "require"

    plan = build_scheduled_gate_plan(
        contract,
        SelectionContext(work_mode="pro"),
    )
    entries = {
        (entry.command, entry.hook, entry.phase): entry
        for entry in plan.entries
    }

    provenance = entries[("env-provenance", "after_phase", "implement")]
    assert (provenance.policy, provenance.action) == ("require", "handoff")

    lint = entries[("lint", "after_phase", "implement")]
    assert (lint.policy, lint.action) == ("require", "repair_loop")


def test_mcp_runtime_gate_does_not_duplicate_baseline_commands() -> None:
    plugin = load_plugin(PROJECT_ROOT)
    contract = VerificationContract.from_plugin(plugin)

    assert contract is not None
    runtime = contract.gate_sets["mcp-runtime"]
    assert runtime.commands == ("run-control-unit",)
