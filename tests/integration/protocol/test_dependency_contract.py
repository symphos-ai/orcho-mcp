from __future__ import annotations

import tomllib
from pathlib import Path


def test_mcp_sdk_stays_on_supported_major() -> None:
    pyproject = tomllib.loads(
        (Path(__file__).parents[3] / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert "mcp>=1.2,<2" in pyproject["project"]["dependencies"]
