"""Supervisor-package boundary gate.

``orcho_mcp/supervisor/`` is split into a small set of single-purpose
modules. Each operation (spawn / resume / cancel / lifecycle reap /
recovery) lives in its own module as a top-level async function (or
sync, for recovery), and ``RunsSupervisor`` is a state holder that
exposes them as thin delegation methods. This gate keeps the layout
from regressing into a single mega-module and locks the composition
contract:

1. **Package shape** — the directory exists with the expected modules.
2. **Public surface stability** — ``orcho_mcp.supervisor`` re-exports
   exactly ``RunHandle``, ``RunsSupervisor``, ``get_supervisor`` (plus
   ``__all__``).
3. **Singleton location** — ``_singleton`` lives on the package
   ``__init__``. The acceptance L4 fixture resets it via
   ``orcho_mcp.supervisor._singleton = None``; if it migrated to
   ``manager.py``, the reset would silently no-op and per-test
   isolation would break.
4. **No mixin classes remain** — composition uses module-level
   functions, not inheritance.
5. **Operation modules export expected callables** — each operation
   module exposes its function via ``__all__`` with the exact
   expected name.
6. **RunsSupervisor has no mixin bases** —
   ``RunsSupervisor.__bases__ == (object,)`` after the migration.
7. **Method signatures are stable** — the public surface of
   ``RunsSupervisor.spawn`` / ``resume`` / ``cancel`` / ``_reap`` /
   ``recover`` is bit-identical to the pre-composition shape.
   ``inspect.signature(...)`` is part of the public contract: it
   shows up in IDE help, doc generators, and reflective tooling.
8. **Soft size tripwire** at ``MAX_LINES`` per module — warning only,
   so re-growing a module is a deliberate choice the diff makes
   visible.
"""
from __future__ import annotations

import ast
import inspect
import warnings
from pathlib import Path

import pytest

PKG_ROOT = Path(__file__).resolve().parents[3] / "src" / "orcho_mcp"
SUPERVISOR_ROOT = PKG_ROOT / "supervisor"

# Per-module soft cap. Any module growing past this is a hint that
# something domain-distinct is hiding inside it and should split off.
MAX_LINES = 320

EXPECTED_MODULES = {
    "__init__.py",
    "handle.py",
    "state.py",
    "paths.py",
    "process.py",
    "spawn.py",
    "resume.py",
    "cancel.py",
    "lifecycle.py",
    "recovery.py",
    "manager.py",
}

# Operation module → (expected callable name, expected ``__all__`` list).
# ``spawn`` / ``resume`` / ``cancel`` use the verb ``execute`` (uniform
# action entry); ``lifecycle`` and ``recovery`` use the named verb of
# their domain because they're the only operation in their module and
# the name carries semantic weight (``reap`` vs ``recover``).
OPERATION_MODULES: dict[str, tuple[str, list[str]]] = {
    "spawn.py":     ("execute", ["execute"]),
    "resume.py":    ("execute", ["execute"]),
    "cancel.py":    ("execute", ["execute"]),
    "lifecycle.py": ("reap",    ["reap"]),
    "recovery.py":  ("recover", ["recover"]),
}

# Exact signature strings of public ``RunsSupervisor`` methods. These
# are the strings ``str(inspect.signature(getattr(RunsSupervisor, name)))``
# produces. They were captured pre-migration and frozen here so the
# composition migration cannot silently drift the public method
# contract (parameter order, defaults, type hints, keyword-only
# markers).
#
# Update protocol: if a method's signature legitimately changes (new
# parameter added, deprecation, etc.), bump the expected string here
# in the same PR. The string MUST come from ``inspect.signature`` —
# do not hand-edit. Generate via:
#   python -c "import inspect, orcho_mcp.supervisor as s; \
#     print(repr(str(inspect.signature(s.RunsSupervisor.spawn))))"
EXPECTED_SIGNATURES: dict[str, str] = {
    "spawn": (
        "(self, *, task: 'str | None' = None, task_file: 'str | None' = None, "
        "project_dir: 'str', profile: 'str' = 'feature', mock: 'bool' = False, "
        "max_rounds: 'int | None' = None, mock_validate_plan_reject: 'int' = 0, "
        "output_mode: 'str' = 'summary', session_mode: 'str' = 'auto', "
        "progress_token: 'str | None' = None, "
        "attach: 'list[str] | None' = None, attach_text: 'list[str] | None' = None, "
        "attach_image: 'list[str] | None' = None, attach_binary: 'list[str] | None' = None, "
        "from_run_plan: 'str | None' = None) -> 'RunHandle'"
    ),
    "resume": "(self, run_id: 'str', *, profile: 'str | None' = None) -> 'RunHandle'",
    "cancel": "(self, run_id: 'str', mode: 'str' = 'graceful') -> 'dict[str, str]'",
    "_reap":  "(self, handle: 'RunHandle') -> 'None'",
    "recover": "(self) -> 'list[str]'",
}


def test_supervisor_is_a_package():
    """The split landed: ``supervisor/`` is a directory, not a file."""
    assert SUPERVISOR_ROOT.is_dir(), (
        f"expected {SUPERVISOR_ROOT} to be a package directory"
    )
    assert (SUPERVISOR_ROOT / "__init__.py").is_file()


def test_supervisor_expected_modules_present():
    """Catch accidental module deletions / renames."""
    present = {p.name for p in SUPERVISOR_ROOT.iterdir() if p.is_file()}
    missing = EXPECTED_MODULES - present
    assert not missing, f"supervisor package missing modules: {missing}"


