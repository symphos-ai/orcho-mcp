"""Unit tests for the typed silent boundary pilot
(``orcho_run_project_typed`` + ``run_project_typed_silent`` adapter).

Three layers of coverage:

  1. **Adapter contract** — monkeypatch ``run_project_pipeline`` and
     assert the ``ProjectRunRequest`` the adapter built carries
     ``presentation=PresentationPolicy.SILENT`` and
     ``no_interactive=True``, plus the right task / project_dir /
     output_dir / profile / max_rounds.

  2. **Response shape from a fake ProjectRunResult** — the adapter
     reads ``status`` / ``halt_reason`` from the in-memory session
     and ``event_kinds`` from ``events.jsonl`` written under
     ``output_dir``. No stdout / file path parsing of free-text
     transcripts.

  3. **Source-level grep guard** — the adapter and the tool handler
     do not inspect stdout transcript markers (``DONE``, ``Run dir:``,
     ``Session:``, ``[PLAN]``, ``[IMPLEMENT]``). This guard trips on
     accidental drift toward CLI-shaped parsing.

A small end-to-end smoke (``MockAgentProvider`` driven through the
real ``run_project_pipeline``) lives at the bottom — sub-second cost,
proves the boundary works without monkeypatch.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from orcho_mcp.errors import InvalidPlanError
from orcho_mcp.run_control.typed_pilot import (
    _collect_event_kinds,
    run_project_typed_silent,
)
from orcho_mcp.schemas import TypedRunResult
from orcho_mcp.tools import orcho_run_project_typed

# ── shared helpers ────────────────────────────────────────────────────


def _write_events(run_dir: Path, kinds: list[str]) -> None:
    """Lay down a minimal ``events.jsonl`` so the adapter's spine
    read has something to parse. Each event is a one-line JSON
    object with at least ``kind``."""
    run_dir.mkdir(parents=True, exist_ok=True)
    events_path = run_dir / "events.jsonl"
    lines = [json.dumps({"kind": k, "seq": i}) for i, k in enumerate(kinds)]
    events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class _CapturingFakePipeline:
    """Captures the typed request the adapter constructs and returns
    a caller-controlled fake :class:`ProjectRunResult`."""

    def __init__(
        self,
        *,
        run_dir: Path,
        session: dict[str, Any],
        events: list[str],
        run_id: str = "test_run_123",
    ) -> None:
        self.run_dir = run_dir
        self.session = session
        self.events = events
        self.run_id = run_id
        self.captured_request: Any = None

    def __call__(self, request: Any) -> Any:
        """Stand-in for ``pipeline.project.app.run_project_pipeline``."""
        from pipeline.project.types import ProjectRunResult

        self.captured_request = request
        # The real pipeline writes events.jsonl to ``output_dir``; the
        # fake mirrors that so the adapter's post-run spine read has
        # something to find.
        _write_events(self.run_dir, self.events)
        return ProjectRunResult(
            session=self.session,
            output_dir=self.run_dir,
            run_id=self.run_id,
        )


# ── 1. Adapter contract ───────────────────────────────────────────────


def test_pilot_adapter_builds_silent_typed_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The adapter must construct a ``ProjectRunRequest`` with
    ``presentation=PresentationPolicy.SILENT`` and
    ``no_interactive=True``. These are the load-bearing flags the
    pilot exists to pin — drift on either trips this test."""
    from pipeline.project.types import (
        PresentationPolicy,
        ProjectRunRequest,
    )

    run_dir = tmp_path / "run"
    fake = _CapturingFakePipeline(
        run_dir=run_dir,
        session={"status": "done"},
        events=["run.start", "phase.start", "phase.end", "run.end"],
    )
    # The adapter routes the run through ``RunService.start``, whose
    # default lazy-imports ``pipeline.project.app.run_project_pipeline``
    # fresh on each call — so we monkeypatch the source module's name,
    # which both the service default and any direct import resolve to.
    monkeypatch.setattr(
        "pipeline.project.app.run_project_pipeline", fake,
    )

    result = run_project_typed_silent(
        task="pilot smoke",
        project_dir="/some/project",
        output_dir=str(run_dir),
        profile="task",
        mock=True,
        max_rounds=1,
    )

    # Adapter packed a typed result.
    assert isinstance(result, TypedRunResult)

    # Adapter captured the typed request the pipeline saw.
    req = fake.captured_request
    assert isinstance(req, ProjectRunRequest), (
        f"adapter must call run_project_pipeline with a "
        f"ProjectRunRequest; got {type(req).__name__}"
    )

    # ── load-bearing assertions: SILENT + no_interactive ────────────
    assert req.presentation is PresentationPolicy.SILENT, (
        f"adapter must set presentation=SILENT; got {req.presentation!r}"
    )
    assert req.no_interactive is True, (
        "adapter must set no_interactive=True (post-init invariant on "
        "ProjectRunRequest: SILENT requires no_interactive=True)"
    )

    # ── routing fields threaded through ────────────────────────────
    assert req.task == "pilot smoke"
    assert req.project_dir == "/some/project"
    assert req.output_dir == run_dir
    assert req.profile_name == "task"
    assert req.max_rounds == 1
    # mock=True means the adapter constructs MockAgentProvider; the
    # request carries it as provider (not as a `mock` bool — orcho-core
    # has no `mock` field on ProjectRunRequest, the caller wires the
    # provider).
    assert req.provider is not None, (
        "mock=True must wire a MockAgentProvider into the request"
    )


