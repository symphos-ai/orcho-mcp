"""E2E mock-smoke: environment-provenance failure surfaces in the MCP cockpit.

End-to-end across the repo boundary with NO monkeypatch of the SDK seam: a
synthetic durable run dir (project plugin contract + a passing command receipt
for a phase-scheduled gate + a *failing* ``verification_environment`` phase
receipt) is read by the **real** ``sdk.get_verification_timeline`` and projected
through the **real** ``inspect_run_evidence`` MCP wire. It pins the companion
contract for the wire change ``GateProjection.detail``:

  - the gate scheduled at the broken phase reads ``status='FAIL'`` (not
    FRESH/PASS) in BOTH the ``verification_timeline`` and ``verification_cockpit``
    slices;
  - ``residual_failed`` carries the gate command;
  - the gate's ``detail`` names the failing ``pipeline_import`` check with its
    expected/actual, so an operator needs no raw logs.

Requires an orcho-core that exposes ``GateProjection.detail``. The connected
core is resolved in two steps so this companion smoke is never silently
masked when it is the delivery gate:

  - ``ORCHO_MCP_CORE_SRC`` (when set) points at the matching orcho-core
    checkout under development; its package dirs are prepended to
    ``sys.path`` so ``import sdk`` resolves to that checkout instead of a
    stale stable install. Only the engine packages are exposed, never the
    checkout's ``tests`` package (which would shadow the orcho-mcp suite).
  - ``ORCHO_MCP_REQUIRE_COMPANION`` (when truthy) makes the companion
    mandatory: if the resolved core *still* predates ``GateProjection.detail``
    the smoke FAILS as externally-blocked rather than skipping, so a delivery
    that changed the public SDK schema cannot be marked green off a masked skip.

Outside delivery mode (neither env set, stale stable core connected) the smoke
skips with a clear reason rather than asserting the old shape — the companion
is only meaningful against the matching core.
"""
from __future__ import annotations

import dataclasses
import json
import os
import sys
from pathlib import Path

import pytest

from tests.fixtures.mcp_workspace import write_run

# Engine packages a matching orcho-core checkout exposes. ``tests`` is
# deliberately excluded: the checkout ships a *regular* ``tests`` package whose
# ``__init__`` would terminate import resolution and shadow the orcho-mcp
# namespace ``tests`` package (breaking ``tests.fixtures.*``).
_CORE_PACKAGES = ("sdk", "pipeline", "core", "cli", "agents")


class _CoreSrcFinder:
    """Meta-path finder mapping engine package roots to a checkout directory.

    Mirrors setuptools' editable finder: it resolves only the named top-level
    packages (and their children) from the checkout, so ``import sdk`` picks up
    the in-development core while the checkout's ``tests`` package stays
    invisible. Order-independent (meta_path is consulted before ``sys.path``),
    so it overrides a stale editable/stable install without a global reinstall.
    """

    def __init__(self, root: Path) -> None:
        self._mapping = {
            pkg: root / pkg for pkg in _CORE_PACKAGES if (root / pkg).is_dir()
        }

    def find_spec(self, fullname, path=None, target=None):
        from importlib.machinery import PathFinder
        from importlib.util import spec_from_file_location

        if fullname in self._mapping:
            init = self._mapping[fullname] / "__init__.py"
            if init.exists():
                return spec_from_file_location(
                    fullname, init, submodule_search_locations=[str(init.parent)]
                )
            return None
        parent, _, _ = fullname.rpartition(".")
        if parent and parent in self._mapping:
            return PathFinder.find_spec(fullname, path=[str(self._mapping[parent])])
        return None


def _wire_matching_core() -> None:
    """Install a finder for an explicit orcho-core checkout's engine packages.

    Lets the delivery harness run this smoke against the in-development core
    (``ORCHO_MCP_CORE_SRC=<checkout>``) without a global editable reinstall and
    without exposing the checkout's ``tests`` package. No-op when unset or the
    path is incomplete; the require-mode gate below reports the consequence.
    """
    src = os.environ.get("ORCHO_MCP_CORE_SRC", "").strip()
    if not src:
        return
    root = Path(src)
    if not root.is_dir():
        return
    # Drop any already-resolved engine modules from a stale install so the new
    # finder rebinds them on next import.
    for name in list(sys.modules):
        if name in _CORE_PACKAGES or name.split(".", 1)[0] in _CORE_PACKAGES:
            del sys.modules[name]
    sys.meta_path.insert(0, _CoreSrcFinder(root))


_wire_matching_core()

# The contract: a single required gate ``env-provenance`` scheduled at
# after_phase(implement) with a ``require`` delivery policy, so a broken
# implement provenance receipt makes it a blocking FAIL.
_PLUGIN = '''\
PLUGIN = {
    "verification_envs": {"ci": {}},
    "verification": {
        "default_env": "ci",
        "required": ["env-provenance"],
        "delivery_policy": "require",
        "commands": {"env-provenance": {"run": "echo prov"}},
        "schedule": [
            {
                "after_phase": "implement",
                "policy": "require",
                "commands": ["env-provenance"],
            },
        ],
    },
}
'''

_EXPECTED = "/abs/checkout/pipeline/__init__.py"
_ACTUAL = "/abs/install/pipeline/__init__.py"


