"""Allow ``python -m orcho_mcp`` to start the server.

Console-script users hit ``orcho_mcp.server:main`` directly via the
setuptools entry-point stub, but ``python -m orcho_mcp`` is the
universal fallback that works without ``pip install`` happening to
register the script. Importantly, it goes through the canonical
``orcho_mcp.server`` import path — not ``__main__`` — so the FastMCP
instance loaded by ``tools.py`` (etc.) matches the one ``main()`` runs.
"""
from __future__ import annotations

import sys

from orcho_mcp.server import main

if __name__ == "__main__":
    sys.exit(main())