def test_pilot_routes_run_through_run_service_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stage 5 adoption invariant: the pilot must consume orcho-core's
    headless ``RunService.start`` — not call ``run_project_pipeline``
    directly.

    This patches a *different seam* than the request/result-parity tests
    above: those patch ``pipeline.project.app.run_project_pipeline``, the
    endpoint both ``RunService.start`` *and* a direct call would reach, so
    they pass either way. Here we replace ``sdk.run_control.RunService``
    itself (resolved by the adapter's lazy ``from sdk.run_control import
    RunService`` at call time). If the adapter reverted to a direct
    pipeline call, the fake service's ``start`` would never run and the
    ``ProjectRunRequest`` assertion below would fail — making the adoption
    requirement executable, not just a code-reading claim.
    """
    from pipeline.project.types import (
        PresentationPolicy,
        ProjectRunRequest,
        ProjectRunResult,
    )

    run_dir = tmp_path / "run"
    captured: dict[str, Any] = {}

    class _FakeRunService:
        """Stand-in for ``sdk.run_control.RunService`` — records the
        request handed to ``.start`` and returns a controlled result."""

        def start(self, request: Any) -> Any:
            captured["request"] = request
            # Mirror the real pipeline's side effect so the adapter's
            # post-run spine read finds an events.jsonl to parse.
            _write_events(run_dir, ["run.start", "run.end"])
            return ProjectRunResult(
                session={"status": "done"},
                output_dir=run_dir,
                run_id="rs_routed",
            )

    monkeypatch.setattr("sdk.run_control.RunService", _FakeRunService)

    result = run_project_typed_silent(
        task="route smoke",
        project_dir="/p",
        output_dir=str(run_dir),
        profile="task",
        mock=True,
        max_rounds=1,
    )

    # ── adoption: the run went through RunService.start ─────────────
    req = captured.get("request")
    assert isinstance(req, ProjectRunRequest), (
        "run_project_typed_silent must call RunService().start(request); "
        "the fake service never received a ProjectRunRequest, so the run "
        "was not routed through RunService.start (likely reverted to a "
        "direct run_project_pipeline call)."
    )
    # The SILENT-typed request shape is still pinned on the adopted path.
    assert req.presentation is PresentationPolicy.SILENT
    assert req.no_interactive is True

    # ── parity: the service's ProjectRunResult is packed verbatim ───
    assert isinstance(result, TypedRunResult)
    assert result.run_id == "rs_routed"
    assert result.output_dir == str(run_dir)
    assert result.status == "done"
    assert result.halt_reason is None
    assert result.event_kinds == ["run.start", "run.end"]


def test_pilot_adapter_rejects_real_provider_path() -> None:
    """``mock=False`` is out of scope for the pilot — embedders that
    accidentally request a real-provider run via the pilot tool
    instead of ``orcho_run_start`` get a clear error at the boundary."""
    with pytest.raises(InvalidPlanError, match="mock-only"):
        run_project_typed_silent(
            task="t",
            project_dir="/p",
            output_dir="/o",
            mock=False,
        )


def test_pilot_adapter_rejects_empty_required_args() -> None:
    """Three required strings can't be empty/blank. Catches embedders
    that forward unvalidated client args straight through."""
    with pytest.raises(InvalidPlanError, match="'task'"):
        run_project_typed_silent(
            task="", project_dir="/p", output_dir="/o",
        )
    with pytest.raises(InvalidPlanError, match="'project_dir'"):
        run_project_typed_silent(
            task="t", project_dir="", output_dir="/o",
        )
    with pytest.raises(InvalidPlanError, match="'output_dir'"):
        run_project_typed_silent(
            task="t", project_dir="/p", output_dir="",
        )


# ── 2. Response shape from a fake ProjectRunResult ────────────────────


def test_pilot_response_packs_done_session_correctly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Done path — ``halt_reason`` is ``None``, ``event_kinds``
    carries the canonical spine."""
    run_dir = tmp_path / "run"
    fake = _CapturingFakePipeline(
        run_dir=run_dir,
        session={"status": "done"},
        events=["run.start", "phase.start", "phase.end", "run.end"],
        run_id="ts_done",
    )
    monkeypatch.setattr(
        "pipeline.project.app.run_project_pipeline", fake,
    )

    result = run_project_typed_silent(
        task="t",
        project_dir="/p",
        output_dir=str(run_dir),
    )

    assert result.run_id == "ts_done"
    assert result.output_dir == str(run_dir)
    assert result.status == "done"
    assert result.halt_reason is None
    assert result.event_kinds == [
        "run.start", "phase.start", "phase.end", "run.end",
    ]