def _core_has_detail() -> bool:
    try:
        from sdk import GateProjection
    except ImportError:
        return False
    return "detail" in {f.name for f in dataclasses.fields(GateProjection)}


def _companion_required() -> bool:
    return os.environ.get("ORCHO_MCP_REQUIRE_COMPANION", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


_HAS_DETAIL = _core_has_detail()
_REQUIRED = _companion_required()

# Delivery gate: when the companion is mandated but the resolved core still
# predates ``GateProjection.detail``, fail loudly as EXTERNALLY BLOCKED instead
# of skipping. A wire change to the public SDK schema must not be marked green
# off a masked skip — see T5 of the delivery contract.
if _REQUIRED and not _HAS_DETAIL:
    def test_companion_externally_blocked() -> None:
        pytest.fail(
            "EXTERNALLY BLOCKED: ORCHO_MCP_REQUIRE_COMPANION is set but the "
            "resolved orcho-core predates GateProjection.detail. Point "
            "ORCHO_MCP_CORE_SRC at the matching orcho-core checkout (the one "
            "carrying the detail field) so this companion smoke runs against "
            "it; do not accept the delivery while the public SDK schema change "
            "is unmirrored.",
        )

# Outside delivery mode, a stale connected core is a benign skip rather than a
# red bar for unrelated local work. Applied per-test (not module-wide) so the
# externally-blocked gate above stays collectable and fails as intended.
_skip_if_stale = pytest.mark.skipif(
    not _HAS_DETAIL,
    reason="connected orcho-core predates GateProjection.detail",
)


def _write_project(root: Path) -> Path:
    project = root / "prov_project"
    plugin_dir = project / ".orcho" / "multiagent"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.py").write_text(_PLUGIN, encoding="utf-8")
    return project


def _write_command_receipt(run_dir: Path, command: str, *, exit_code: int = 0) -> None:
    rdir = run_dir / "verification_command_receipts"
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / f"{command}.json").write_text(
        json.dumps({
            "kind": "verification_command",
            "command": command,
            "env": "ci",
            "exit_code": exit_code,
            "assertions": [],
            "detail": "",
            "git": {
                "checkout_head": None,
                "baseline_head": None,
                "changed_files_fingerprint": None,
            },
            "dependencies": [],
        }),
        encoding="utf-8",
    )


def _write_failed_phase_receipt(run_dir: Path) -> Path:
    rdir = run_dir / "verification_receipts"
    rdir.mkdir(parents=True, exist_ok=True)
    path = rdir / "implement_round1.json"
    path.write_text(
        json.dumps({
            "phase": "implement",
            "round": 1,
            "kind": "verification_environment",
            "cwd": "/abs/checkout",
            "python": "3.12.0 (/abs/install/python)",
            "checks": [{
                "name": "pipeline_import",
                "expected": _EXPECTED,
                "actual": _ACTUAL,
                "passed": False,
            }],
            "commands": [{"argv": ["python", "-c", "..."], "exit_code": 0}],
            "temp_env_outside_checkout": True,
        }),
        encoding="utf-8",
    )
    return path


def _setup_run(fake_workspace: Path) -> str:
    project = _write_project(fake_workspace)
    run_id = "20260625_174403_06c642"
    run_dir = write_run(
        fake_workspace,
        run_id,
        meta={"task": "t", "status": "done", "project": str(project)},
    )
    # A fresh, passing command receipt would otherwise read present/PASS ...
    _write_command_receipt(run_dir, "env-provenance", exit_code=0)
    # ... but the implement phase's environment provenance broke.
    _write_failed_phase_receipt(run_dir)
    return run_id


@_skip_if_stale
@pytest.mark.acceptance
def test_cockpit_slice_projects_provenance_fail_with_detail(
    fake_workspace: Path,
) -> None:
    from orcho_mcp.inspection.evidence import inspect_run_evidence

    run_id = _setup_run(fake_workspace)

    result = inspect_run_evidence(run_id, slice="verification_cockpit")
    cockpit = result.verification_cockpit
    assert cockpit is not None

    gate = next(g for g in cockpit.gates if g.command == "env-provenance")
    # FAIL, not FRESH/PASS — the broken phase receipt downgrades the gate.
    assert gate.status == "FAIL"
    # Self-sufficient operator-evidence: the failing check + expected/actual.
    assert gate.detail is not None
    assert gate.detail.startswith("pipeline_import:")
    assert _EXPECTED in gate.detail
    assert _ACTUAL in gate.detail
    # The blocking residual carries the gate command.
    assert "env-provenance" in cockpit.residual_failed
    # require policy => required/blocking in the cockpit row.
    assert gate.required is True


@_skip_if_stale
@pytest.mark.acceptance
def test_timeline_slice_mirrors_provenance_fail_with_detail(
    fake_workspace: Path,
) -> None:
    from orcho_mcp.inspection.evidence import inspect_run_evidence

    run_id = _setup_run(fake_workspace)

    result = inspect_run_evidence(run_id, slice="verification_timeline")
    timeline = result.verification_timeline
    assert timeline is not None

    gate = next(g for g in timeline.gates if g.command == "env-provenance")
    assert gate.status == "FAIL"
    assert gate.detail is not None
    assert "pipeline_import" in gate.detail
    assert _EXPECTED in gate.detail
    assert "env-provenance" in timeline.residual_failed
    # A non-present required gate still carries its rerun hint.
    assert gate.rerun_hint
