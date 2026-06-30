"""orcho_mcp.resources.profiles — ``orcho://profiles[/...]`` resources.

Two MCP resources: the full v2 profile catalogue and per-name lookup.
Both pass through ``services.read_queries.get_profiles_list``.
"""
from __future__ import annotations

from orcho_mcp.instance import mcp
from orcho_mcp.resources.helpers import _dump
from orcho_mcp.services.read_queries import get_profiles_list


@mcp.resource(
    "orcho://profiles",
    name="orcho_profiles",
    description=(
        "Catalogue of pipeline profiles keyed by semantic work-kind "
        "(feature, small_task, planning, code_review, …) plus any "
        "custom/plugin profiles. Engine-internal profiles are included but "
        "flagged internal=true."
    ),
    mime_type="application/json",
)
def profiles_resource() -> str:
    return _dump(get_profiles_list())


@mcp.resource(
    "orcho://profiles/{name}",
    name="orcho_profile",
    description="A single pipeline profile by name.",
    mime_type="application/json",
)
def profile_resource(name: str) -> str:
    listing = get_profiles_list()
    for p in listing.profiles:
        if p.name == name:
            return _dump(p)
    raise KeyError(f"profile not found: {name}")


__all__ = ["profile_resource", "profiles_resource"]