def test_pilot_response_surfaces_failure_halt_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed path — ``status`` and ``halt_reason`` flow through to
    the wire model. Embedders surface non-done terminals to UI from
    these two fields without re-reading meta.json."""
    run_dir = tmp_path / "run"
    fake = _CapturingFakePipeline(
        run_dir=run_dir,
        session={
            "status": "failed",
            "halt_reason": "phase_failure:RuntimeError",
            "failure": {"type": "RuntimeError", "error": "boom"},
        },
        events=["run.start", "phase.start", "phase.end", "run.end"],
        run_id="ts_failed",
    )
    monkeypatch.setattr(
        "pipeline.project.app.run_project_pipeline", fake,
    )

    result = run_project_typed_silent(
        task="t",
        project_dir="/p",
        output_dir=str(run_dir),
    )

    assert result.status == "failed"
    assert result.halt_reason == "phase_failure:RuntimeError"


def test_pilot_response_handles_missing_events_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the pipeline failed before writing any events, the adapter
    still produces a valid wire model — ``event_kinds=[]``, no
    crash."""
    from pipeline.project.types import ProjectRunResult

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    # No events.jsonl written — events sink never opened.

    def _fake(_request: Any) -> Any:
        return ProjectRunResult(
            session={"status": "failed", "halt_reason": "bootstrap_error"},
            output_dir=run_dir,
            run_id="ts_bootstrap_fail",
        )

    monkeypatch.setattr(
        "pipeline.project.app.run_project_pipeline", _fake,
    )

    result = run_project_typed_silent(
        task="t",
        project_dir="/p",
        output_dir=str(run_dir),
    )

    assert result.status == "failed"
    assert result.event_kinds == []


