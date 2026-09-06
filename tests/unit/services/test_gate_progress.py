"""The gate projection consumes the SDK contract and its shared error owner."""
from dataclasses import replace

import pytest
from sdk import NoWorkspace, RunNotFound
from sdk.gate_progress import GateProgressSnapshot

from orcho_mcp.errors import RunNotFoundError, WorkspaceNotResolvedError
from orcho_mcp.services import gate_progress as gp


def test_bounded_projection_and_absence(monkeypatch):
    snap = GateProgressSnapshot("unit", "after_phase", "implement", "i", "t", "t", 1,
                                None, "x" * 4000, "y" * 4000, True, "running", None, None)
    monkeypatch.setattr(gp, "read_active_gate_progress", lambda *a, **kw: snap)
    result = gp.project_active_gate("r")
    assert result.stdout_tail == "x" * 2000
    assert result.stderr_tail == "y" * 2000
    assert result.invocation_id == "i"
    snap = replace(snap, has_output=False, stdout_tail="", stderr_tail="")
    assert gp.project_active_gate("r").has_output is False
    monkeypatch.setattr(gp, "read_active_gate_progress", lambda *a, **kw: None)
    assert gp.project_active_gate("r") is None


@pytest.mark.parametrize(("error", "mapped"), [(RunNotFound, RunNotFoundError),
                                                 (NoWorkspace, WorkspaceNotResolvedError)])
def test_sdk_errors_use_shared_mapping(monkeypatch, error, mapped):
    def fail(*args, **kwargs):
        raise error("missing")
    monkeypatch.setattr(gp, "read_active_gate_progress", fail)
    with pytest.raises(mapped):
        gp.project_active_gate("r")
