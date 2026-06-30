"""Root pytest configuration for orcho-mcp tests.

Re-exports cross-layer fixtures from ``tests/fixtures/*`` so unit +
integration tests pick them up through pytest's normal conftest
discovery without any per-layer boilerplate. Mirrors the orcho-core
pattern (``orcho-core/tests/conftest.py``) where shared fixtures live
in one root conftest and per-layer subconftests stay scoped to that
layer's specifics (e.g. ``tests/acceptance/conftest.py`` for the L4
mock-pipeline fixtures).

``pythonpath = ["."]`` in ``pyproject.toml`` makes the
``tests.fixtures.*`` namespace path resolvable. ``tests/`` and its
subdirs intentionally have no ``__init__.py`` to keep the layout as
pytest-discovered namespace packages and avoid shadowing the ``mcp``
SDK at import time inside test modules.

Before any core-touching fixture loads, :mod:`tests._core_source` pins
``sdk`` / ``pipeline`` / ``core`` to the orcho-core checkout under review:
an ``ORCHO_CORE_SRC`` override, else the active Orcho run's isolated
worktree (derived from ``ORCHO_RUNSPACE`` + ``ORCHO_RUN_ID``), else the
sibling ``../orcho-core`` dev checkout. This proves the companion against
the *current* repaired core — never a stale promoted stable install or a
clean sibling missing the under-review diff. See that module for the
top-level-``tests``-collision rationale.
"""
import pytest

from tests._core_source import pin_core_source

# Must run before importing fixtures or service modules so the selective core
# finder is authoritative for every subsequent ``sdk`` / ``pipeline`` import.
pin_core_source()

from tests.fixtures.mcp_workspace import (  # noqa: E402,F401
    fake_workspace,
    init_git_repo,
    write_run,
)


@pytest.fixture
def isolated_user_skills(tmp_path, monkeypatch):
    """Pin ``$HOME`` to an empty dir so skill discovery does not pick up the
    developer's real ``~/.agents/skills`` packages.

    ``pipeline.skills.discover_skills`` defaults ``home_dir`` to
    ``Path.home()`` and intentionally surfaces user-level skills. Tests that
    assert on project-scoped skill counts must therefore neutralise the
    user-level source to stay hermetic across machines.
    """
    home = tmp_path / "_isolated_home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


__all__ = ["fake_workspace", "init_git_repo", "write_run", "isolated_user_skills"]


def pytest_collection_modifyitems(config, items):
    """Attach routing markers from the test path for targeted debug loops."""
    from pathlib import Path

    import pytest

    root = config.rootpath
    for item in items:
        rel = Path(item.path).relative_to(root).as_posix()
        for mark in _markers_for_test_path(rel):
            item.add_marker(getattr(pytest.mark, mark))


def _markers_for_test_path(rel: str) -> set[str]:
    marks: set[str] = set()
    parts = rel.split("/")

    if len(parts) > 1 and parts[1] in {"unit", "integration", "acceptance"}:
        marks.add(parts[1])

    if "/integration/protocol/" in rel or "/prompts/" in rel or "schema" in rel:
        marks.add("mcp_protocol")
    if "/run_control/" in rel or "/acceptance/mock_pipeline/" in rel or "orcho_run" in rel:
        marks.add("mcp_run_control")
    if "/supervisor/" in rel or "spawn" in rel or "cancel" in rel or "resume" in rel:
        marks.add("mcp_supervisor")
    if "/observe/" in rel or "watch" in rel or "event_tail" in rel or "summary" in rel:
        marks.add("mcp_observe")
    if "/services/" in rel or "/workspace_state/" in rel or "/workflows/" in rel:
        marks.add("mcp_services")
    if "schema_snapshot" in rel or "schema" in rel:
        marks.add("mcp_schema")

    if _is_serial_test_path(rel):
        marks.add("serial")

    return marks


def _is_serial_test_path(rel: str) -> bool:
    serial_fragments = (
        "/acceptance/",
        "/integration/protocol/",
        "/run_control/",
        "/supervisor/",
        "/observe/",
        "stdio",
        "subprocess",
        "watch",
        "spawn",
        "cancel",
        "resume",
        "typed_pilot",
        "event_tail",
    )
    return any(fragment in rel for fragment in serial_fragments)
