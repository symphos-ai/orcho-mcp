"""Byte-exact wire snapshot for the profile catalogue (baseline lock).

Freezes the JSON that ``orcho_profiles_list`` and the ``orcho://profiles``
resource emit for the shipped v2 catalogue, BEFORE the read-layer refactor
that swaps the backing from ``pipeline.profiles.loader.load_profiles_v2`` to
``sdk.profiles.list_profiles``. Both the tool backing
(``services.read_queries.get_profiles_list``) and the resource adapter
(``resources.profiles.profiles_resource``) go through the same ``_dump``
serializer, so this test pins that both keep producing byte-identical output.

The golden fixture is captured on the shipped catalogue (``source='json_v2'``).
If the catalogue is absent from the environment the tool falls back to
``source='missing'`` with an environment-specific diagnostic path that cannot
be snapshotted portably; the test then skips rather than asserting against a
non-representative baseline.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from orcho_mcp.resources.helpers import _dump
from orcho_mcp.resources.profiles import profiles_resource
from orcho_mcp.services.read_queries import get_profiles_list

_GOLDEN = Path(__file__).parent / "fixtures" / "profiles_wire_golden.json"


def _golden_text() -> str:
    # Read as raw text with no newline translation so the comparison is
    # byte-exact against ``_dump`` output (``model_dump_json(indent=2)``,
    # no trailing newline).
    return _GOLDEN.read_text(encoding="utf-8")


def _require_shipped_catalogue() -> None:
    if get_profiles_list().source != "json_v2":
        pytest.skip(
            "v2 profile catalogue not present in this environment; "
            "baseline snapshot is captured on source='json_v2'.",
        )


def test_tool_backing_matches_golden():
    """``get_profiles_list()`` serialized == the frozen wire bytes."""
    _require_shipped_catalogue()
    assert _dump(get_profiles_list()) == _golden_text()


def test_profiles_resource_matches_golden():
    """The ``orcho://profiles`` resource == the frozen wire bytes."""
    _require_shipped_catalogue()
    assert profiles_resource() == _golden_text()


def test_tool_and_resource_agree():
    """Tool backing and resource adapter emit identical bytes.

    Structural cross-check independent of the golden file: both adapters are
    thin wrappers over the same serializer, so their output must never drift
    from each other regardless of catalogue contents.
    """
    assert _dump(get_profiles_list()) == profiles_resource()
