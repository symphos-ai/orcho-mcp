"""Unit tests for ``RunsSupervisor.spawn`` delegation to the SDK launch seam.

Spawn now delegates the OS mechanics (argv build, ``ORCHO_PIPELINE`` /
``auto-detect`` env handling, run-dir creation, ``runner.log``, and the
detached-session ``Popen``) to ``sdk.run_control.launch.launch_run``.
These tests therefore assert on the ``LaunchSpec`` the supervisor builds
and hands to that seam — profile / attachment / output_mode / session /
``from_run_plan`` threading, invalid-output_mode fast-fail, and the
minted ``run_id`` forwarded to ``launch_run`` — rather than the internal
subprocess call. The env/argv details are covered by the SDK's own unit
tests and the L4 mock-integration smoke.

One test drives an actual mock pipeline through the real ``launch_run``
to validate the small_task profile's session shape end-to-end.
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest
from sdk.run_control.launch import LaunchedRun, LaunchResult

from orcho_mcp.supervisor import RunsSupervisor


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _install_fake_launch(monkeypatch, fake_workspace, captured):
    """Patch the spawn module's ``launch_run`` seam.

    Records the ``LaunchSpec`` + ``run_id`` the supervisor builds, then
    returns a ``LaunchResult`` around a real fast-exit child so the
    background ``_reap`` can ``wait()`` on a live ``Popen`` exactly as it
    would for the real seam. Creates the run dir the neutral seam would
    have created so ``write_state`` can persist ``mcp_supervisor.json``.
    """
    runs_dir = fake_workspace / "runspace" / "runs"
    real_popen = subprocess.Popen

    def fake_launch_run(spec, *, run_id=None):
        captured["spec"] = spec
        captured["run_id"] = run_id
        run_dir = runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        popen = real_popen([sys.executable, "-c", "import sys; sys.exit(0)"])
        run = LaunchedRun(
            run_id=run_id,
            pid=popen.pid,
            pgid=popen.pid,
            run_dir=run_dir,
            project_dir=spec.project_dir,
            command=[sys.executable, "-m", "pipeline.project_orchestrator"],
            started_at="2026-07-07T00:00:00.000Z",
            mock=spec.mock,
            output_mode=spec.output_mode,
        )
        return LaunchResult(run=run, popen=popen)

    monkeypatch.setattr(
        "orcho_mcp.supervisor.spawn.launch_run", fake_launch_run
    )
    return captured


def _reap_cleanup(handle):
    if handle.popen and handle.popen.poll() is None:
        handle.popen.terminate()
        handle.popen.wait(timeout=2)


# ── profile propagation ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_spawn_threads_profile_into_launch_spec(
    tmp_path, fake_workspace, monkeypatch,
):
    """When ``profile`` is supplied to ``spawn`` it must land on the
    ``LaunchSpec.profile`` the supervisor hands to ``launch_run``.

    The concrete ``ORCHO_PIPELINE`` env override + ``--profile`` argv are
    now built inside ``launch_run`` (SDK) and covered there; the
    supervisor's contract is that it forwards the caller's profile
    verbatim on the spec and mints a ``run_id`` for the launch seam.

    Regression: pre-composition, the ``profile`` kwarg was silently
    dropped by ``supervisor.spawn``.
    """
    captured: dict[str, object] = {}
    _install_fake_launch(monkeypatch, fake_workspace, captured)

    sup = RunsSupervisor()
    handle = await sup.spawn(
        task="snapshot",
        project_dir=str(tmp_path),
        profile="small_task",
        mock=True,
    )
    try:
        spec = captured["spec"]
        assert spec.profile == "small_task"
        # The minted run_id is forwarded to the launch seam and echoed
        # back on the handle.
        assert captured["run_id"] == handle.run_id
    finally:
        _reap_cleanup(handle)


@pytest.mark.asyncio
async def test_spawn_threads_attachments_into_launch_spec(
    tmp_path, fake_workspace, monkeypatch,
):
    """Attachment parameters propagate onto the ``LaunchSpec``.

    ``orcho_run_start.attach`` / ``attach_text`` / ``attach_image`` /
    ``attach_binary`` each land on the matching spec field; the SDK seam
    turns them into ``--attach*`` argv pairs.
    """
    captured: dict[str, object] = {}
    _install_fake_launch(monkeypatch, fake_workspace, captured)

    sup = RunsSupervisor()
    handle = await sup.spawn(
        task="snapshot",
        project_dir=str(tmp_path),
        mock=True,
        attach=["spec.md", "logs/error.log"],
        attach_image=["mockup.png"],
    )
    try:
        spec = captured["spec"]
        assert spec.attach == ["spec.md", "logs/error.log"]
        assert spec.attach_image == ["mockup.png"]
        assert spec.attach_text is None
        assert spec.attach_binary is None
    finally:
        _reap_cleanup(handle)


@pytest.mark.asyncio
async def test_spawn_threads_output_mode_into_launch_spec(
    tmp_path, fake_workspace, monkeypatch,
):
    captured: dict[str, object] = {}
    _install_fake_launch(monkeypatch, fake_workspace, captured)

    sup = RunsSupervisor()
    handle = await sup.spawn(
        task="snapshot",
        project_dir=str(tmp_path),
        mock=True,
        output_mode="debug",
    )
    try:
        assert captured["spec"].output_mode == "debug"
        # The handle mirrors the mode the launch seam recorded.
        assert handle.output_mode == "debug"
    finally:
        _reap_cleanup(handle)


@pytest.mark.asyncio
async def test_spawn_threads_session_mode_into_launch_spec(
    tmp_path, fake_workspace, monkeypatch,
):
    captured: dict[str, object] = {}
    _install_fake_launch(monkeypatch, fake_workspace, captured)

    sup = RunsSupervisor()
    handle = await sup.spawn(
        task="snapshot",
        project_dir=str(tmp_path),
        mock=True,
        session_mode="stateless",
    )
    try:
        assert captured["spec"].session_mode == "stateless"
    finally:
        _reap_cleanup(handle)


@pytest.mark.asyncio
async def test_spawn_rejects_invalid_output_mode(
    tmp_path, fake_workspace, monkeypatch,
):
    """Shared validator must fail fast — before the launch seam is touched."""
    launch_calls: list = []

    def fail_if_called(spec, *, run_id=None):
        launch_calls.append(spec)
        raise AssertionError("launch_run should not run with invalid output_mode")

    monkeypatch.setattr("orcho_mcp.supervisor.spawn.launch_run", fail_if_called)

    sup = RunsSupervisor()
    with pytest.raises(ValueError, match="invalid output mode"):
        await sup.spawn(
            task="snapshot",
            project_dir=str(tmp_path),
            mock=True,
            output_mode="loud",
        )
    assert launch_calls == []


@pytest.mark.asyncio
async def test_profile_param_drives_session_shape(
    tmp_path, fake_workspace,
):
    """``profile="small_task"`` drives the expected scoped session shape.

    End-to-end through the *real* ``launch_run`` seam: when
    ``profile="small_task"`` is supplied, orcho-core dispatches via the
    small_task profile. Result: session contains the small_task scope
    phases: plan, validate_plan, and implement, with no review/fix
    rounds. ``small_task`` declares ``hypothesis.attempts=0``, so the
    hypothesis phase is intentionally absent on this profile.
    """
    from tests.conftest import init_git_repo
    project = tmp_path / "proj"
    init_git_repo(project)

    sup = RunsSupervisor()
    handle = await sup.spawn(
        task="small_task scope smoke",
        project_dir=str(project),
        profile="small_task",
        mock=True,
        max_rounds=1,
    )
    if handle.popen:
        handle.popen.wait(timeout=10)

    meta_path = handle.run_dir / "meta.json"
    assert meta_path.exists(), "meta.json not produced"
    meta = json.loads(meta_path.read_text())
    phases = meta.get("phases", {})
    assert "plan" in phases, (
        f"profile=small_task must produce plan phase; got {sorted(phases)}"
    )
    assert "validate_plan" in phases, (
        f"profile=small_task must validate the plan; got {sorted(phases)}"
    )
    assert "implement" in phases, (
        f"profile=small_task must produce implement phase; got {sorted(phases)}"
    )
    assert phases.get("rounds") == [], (
        f"profile=small_task must skip review/fix rounds; "
        f"got {phases.get('rounds')}"
    )


@pytest.mark.asyncio
async def test_spawn_defaults_to_feature_profile_on_launch_spec(
    tmp_path, fake_workspace, monkeypatch,
):
    """Without an explicit ``profile``, the spec carries the semantic
    default ``feature``. The SDK seam owns the rule that ``feature`` (and
    the implicit default) leave ``ORCHO_PIPELINE`` unset."""
    captured: dict[str, object] = {}
    _install_fake_launch(monkeypatch, fake_workspace, captured)

    sup = RunsSupervisor()
    handle = await sup.spawn(
        task="snapshot",
        project_dir=str(tmp_path),
        mock=True,
    )
    try:
        assert captured["spec"].profile == "feature"
    finally:
        _reap_cleanup(handle)


@pytest.mark.asyncio
async def test_spawn_threads_explicit_feature_profile_on_launch_spec(
    tmp_path, fake_workspace, monkeypatch,
):
    """An explicit ``profile="feature"`` is threaded verbatim onto the
    spec — indistinguishable from the default at the supervisor layer."""
    captured: dict[str, object] = {}
    _install_fake_launch(monkeypatch, fake_workspace, captured)

    sup = RunsSupervisor()
    handle = await sup.spawn(
        task="snapshot",
        project_dir=str(tmp_path),
        profile="feature",
        mock=True,
    )
    try:
        assert captured["spec"].profile == "feature"
    finally:
        _reap_cleanup(handle)


# ── --from-run-plan surface ─────────────────────────────────────────────────

def test_spawn_threads_from_run_plan_via_argv_builder():
    """The SDK argv builder emits a single ``--from-run-plan <spec>`` pair.

    Pins the argv contract the launch seam relies on when the supervisor
    forwards ``from_run_plan`` on the ``LaunchSpec``. Pure argv assertion,
    no subprocess; the L4 mock-integration smoke drives the real
    subprocess.
    """
    from pipeline.argv import build_orch_argv

    argv = build_orch_argv(
        project="/p",
        task="follow-up",
        run_id="20260524_test_child",
        output_dir="/runs/20260524_test_child",
        profile="feature",
        from_run_plan="20260523_test_parent",
    )
    assert "--from-run-plan" in argv
    assert argv[argv.index("--from-run-plan") + 1] == (
        "20260523_test_parent"
    )
    # Without --from-run-plan supplied, the flag is absent.
    plain = build_orch_argv(
        project="/p", task="t",
        run_id="x", output_dir="/runs/x",
        profile="feature",
    )
    assert "--from-run-plan" not in plain


@pytest.mark.asyncio
async def test_spawn_forwards_from_run_plan_onto_launch_spec(
    tmp_path, fake_workspace, monkeypatch,
):
    """``Supervisor.spawn(from_run_plan=...)`` must land the spec on the
    ``LaunchSpec.from_run_plan`` handed to ``launch_run`` (which turns it
    into the ``--from-run-plan`` argv pair)."""
    captured: dict[str, object] = {}
    _install_fake_launch(monkeypatch, fake_workspace, captured)

    sup = RunsSupervisor()
    handle = await sup.spawn(
        task="follow-up task",
        project_dir=str(tmp_path),
        mock=True,
        from_run_plan="20260523_test_parent",
    )
    try:
        assert captured["spec"].from_run_plan == "20260523_test_parent"
    finally:
        _reap_cleanup(handle)
