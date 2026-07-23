"""Pin orcho-core imports to a chosen checkout for the test session.

orcho-mcp's services exercise the *real* orcho-core surfaces — most notably
``sdk.delivery_decision_state`` — rather than fakes, so the gate/diagnosis/
live-status projections are proven against authoritative core behaviour. That
makes the suite sensitive to *which* orcho-core the interpreter resolves:

* the project ``.venv`` editable-installs the sibling ``../orcho-core`` checkout;
* a bare ``python`` from another environment (e.g. a conda base) can instead
  resolve an older *stable* install promoted under
  ``~/.local/share/orcho-core``.

When a cross-repo change touches both repos at once, the MCP suite must import
the orcho-core checkout *under review*, not whichever copy happens to be
installed. A naive ``PYTHONPATH=<core>`` prepend does not work: the orcho-core
checkout has its own top-level ``tests`` package, which would shadow
orcho-mcp's ``tests.fixtures`` helpers and break collection.

This module installs a selective :class:`importlib.abc.MetaPathFinder` that
maps *only* orcho-core's top-level packages to a chosen checkout root, ahead of
any editable/stable finder. It never exposes the checkout's ``tests`` package,
so orcho-mcp's own ``tests.*`` namespace is untouched.

Source precedence (first match wins):

1. ``ORCHO_CORE_SRC`` — explicit override. Point it at any orcho-core checkout
   to prove the companion against exactly that core slice.
2. The active Orcho-managed run's isolated worktree checkout, derived from the
   run environment (``ORCHO_RUNSPACE`` + ``ORCHO_RUN_ID``) — never a hardcoded
   run id. During an Orcho review the repaired-but-undelivered sources live
   ONLY in that per-run worktree, so a bare ``python -m pytest`` must validate
   the companion against it, not the clean sibling. Selected only when that
   worktree is itself an orcho-core checkout (an Orcho run targeting another
   repo leaves it absent / non-core and this candidate is skipped).
3. The sibling ``../orcho-core`` dev checkout next to this repo — the standard
   editable cross-repo layout for normal local runs, preferred over a stale
   promoted stable install so a bare ``python -m pytest`` still exercises the
   development core.
4. The paired ``wt_core/checkout`` when this MCP checkout itself is running
   from an Orcho worktree. This keeps the provenance gate valid for a
   cross-repo run even when ``ORCHO_RUN_ID`` is not exported to its verifier.

If none resolves to a real checkout the module is inert and imports fall
through to whatever is installed.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from importlib.machinery import ModuleSpec
from pathlib import Path

# orcho-core's public top-level packages. Deliberately excludes ``tests`` so we
# never shadow orcho-mcp's own ``tests.fixtures`` helpers (the reason a plain
# ``PYTHONPATH`` prepend is unsafe).
_CORE_TOP_LEVEL: tuple[str, ...] = ("sdk", "pipeline", "core", "agents", "cli")


class _OrchoCoreSrcFinder:
    """Resolve orcho-core top-level packages to a fixed checkout root.

    Only the top-level package import is intercepted; once the package spec
    carries the correct ``submodule_search_locations`` the default import
    machinery resolves every submodule from the same checkout.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    def find_spec(self, fullname: str, path=None, target=None) -> ModuleSpec | None:
        if fullname not in _CORE_TOP_LEVEL:
            return None
        pkg_dir = self._root / fullname
        init = pkg_dir / "__init__.py"
        if not init.is_file():
            return None
        return importlib.util.spec_from_file_location(
            fullname, init, submodule_search_locations=[str(pkg_dir)]
        )


