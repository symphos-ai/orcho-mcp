"""Smoke tests for package import, server registration, and --version.

Intentionally minimal. Other test files cover domain behaviour; this
file locks in the baseline so a broken scaffold gets caught immediately
on every pytest run.

Note: this test directory has no ``__init__.py`` on purpose — pytest picks
up test files via rootdir collection, and creating one would shadow the
``mcp`` SDK at import time inside the test module.
"""
from __future__ import annotations

import importlib
import importlib.metadata
from importlib.metadata import version

import pytest


def test_package_imports_clean():
    """Core modules import without side effects."""
    import orcho_mcp
    import orcho_mcp.errors
    import orcho_mcp.schemas
    import orcho_mcp.server

    assert orcho_mcp.__version__
    assert isinstance(orcho_mcp.__version__, str)
    assert orcho_mcp.__version__ == version("orcho-mcp")


def test_package_version_has_source_checkout_fallback(monkeypatch):
    """A source import without installed metadata remains diagnosable."""
    import orcho_mcp

    real_version = importlib.metadata.version

    def missing_metadata(_distribution: str) -> str:
        raise importlib.metadata.PackageNotFoundError

    monkeypatch.setattr(importlib.metadata, "version", missing_metadata)
    try:
        assert importlib.reload(orcho_mcp).__version__ == "0+unknown"
    finally:
        monkeypatch.setattr(importlib.metadata, "version", real_version)
        importlib.reload(orcho_mcp)


def test_server_instance_named_orcho():
    """FastMCP server is registered under the canonical name."""
    from orcho_mcp.server import mcp as server_instance

    assert server_instance.name == "orcho"


def test_main_handles_version_flag(capsys):
    """``orcho-mcp --version`` exits 0 with a version string on stdout."""
    from orcho_mcp.server import main

    with pytest.raises(SystemExit) as exc:
        main(["--version"])

    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == f"orcho-mcp {version('orcho-mcp')}"


def test_errors_hierarchy():
    """Domain errors all derive from OrchoMCPError so dispatch can catch broadly."""
    from orcho_mcp.errors import (
        InvalidPlanError,
        OrchoMCPError,
        PipelineSpawnError,
        RunNotFoundError,
        WorkspaceNotResolvedError,
    )

    for cls in (RunNotFoundError, WorkspaceNotResolvedError,
                PipelineSpawnError, InvalidPlanError):
        assert issubclass(cls, OrchoMCPError)
