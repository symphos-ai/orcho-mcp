from __future__ import annotations

from sdk import EvidenceCommandRecord

from orcho_mcp.inspection.evidence import inspect_run_evidence


def test_commands_slice_preserves_bounded_managed_command_fields(
    monkeypatch,
) -> None:
    secret = "sentinel-secret-must-not-project"
    record = EvidenceCommandRecord(
        argv_summary="python3",
        cwd="",
        exit_code=0,
        duration_s=5.0,
        outcome="success",
        source="managed",
        identity_digest="a" * 64,
        phase="implement",
        state="exited",
        executable="python3",
        started_at="2026-07-21T10:25:47+00:00",
        finished_at="2026-07-21T10:25:52+00:00",
        artifact_path=f"managed_commands/receipts/{'a' * 64}.attempt.json",
    )
    monkeypatch.setattr(
        "orcho_mcp.inspection.evidence._sdk_list_evidence_commands",
        lambda run_id, cwd=None: [record],
    )

    result = inspect_run_evidence("rid", slice="commands")

    assert result.commands is not None
    assert len(result.commands) == 1
    projected = result.commands[0]
    assert projected.source == "managed"
    assert projected.state == "exited"
    assert projected.exit_code == 0
    assert projected.identity_digest == "a" * 64
    assert projected.phase == "implement"
    assert projected.executable == "python3"
    assert projected.artifact_path is not None
    assert secret not in projected.model_dump_json()
