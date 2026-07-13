"""Unit-layer shared fixtures.

Hosts hermeticity guards for the in-process mock-pipeline unit tests
(``run_control`` and ``observe`` drive ``run_project_pipeline`` /
``run_project_typed_silent`` under a ``MockAgentProvider`` in-process).

Any file under ``tests/unit/`` inherits these automatically through
pytest's standard conftest scoping. They do not reach the protocol or
acceptance layers.
"""
from __future__ import annotations

import pytest

# Write phases whose runtime must resolve to a *write-capable* mock under
# ``--mock``. ``MockAgentProvider`` maps each configured runtime to its mock
# agent; the ``codex`` mock is reviewer-only and raises on the write path
# ("Mock CodexAgent does not implement write path. Pin claude for build/fix
# phases under --mock"). The env-var stems mirror orcho-core's
# ``core.infra.config._PHASE_ENV_MAP``.
_MOCK_WRITE_PHASE_RUNTIME_ENV: tuple[str, ...] = (
    "RUNTIME_IMPLEMENT",
    "RUNTIME_REPAIR_CHANGES",
    "RUNTIME_REPAIR_ESCALATION",
)


@pytest.fixture(autouse=True)
def _pin_mock_write_phase_runtime(monkeypatch):
    """Pin build/fix phase runtimes to ``claude`` for the unit layer.

    The suite pins ``sdk`` / ``pipeline`` to the orcho-core checkout under
    review (see :mod:`tests._core_source`), so the in-process mock pipeline
    reads that checkout's ``config.defaults.json``. When a core working tree
    pins a write phase to a non-write mock runtime (e.g. ``codex``), the mock
    ``implement`` / ``repair`` phase aborts with ``NotImplementedError`` even
    though the run itself is not under test — a false red keyed on ambient
    core config rather than orcho-mcp behaviour.

    Forcing ``claude`` for the write phases keeps the mock unit tests hermetic
    against that drift, matching the documented ``--mock`` invariant ("pin
    claude for build/fix phases"). It is inert for tests that never launch a
    pipeline, and for any core config that already pins these phases to a
    write-capable runtime.

    ``core.infra.config.AppConfig.load`` caches the resolved phase spec for the
    life of the process, so an earlier non-unit test (outside this conftest's
    autouse scope) can populate that cache *before* these env vars are set. The
    cache is cleared here so the pin is authoritative regardless of collection
    order, and again on teardown so the restored env is re-read.
    """
    from core.infra import config as _core_config

    for env_var in _MOCK_WRITE_PHASE_RUNTIME_ENV:
        monkeypatch.setenv(env_var, "claude")
    _core_config._reset_config()
    yield
    _core_config._reset_config()
