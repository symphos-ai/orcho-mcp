"""Focused wire projection tests for the public cross graph SDK readers."""
from __future__ import annotations

from pathlib import Path

import pytest
from sdk import CrossExecutionGraphInvalid

from orcho_mcp.errors import InvalidPlanError
from orcho_mcp.services import run_reads


def _graph_and_state() -> tuple[object, object]:
    """Use promoted SDK-shaped values, including both child operation kinds."""
    from sdk import (
        CrossExecutionGraph,
        CrossExecutionGraphCompileIdentity,
        CrossExecutionGraphExecutor,
        CrossExecutionGraphExecutorPolicy,
        CrossExecutionGraphNode,
        CrossExecutionGraphNodeKind,
        CrossExecutionGraphNodeOwner,
        CrossExecutionGraphNodeState,
        CrossExecutionGraphOperation,
        CrossExecutionGraphOperationExecutor,
        CrossExecutionGraphReason,
        CrossExecutionGraphState,
        CrossExecutionGraphStatus,
    )

    graph = CrossExecutionGraph(
        CrossExecutionGraphCompileIdentity(7, "compile-fingerprint"),
        (
            CrossExecutionGraphNode(
                "producer", CrossExecutionGraphNodeKind.PROJECT, (),
                CrossExecutionGraphNodeOwner.PROJECT,
                CrossExecutionGraphExecutorPolicy(
                    CrossExecutionGraphExecutor.PROJECT_PIPELINE,
                    handler="ignored", enabled=False, run="always",
                    on_skip="allow", mode="task",
                ),
                required=False,
            ),
            CrossExecutionGraphNode(
                "consumer", CrossExecutionGraphNodeKind.CONTRACT_CHECK,
                ("producer",), CrossExecutionGraphNodeOwner.RUNNER,
                CrossExecutionGraphExecutorPolicy(
                    CrossExecutionGraphExecutor.RUNNER_GATE,
                    enabled=True, run="auto", on_skip="block", mode="full",
                ),
            ),
        ),
    )
    state = CrossExecutionGraphState((
        CrossExecutionGraphNodeState(
            "producer", CrossExecutionGraphNodeKind.PROJECT,
            CrossExecutionGraphStatus.RUNNING,
            CrossExecutionGraphReason.CHILD_RUNNING,
            alias="producer",
            operations=(
                CrossExecutionGraphOperation(
                    "producer", CrossExecutionGraphOperationExecutor.CHILD_PHASE,
                    "implement",
                ),
                CrossExecutionGraphOperation(
                    "producer",
                    CrossExecutionGraphOperationExecutor.CHILD_SCHEDULED_GATE,
                    "implement", "after_phase", ("python", "-m", "ruff"),
                ),
            ),
        ),
        CrossExecutionGraphNodeState(
            "consumer", CrossExecutionGraphNodeKind.CONTRACT_CHECK,
            CrossExecutionGraphStatus.PENDING,
            CrossExecutionGraphReason.DEPENDENCY_PENDING,
        ),
    ))
    return graph, state


def test_projection_round_trips_structure_state_order_and_operations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph, state = _graph_and_state()
    (tmp_path / "cross_execution_graph.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(run_reads, "_sdk_load_cross_execution_graph", lambda *a, **k: graph)
    monkeypatch.setattr(run_reads, "_sdk_load_cross_execution_graph_state", lambda *a, **k: state)

    result = run_reads._project_cross_execution_graph("cross-1", tmp_path)

    assert result is not None
    assert result.compile_identity.model_dump() == {
        "schema_version": 7, "fingerprint": "compile-fingerprint",
    }
    assert [node.identity for node in result.nodes] == ["producer", "consumer"]
    producer, consumer = result.nodes
    assert producer.dependencies == []
    assert producer.owner == "project"
    assert producer.required is False
    assert producer.executor.model_dump() == {
        "executor": "project_pipeline", "handler": "ignored", "enabled": False,
        "run": "always", "on_skip": "allow", "mode": "task",
    }
    assert producer.status == "running"
    assert producer.reason == "child_running"
    assert producer.alias == "producer"
    assert [operation.model_dump() for operation in producer.operations] == [
        {"alias": "producer", "executor": "child_phase", "phase": "implement", "hook": None, "command": []},
        {"alias": "producer", "executor": "child_scheduled_gate", "phase": "implement", "hook": "after_phase", "command": ["python", "-m", "ruff"]},
    ]
    assert consumer.dependencies == ["producer"]
    assert (consumer.status, consumer.reason) == ("pending", "dependency_pending")


def test_absent_graph_does_not_call_sdk_loaders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_loader(*args: object, **kwargs: object) -> object:
        raise AssertionError("graph loader must not run without the artifact")

    monkeypatch.setattr(run_reads, "_sdk_load_cross_execution_graph", unexpected_loader)
    monkeypatch.setattr(run_reads, "_sdk_load_cross_execution_graph_state", unexpected_loader)
    assert run_reads._project_cross_execution_graph("mono-1", tmp_path) is None


def test_existing_invalid_graph_maps_to_invalid_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "cross_execution_graph.json").write_text("{bad", encoding="utf-8")

    def invalid_loader(*args: object, **kwargs: object) -> object:
        raise CrossExecutionGraphInvalid("malformed cross graph")

    monkeypatch.setattr(run_reads, "_sdk_load_cross_execution_graph", invalid_loader)
    with pytest.raises(InvalidPlanError, match="malformed cross graph"):
        run_reads._project_cross_execution_graph("cross-1", tmp_path)


def test_identity_drift_fails_closed_as_invalid_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph, state = _graph_and_state()
    (tmp_path / "cross_execution_graph.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(run_reads, "_sdk_load_cross_execution_graph", lambda *a, **k: graph)
    monkeypatch.setattr(
        run_reads, "_sdk_load_cross_execution_graph_state",
        lambda *a, **k: type(state)(tuple(reversed(state.nodes))),
    )

    with pytest.raises(InvalidPlanError, match="identity mismatch"):
        run_reads._project_cross_execution_graph("cross-1", tmp_path)
