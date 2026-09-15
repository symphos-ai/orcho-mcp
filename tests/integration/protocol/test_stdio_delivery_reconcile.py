"""A real MCP client records delivery and receives structured SDK refusals."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from pipeline.engine.delivery_ledger import default_delivery_subject, load_delivery_ledger

from tests.fixtures.mcp_workspace import init_git_repo, meta, write_run
from tests.fixtures.stdio import initialized_stdio_session

pytestmark = [pytest.mark.mcp_protocol, pytest.mark.serial]

RUN_ID = "20260914_000001"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()


@pytest.mark.anyio
@pytest.mark.parametrize("lookup", ["ambient", "workspace", "runs_dir"])
async def test_stdio_records_rejected_delivery_and_preserves_refusals(
    fake_workspace, tmp_path, lookup,
):
    repo = tmp_path / "project"
    init_git_repo(repo)
    baseline = _git(repo, "rev-parse", "HEAD")
    run_dir = write_run(
        fake_workspace, RUN_ID,
        meta=meta(
            status="halted", halt_reason="commit_delivery_pending", project=str(repo),
            worktree={"path": str(repo), "base_ref": baseline},
            phases={"final_acceptance": {
                "verdict": "REJECTED", "approved": False,
                "short_summary": "Acceptance remains rejected.", "release_blockers": [],
            }},
        ),
    )
    original_meta = (run_dir / "meta.json").read_bytes()
    args = {"run_id": RUN_ID, "operator": "Test operator", "note": "Verified delivery"}
    # Explicit SDK selectors must work even when the server's ambient
    # workspace contains no such run.
    server_workspace = fake_workspace
    if lookup != "ambient":
        args[lookup] = str(fake_workspace if lookup == "workspace" else run_dir.parent)
        server_workspace = tmp_path / "other-workspace"
        (server_workspace / "runspace" / "runs").mkdir(parents=True)

    async with initialized_stdio_session(server_workspace) as (session, _):
        tool = next(t for t in (await session.list_tools()).tools
                    if t.name == "orcho_reconcile_delivery")
        assert set(tool.inputSchema["properties"]) == {
            "run_id", "operator", "commit", "note", "workspace", "runs_dir",
        }
        assert set(tool.inputSchema["required"]) == {"run_id", "operator"}
        assert "blocker" in tool.outputSchema["properties"]

        missing = await session.call_tool(tool.name, args)
        assert missing.isError is False
        assert missing.structuredContent["accepted"] is False
        assert missing.structuredContent["blocker"] == "no_delivery_commit_found"
        assert (run_dir / "meta.json").read_bytes() == original_meta
        assert not (run_dir / "commit_decisions").exists()

        (repo / "app.txt").write_text("delivered\n", encoding="utf-8")
        _git(repo, "add", "app.txt")
        _git(repo, "commit", "-q", "-m", default_delivery_subject(RUN_ID))
        sha = _git(repo, "rev-parse", "HEAD")

        mismatch = await session.call_tool(tool.name, {**args, "commit": baseline})
        assert mismatch.isError is False
        assert mismatch.structuredContent["accepted"] is False
        assert mismatch.structuredContent["blocker"] == "commit_mismatch"
        assert mismatch.structuredContent["commit_sha"] == sha
        assert (run_dir / "meta.json").read_bytes() == original_meta
        assert not (run_dir / "commit_decisions").exists()

        recorded = await session.call_tool(tool.name, {**args, "commit": sha[:12]})
        assert recorded.isError is False
        payload = recorded.structuredContent
        assert payload["accepted"] is True
        assert payload["blocker"] is None
        assert payload["commit_sha"] == sha
        assert payload["release_verdict"] == "REJECTED"
        assert payload["terminal_outcome"] == "done"
        artifact_path = Path(payload["artifact_path"])
        artifact_bytes = artifact_path.read_bytes()
        artifact = json.loads(artifact_bytes)
        assert artifact["operator"] == args["operator"]
        assert artifact["note"] == args["note"]
        assert artifact["commit_sha"] == sha
        persisted = json.loads((run_dir / "meta.json").read_text())
        assert persisted["commit_delivery"]["provenance"] == "reconciled"
        assert persisted["delivery_override"]["provenance"] == "reconciled"
        assert persisted["phases"]["final_acceptance"]["verdict"] == "REJECTED"
        ledger = load_delivery_ledger(run_dir, RUN_ID)
        assert ledger.commit_sha == sha
        assert ledger.stage == "recorded"
        assert ledger.provenance == "reconciled"

        again = await session.call_tool(tool.name, args)
        assert again.isError is False
        assert again.structuredContent["accepted"] is False
        assert again.structuredContent["blocker"] == "already_recorded"
        assert artifact_path.read_bytes() == artifact_bytes
        assert _git(repo, "rev-parse", "HEAD") == sha
        assert _git(repo, "status", "--porcelain") == ""