def _orcho_run_worktree() -> Path | None:
    """Derive the active Orcho run's isolated core checkout from the run env.

    An Orcho-managed run executes inside a per-run worktree whose checkout holds
    the repaired-but-undelivered sources; the sibling dev checkout stays clean
    until delivery applies the diff. The path is derived from ``ORCHO_RUNSPACE``
    + ``ORCHO_RUN_ID`` (canonical layout ``worktrees/wt_<run_id>/checkout``,
    with a run-id glob as a prefix-tolerant fallback) — never a hardcoded run
    id. Returns the path only when it is itself an orcho-core checkout; a run
    targeting a different repo leaves it non-core and yields ``None``.
    """
    runspace = os.environ.get("ORCHO_RUNSPACE", "").strip()
    run_id = os.environ.get("ORCHO_RUN_ID", "").strip()
    if not runspace or not run_id:
        return None
    worktrees = Path(runspace).expanduser() / "worktrees"
    candidates = [worktrees / f"wt_{run_id}" / "checkout"]
    candidates.extend(sorted(worktrees.glob(f"*{run_id}*/checkout")))
    for cand in candidates:
        if _is_core_checkout(cand):
            return cand
    return None


def _candidate_roots() -> list[Path]:
    roots: list[Path] = []
    env = os.environ.get("ORCHO_CORE_SRC", "").strip()
    if env:
        roots.append(Path(env).expanduser())
    # Active Orcho-managed run: the repaired core lives in the per-run worktree,
    # which must win over the (clean) sibling dev checkout below.
    run_worktree = _orcho_run_worktree()
    if run_worktree is not None:
        roots.append(run_worktree)
    # A cross-repo Orcho worktree is laid out as
    # ``.../worktrees/wt_mcp/checkout`` beside ``wt_core/checkout``.  The
    # verification process may not inherit ORCHO_RUN_ID, so derive this
    # co-located companion from this file rather than falling through to an
    # installed package.  The candidate is still accepted only after the same
    # checkout-shape validation below.
    checkout = Path(__file__).resolve().parents[1]
    if checkout.name == "checkout" and checkout.parent.name.startswith("wt_"):
        roots.append(checkout.parent.parent / "wt_core" / "checkout")
    # A runspace checkout is nested below the repository workspace rather than
    # directly beside its companion.  Walk only its ancestors and retain the
    # same checkout-shape validation below; this finds
    # ``<workspace>/orcho-core`` without ever selecting an installed package.
    roots.extend(parent / "orcho-core" for parent in checkout.parents)
    # Sibling dev checkout: ``.../orcho-mcp`` → ``.../orcho-core``.
    roots.append(checkout.parent / "orcho-core")
    return roots


def _is_core_checkout(root: Path) -> bool:
    return (root / "sdk" / "__init__.py").is_file() and (
        root / "pipeline" / "__init__.py"
    ).is_file()


def _evict_loaded_core() -> None:
    """Drop any already-imported core modules so the finder is authoritative.

    A stale ``sdk`` / ``pipeline`` cached in ``sys.modules`` (imported before
    this finder was installed) would otherwise win over the finder. Eviction is
    safe at conftest import time: no test has bound a core symbol yet.
    """
    for name in list(sys.modules):
        top = name.split(".", 1)[0]
        if top in _CORE_TOP_LEVEL:
            del sys.modules[name]


def pin_core_source() -> Path | None:
    """Activate the selective core finder for the first resolvable root.

    Returns the chosen checkout root, or ``None`` when no override applies and
    imports should fall through to the installed core. Idempotent.
    """
    for root in _candidate_roots():
        root = root.resolve()
        if not _is_core_checkout(root):
            continue
        already = any(isinstance(f, _OrchoCoreSrcFinder) and f._root == root for f in sys.meta_path)
        if already:
            return root
        # Remove any prior instance pointing elsewhere, then take precedence.
        sys.meta_path[:] = [f for f in sys.meta_path if not isinstance(f, _OrchoCoreSrcFinder)]
        _evict_loaded_core()
        sys.meta_path.insert(0, _OrchoCoreSrcFinder(root))
        importlib.invalidate_caches()
        # Record the resolved root so any out-of-process consumer (e.g. the child
        # pipeline subprocess in an mcp_integration smoke) can opt to honour the
        # same checkout via env rather than re-deriving it.
        os.environ.setdefault("ORCHO_CORE_SRC", str(root))
        return root
    return None
