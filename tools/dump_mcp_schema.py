#!/usr/bin/env python3
"""tools/dump_mcp_schema.py — write the orcho-mcp catalog to docs/mcp_schema.json.

Run after any change that affects the MCP surface (new/removed/renamed
tool, resource, or prompt; Pydantic model adjustment that changes
JSON Schema). Commit the resulting docs/mcp_schema.json as part of the
same change — PR reviewers see the public-surface diff alongside the
implementation.

Usage:
    python tools/dump_mcp_schema.py            # writes docs/mcp_schema.json
    python tools/dump_mcp_schema.py --check    # exits non-zero if file would change

The committed file is the «poor man's Swagger doc» for orcho-mcp:
machine-readable, human-reviewable, no server spin-up needed.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Make THIS checkout's ``orcho_mcp`` importable when run standalone. The
# package lives under ``src/`` (``[tool.setuptools.packages.find] where =
# ["src"]``), so the repo root alone does not expose it — without ``src`` on
# ``sys.path`` a bare ``python tools/dump_mcp_schema.py`` would import a stale
# site-packages / editable install instead and dump the wrong surface. Prepend
# both so the checkout's models always win over any installed copy (mirrors the
# ``pythonpath = ["src", "."]`` pytest config).
_REPO_ROOT = Path(__file__).parent.parent.resolve()
for _path in (str(_REPO_ROOT), str(_REPO_ROOT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from orcho_mcp.discovery import collect_catalog  # noqa: E402

_OUTPUT = _REPO_ROOT / "docs" / "mcp_schema.json"


def _serialize(catalog: dict) -> str:
    """Stable JSON: 2-space indent, sorted keys, trailing newline."""
    return json.dumps(catalog, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true",
        help="Exit non-zero if the on-disk file would change (CI guard).",
    )
    args = parser.parse_args(argv)

    catalog = collect_catalog()
    new_text = _serialize(catalog)

    if args.check:
        existing = _OUTPUT.read_text(encoding="utf-8") if _OUTPUT.is_file() else ""
        if existing != new_text:
            print(
                "docs/mcp_schema.json is stale.\n"
                "  Run: python tools/dump_mcp_schema.py",
                file=sys.stderr,
            )
            return 1
        return 0

    _OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    _OUTPUT.write_text(new_text, encoding="utf-8")
    print(f"wrote {_OUTPUT.relative_to(_REPO_ROOT)}: "
          f"{len(catalog['tools'])} tools, "
          f"{len(catalog['resources'])} resources, "
          f"{len(catalog['resourceTemplates'])} templates, "
          f"{len(catalog['prompts'])} prompts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