def test_collect_event_kinds_skips_blank_and_malformed_lines(
    tmp_path: Path,
) -> None:
    """Direct test of the helper — proves a truncated tail or empty
    line doesn't crash the spine read."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "events.jsonl").write_text(
        '{"kind": "run.start"}\n'
        '\n'  # blank line
        '{"kind": "phase.start"}\n'
        '{"kind"}\n'  # malformed — must not crash
        '{"no_kind_field": true}\n'  # missing kind — skip
        '{"kind": "run.end"}\n',
        encoding="utf-8",
    )
    assert _collect_event_kinds(run_dir) == [
        "run.start", "phase.start", "run.end",
    ]


# ── 3. Source-level grep guard ────────────────────────────────────────


_BANNED_TRANSCRIPT_MARKERS = [
    # CLI transcript markers that any caller parsing stdout would look
    # for. The pilot is library-call shape — none of these belong in
    # the adapter or the handler body.
    "[PLAN]",
    "[IMPLEMENT]",
    "[REVIEW]",
    "[DONE]",
    "Run dir:",
    "Session:",
    "Usage:",
    "[FAILED]",
    "▶ SUB-PIPELINE",
]


def test_pilot_does_not_parse_stdout_transcript_markers() -> None:
    """The pilot adapter + tool handler are pure structured-state
    code — no string-matching against CLI transcript markers.

    This grep guard trips on accidental drift toward "look for DONE in
    stdout" patterns. If a transcript marker legitimately appears in
    the source (e.g. as part of a docstring's anti-pattern example),
    the offending file should not be the pilot adapter — push such
    examples to docs.
    """
    pkg_root = (
        Path(__file__).resolve().parents[3] / "src" / "orcho_mcp"
    )
    pilot_files = [
        pkg_root / "run_control" / "typed_pilot.py",
    ]
    for source in pilot_files:
        src = source.read_text(encoding="utf-8")
        for marker in _BANNED_TRANSCRIPT_MARKERS:
            assert marker not in src, (
                f"{source.name} contains transcript marker "
                f"{marker!r} — the pilot is library-call shape and "
                f"must not pattern-match against CLI output. Push any "
                f"transcript example to docs or comment phrasing."
            )


# ── 3b. Wire-model field guards (no accidental wire expansion) ─────────


def test_typed_run_result_wire_fields_are_pinned() -> None:
    """``TypedRunResult`` carries exactly the five wire fields the pilot
    promises — no more, no less.

    The RunService.start adoption must stay byte-for-byte wire-neutral;
    a stray field added during adapter work (e.g. leaking a richer
    ProjectRunResult slice into the response) would silently widen the
    MCP contract. This guard trips on any such drift.
    """
    from orcho_mcp.schemas import TypedRunResult

    assert set(TypedRunResult.model_fields) == {
        "run_id", "output_dir", "status", "halt_reason", "event_kinds",
    }


def test_typed_run_started_result_wire_fields_are_pinned() -> None:
    """``TypedRunStartedResult`` (async start) carries exactly its four
    wire fields. Same anti-drift contract as the blocking result above."""
    from orcho_mcp.schemas import TypedRunStartedResult

    assert set(TypedRunStartedResult.model_fields) == {
        "run_id", "output_dir", "status", "started_at",
    }


# ── 4. Tool handler delegates without inspection ──────────────────────


def test_tool_handler_delegates_to_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The @mcp.tool handler is a thin one-line delegation. This test
    confirms calling ``orcho_run_project_typed(...)`` as a plain
    Python function flows straight into the adapter and returns the
    typed wire model."""
    run_dir = tmp_path / "run"
    fake = _CapturingFakePipeline(
        run_dir=run_dir,
        session={"status": "done"},
        events=["run.start", "run.end"],
    )
    monkeypatch.setattr(
        "pipeline.project.app.run_project_pipeline", fake,
    )

    result = orcho_run_project_typed(
        task="handler smoke",
        project_dir="/p",
        output_dir=str(run_dir),
    )

    assert isinstance(result, TypedRunResult)
    assert result.status == "done"
    assert result.event_kinds == ["run.start", "run.end"]
    # Adapter saw the SILENT-typed request.
    from pipeline.project.types import PresentationPolicy

    assert fake.captured_request.presentation is PresentationPolicy.SILENT


# ── 5. End-to-end mock smoke (sub-second) ─────────────────────────────


def test_pilot_drives_real_mock_pipeline_end_to_end(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """End-to-end: no monkeypatch, ``MockAgentProvider`` drives the
    real ``run_project_pipeline`` through the pilot adapter. Proves
    the whole boundary works in a real call, not just at the unit
    seam.

    Pinned contract:
      * ``capsys.out == "" and capsys.err == ""`` — SILENT in
        practice, not just in dataclass field.
      * ``status == "done"`` from the structured session.
      * Canonical event spine present in ``event_kinds`` —
        ``run.start``, ``phase.start``, ``phase.end``, ``run.end``.
    """
    from tests.conftest import init_git_repo
    project = tmp_path / "proj"
    init_git_repo(project)

    run_dir = tmp_path / "runs" / "pilot_smoke"

    result = run_project_typed_silent(
        task="pilot end-to-end smoke",
        project_dir=str(project),
        output_dir=str(run_dir),
        profile="task",
        mock=True,
        max_rounds=1,
    )

    captured = capsys.readouterr()
    assert captured.out == "", (
        f"pilot must not leak stdout (stdio purity invariant); got "
        f"{len(captured.out)} chars: {captured.out[:200]!r}"
    )
    assert captured.err == "", (
        f"pilot must not leak stderr; got: {captured.err[:200]!r}"
    )

    assert isinstance(result, TypedRunResult)
    assert result.status == "done", (
        f"expected status=done; got {result.status!r}"
    )
    assert result.halt_reason is None
    assert result.output_dir == str(run_dir)
    assert result.run_id

    # Canonical spine present.
    for kind in ("run.start", "phase.start", "phase.end", "run.end"):
        assert kind in result.event_kinds, (
            f"event spine missing {kind!r}; got {result.event_kinds!r}"
        )
