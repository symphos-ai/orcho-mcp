"""Wire result for recording an existing delivery through the core SDK."""
from __future__ import annotations

from pydantic import BaseModel, Field


class DeliveryReconcileResult(BaseModel):
    """SDK reconciliation outcome, including typed refusals in ``blocker``."""

    run_id: str
    accepted: bool
    state: str
    commit_sha: str | None = None
    blocker: str | None = None
    reason: str = ""
    artifact_path: str | None = None
    terminal_outcome: str | None = None
    release_verdict: str | None = None
    notes: list[str] = Field(default_factory=list)
