"""Profile *selectors* in the catalogue (T3).

``auto-detect`` is a profile *selector* — a ``profile`` value core resolves
into a semantic profile before profile resolution — NOT an executable recipe.
``orcho_profiles_list`` must surface it in a dedicated ``selectors`` field,
disjoint from the executable ``profiles`` list, and from a single core-sourced
token. Selectors are catalogue-independent, so they appear even when the v2
file is missing.
"""
from __future__ import annotations

from orcho_mcp.services import read_queries as rq
from orcho_mcp.services.read_queries import (
    _AUTO_DETECT_PROFILE_TOKEN,
    _profile_selectors,
    get_profiles_list,
)

SELECTOR = _AUTO_DETECT_PROFILE_TOKEN


def test_selectors_contains_auto_detect():
    r = get_profiles_list()
    names = [s.name for s in r.selectors]
    assert SELECTOR in names
    sel = next(s for s in r.selectors if s.name == SELECTOR)
    assert sel.is_selector is True
    assert sel.description  # non-empty one-liner


def test_auto_detect_absent_from_executable_profiles():
    r = get_profiles_list()
    profile_names = {p.name for p in r.profiles}
    assert SELECTOR not in profile_names
    # And the converse: the selector token is not duplicated as a recipe.
    selector_names = {s.name for s in r.selectors}
    assert profile_names.isdisjoint(selector_names)


def test_selectors_present_when_source_missing(monkeypatch, tmp_path):
    """Selectors do not depend on the v2 catalogue file — a ``missing``
    source still returns the ``auto-detect`` selector (profiles stay empty)."""
    nowhere = tmp_path / "does-not-exist.json"
    monkeypatch.setenv("ORCHO_PROFILES_V2_PATH", str(nowhere))
    r = get_profiles_list()
    assert r.source == "missing"
    assert r.profiles == []
    assert [s.name for s in r.selectors] == [SELECTOR]


def test_selector_name_sourced_from_single_core_token(monkeypatch):
    """The selector name comes from the one module-level token constant.

    The constant is the defensive import target (``try: import core const /
    except ImportError: 'auto-detect'``), so patching it here proves the
    selector name follows whatever that single source resolves to — i.e. the
    stale-core literal fallback would propagate identically.
    """
    monkeypatch.setattr(rq, "_AUTO_DETECT_PROFILE_TOKEN", "auto-detect-xyz")
    sels = _profile_selectors()
    assert [s.name for s in sels] == ["auto-detect-xyz"]


def test_selector_token_default_is_literal_fallback_value():
    # The real token resolves to the documented literal whether sourced from
    # core or the ImportError fallback — pin it so a drift is caught.
    assert SELECTOR == "auto-detect"