def test_supervisor_public_surface_stable():
    """``__all__`` and the importable names must stay narrow.

    Tests, the FastMCP server, and the acceptance fixture all import
    from this surface; widening it accidentally couples downstream
    consumers to internals.
    """
    import orcho_mcp.supervisor as sup_pkg

    assert set(sup_pkg.__all__) == {"RunHandle", "RunsSupervisor", "get_supervisor"}
    # Each name actually resolves.
    assert sup_pkg.RunHandle is not None
    assert sup_pkg.RunsSupervisor is not None
    assert callable(sup_pkg.get_supervisor)


def test_supervisor_singleton_lives_on_package_init():
    """Acceptance L4 fixture writes ``_sup._singleton = None`` to reset
    the singleton between tests; that assignment lands on the
    package namespace. If the singleton migrated to ``manager.py``
    the reset would silently no-op.
    """
    import orcho_mcp.supervisor as sup_pkg

    assert hasattr(sup_pkg, "_singleton"), (
        "_singleton must live on orcho_mcp.supervisor (__init__.py); "
        "the acceptance fixture resets it there"
    )
    # And get_supervisor honours that reset round-trip.
    sup_pkg._singleton = None
    first = sup_pkg.get_supervisor()
    sup_pkg._singleton = None
    second = sup_pkg.get_supervisor()
    assert first is not second, (
        "get_supervisor must build a fresh instance after _singleton reset"
    )


def test_no_mixin_classes_remain():
    """AST walk every supervisor module; no ``class XMixin:`` may remain.

    The composition migration removed every mixin class in favour of
    module-level functions. A regression that re-introduces a mixin
    silently bypasses the signature contract guarded below.
    """
    offenders: list[str] = []
    for path in sorted(SUPERVISOR_ROOT.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name.endswith("Mixin"):
                offenders.append(f"{path.name}:{node.lineno}: class {node.name}")
    assert not offenders, (
        "Mixin classes found in supervisor/. The package uses module-"
        "level function composition; re-introducing a mixin bypasses "
        "the signature contract:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize(
    ("module", "expected_callable", "expected_all"),
    [
        (mod, callable_name, all_list)
        for mod, (callable_name, all_list) in OPERATION_MODULES.items()
    ],
    ids=list(OPERATION_MODULES),
)
def test_operation_modules_export_expected_callable(
    module: str, expected_callable: str, expected_all: list[str],
) -> None:
    """Each operation module exports its function via ``__all__`` with
    the exact expected name. Guards against partial migrations where
    a callable is added but the old name lingers in ``__all__``, or
    where the callable is renamed without updating ``__all__``.
    """
    import importlib

    mod_path = f"orcho_mcp.supervisor.{module.removesuffix('.py')}"
    mod = importlib.import_module(mod_path)

    # Callable present + actually callable.
    assert hasattr(mod, expected_callable), (
        f"{mod_path}: expected callable `{expected_callable}` not found"
    )
    assert callable(getattr(mod, expected_callable))

    # ``__all__`` matches exactly.
    actual_all = list(getattr(mod, "__all__", []))
    assert actual_all == expected_all, (
        f"{mod_path}: __all__ drift. expected {expected_all!r}, got {actual_all!r}"
    )


def test_runs_supervisor_has_no_mixin_bases():
    """``RunsSupervisor.__bases__ == (object,)`` after the migration.

    Mixin composition is gone; the class is a plain state holder with
    delegation methods. Any base other than ``object`` means a mixin
    or other inheritance has crept back in.
    """
    from orcho_mcp.supervisor.manager import RunsSupervisor

    assert RunsSupervisor.__bases__ == (object,), (
        f"RunsSupervisor must inherit only from object after the "
        f"composition migration; got {RunsSupervisor.__bases__!r}"
    )


@pytest.mark.parametrize(
    ("method_name", "expected_sig"),
    list(EXPECTED_SIGNATURES.items()),
)
def test_runs_supervisor_signatures_stable(method_name: str, expected_sig: str) -> None:
    """``inspect.signature(RunsSupervisor.<method>)`` matches the
    frozen string. The signature is part of the public contract — it
    appears in IDE help, doc generators, and reflective tooling.
    ``**kw`` forwarding shortcuts in delegation methods are forbidden
    because they mutate the signature silently.

    All five public methods are guarded: external-facing operations
    (``spawn`` / ``resume`` / ``cancel`` / ``recover``) plus the
    private-but-test-called ``_reap``. Guarding the full set removes
    the human-judgment gap around which methods "matter" for
    consumers.
    """
    from orcho_mcp.supervisor.manager import RunsSupervisor

    actual = str(inspect.signature(getattr(RunsSupervisor, method_name)))
    assert actual == expected_sig, (
        f"RunsSupervisor.{method_name} signature drifted.\n"
        f"  expected: {expected_sig}\n"
        f"  actual:   {actual}\n"
        "Update EXPECTED_SIGNATURES in this file in the SAME PR that "
        "changes the method signature, with the new string from "
        "``str(inspect.signature(...))``."
    )


@pytest.mark.parametrize(
    "module",
    sorted(EXPECTED_MODULES),
)
def test_supervisor_module_soft_size_cap(module):
    path = SUPERVISOR_ROOT / module
    n = sum(1 for _ in path.open("r", encoding="utf-8"))
    if n > MAX_LINES:
        warnings.warn(
            f"{module} has {n} lines (> {MAX_LINES}); "
            "consider splitting further or raising MAX_LINES deliberately.",
            stacklevel=1,
        )
