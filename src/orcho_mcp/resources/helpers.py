"""orcho_mcp.resources.helpers — encoding utilities and private serializer.

Leaf module: depends only on stdlib. Domain submodules
(``workspace``, ``runs``, ``profiles``, ``projects``) import ``_dump``
from here for stable JSON serialization, and the URI-segment encoders
are re-exported from the package ``__init__`` for client use.
"""
from __future__ import annotations

import base64
import json


def encode_project_dir(project_dir: str) -> str:
    """URL-safe base64 of a project_dir, no ``=`` padding.

    Mirrors the encoding the resource handlers expect; clients constructing
    ``orcho://projects/{X}/skills`` URIs use this to derive ``X`` from the
    absolute path.
    """
    raw = project_dir.encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_project_dir(encoded: str) -> str:
    """Inverse of ``encode_project_dir``. Raises ValueError on malformed input."""
    # Restore stripped ``=`` padding to a multiple of 4.
    padded = encoded + "=" * (-len(encoded) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")


def _dump(obj) -> str:
    """JSON-serialise a Pydantic model or plain dict to a stable string.

    Private to the resources package. Each domain submodule imports this
    directly (``from orcho_mcp.resources.helpers import _dump``); the
    package ``__init__`` intentionally does NOT re-export it.
    """
    if hasattr(obj, "model_dump_json"):
        return obj.model_dump_json(indent=2)
    return json.dumps(obj, indent=2, ensure_ascii=False)


__all__ = ["decode_project_dir", "encode_project_dir"]
