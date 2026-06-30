"""orcho_mcp.resources.projects — ``orcho://projects/{project_b64}/skills`` resource.

One MCP resource exposing the per-project skill registry. Project path
is URL-safe base64-encoded into the URI segment; clients use the
re-exported ``encode_project_dir`` from the package root to construct
the URI.
"""
from __future__ import annotations

from orcho_mcp.instance import mcp
from orcho_mcp.resources.helpers import _dump, decode_project_dir
from orcho_mcp.services.read_queries import get_project_skills


@mcp.resource(
    "orcho://projects/{project_b64}/skills",
    name="orcho_project_skills",
    description="Specialist skill registry under <project>/.agent/multiagent/skills/. "
                "Project path is URL-safe base64 (see encode_project_dir).",
    mime_type="application/json",
)
def project_skills_resource(project_b64: str) -> str:
    try:
        project_dir = decode_project_dir(project_b64)
    except ValueError as e:
        raise ValueError(
            f"malformed project_b64 segment {project_b64!r}: {e}. "
            "Use orcho_mcp.resources.encode_project_dir to construct URIs."
        ) from e
    return _dump(get_project_skills(project_dir=project_dir))


__all__ = ["project_skills_resource"]
