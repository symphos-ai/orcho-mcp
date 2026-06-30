"""Public text hygiene contract.

Catches stale internal-process wording and retired paths in this
repo's public-ish text. The goal is to keep docs / docstrings /
comments current-fact instead of letting them drift into history that
will not help readers a year from now.

Three scopes:

1. **Retired path references** — old layout strings that must stay
   buried (``tests/mcp``, ``orcho_mcp/resources.py``, etc.).
2. **Internal process markers** — PR numbers, REA tags, ADR refs, and
   one-off phase codes that read fine in a commit message but turn
   into noise inside long-lived prose.
3. **Public-boundary banned terms** — the same list enforced at edit
   time by the workspace's ``orcho-public-boundary`` skill, now
   materialised as a gate so a stray mention in docs or docstrings
   blocks the build instead of waiting for a reviewer to spot it.

Intentional carve-outs:

- ``docs/mcp_schema.json`` is a generated wire snapshot and is never
  scanned.
- This file and ``test_test_layout_contract.py`` describe the
  patterns they enforce — they are skipped explicitly via
  ``SKIP_FILES``.
- The word ``legacy`` is intentionally NOT on the banned list yet:
  legitimate technical uses exist ("stale lock", "compatibility
  protocols"), and the current tree is clean of stale-history uses.
  Tighten when (and if) it regresses.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

# Files / directories scanned for hygiene. Each entry can be a single
# file or a directory; directories are walked recursively.
SCAN_ROOTS: tuple[str, ...] = (
    "AGENTS.md",
    "CLAUDE.md",
    "README.md",
    "docs",
    "src/orcho_mcp",
    "tests",
    "pyproject.toml",
    "tools",
)

# Files that mention the forbidden patterns by necessity (they ARE the
# guards). Anything added here needs a one-line justification.
SKIP_FILES: set[Path] = {
    # This file — describes the patterns it enforces.
    Path(__file__).resolve(),
    # Layout contract — names ``tests/mcp`` as the retired layout to
    # forbid its return.
    (Path(__file__).resolve().parent / "test_test_layout_contract.py"),
}

# Path fragments that are generated, cache-only, or otherwise off-limits.
SKIP_PATH_FRAGMENTS: tuple[str, ...] = (
    "docs/mcp_schema.json",
    "__pycache__",
    ".pytest_cache",
    ".egg-info",
)

# Only scan text-ish files. Binary blobs and lock files are skipped
# implicitly by suffix.
TEXT_SUFFIXES: frozenset[str] = frozenset({".md", ".py", ".toml", ".txt"})

# Retired layout paths. Each former monolith was split into its own
# sub-package; references to the old ``.py`` file mislead grep,
# onboarding, and search. Add a new pair (``orcho_mcp/X.py`` +
# ``src/orcho_mcp/X.py``) whenever a flat module gets promoted into
# a sub-package.
RETIRED_PATHS = re.compile(
    r"tests/mcp\b"
    r"|orcho_mcp/resources\.py"
    r"|orcho_mcp/supervisor\.py"
    r"|orcho_mcp/schemas\.py"
    r"|orcho-mcp/orcho_mcp"
    r"|src/orcho_mcp/resources\.py"
    r"|src/orcho_mcp/supervisor\.py"
    r"|src/orcho_mcp/schemas\.py"
)

# Internal process markers. ``phase`` alone is a legitimate product
# concept (pipeline phases, phase handoff) — only the specific stale
# code ``phase5c`` is banned. ``\bADR \d+\b`` catches "ADR 0037"
# without firing on the word "adrenaline" or similar.
PROCESS_MARKERS = re.compile(
    r"\bPR\d+\b"
    r"|\bREA-\d+\b"
    r"|\bADR \d+\b"
    r"|\bphase5c\b"
    r"|\bold module-local\b"
    r"|\bretired tests/mcp\b"
)

# Public-boundary blocked vocabulary, case-insensitive. Build the
# pattern from split strings so broader cross-repo text scanners don't
# flag this guard file for naming the vocabulary it enforces.
_BOUNDARY_TERMS = (
    "desk" "top",
    "orcho-" "desk" "top",
    "py" "webview",
    "com" "mercial",
    "pro" "prietary",
    "pa" "id",
    "pre" "mium",
    "license-" "gate",
    "enterprise" " tier",
)
BANNED_TERMS = re.compile(
    "|".join(rf"\b{re.escape(term)}\b" for term in _BOUNDARY_TERMS),
    re.IGNORECASE,
)


def _candidate_files() -> list[Path]:
    """Materialise the list of in-scope text files."""
    out: list[Path] = []
    for rel in SCAN_ROOTS:
        root = REPO_ROOT / rel
        if not root.exists():
            continue
        candidates = [root] if root.is_file() else [
            p for p in root.rglob("*") if p.is_file()
        ]
        for p in candidates:
            rel_str = p.relative_to(REPO_ROOT).as_posix()
            if any(frag in rel_str for frag in SKIP_PATH_FRAGMENTS):
                continue
            if p.suffix not in TEXT_SUFFIXES:
                continue
            if p.resolve() in SKIP_FILES:
                continue
            out.append(p)
    return out


def _scan(pattern: re.Pattern[str]) -> list[str]:
    """Return ``"<rel-path>:<line>: <text>"`` for every matching line."""
    hits: list[str] = []
    for path in _candidate_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        for i, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                hits.append(f"{rel}:{i}: {line.strip()}")
    return hits


def test_scan_has_files_to_inspect() -> None:
    """Empty scan would make every other assertion pass trivially.
    Catch a misconfigured ``SCAN_ROOTS`` early.
    """
    assert _candidate_files(), (
        "no candidate files found — SCAN_ROOTS misconfigured?"
    )


def test_no_retired_path_references() -> None:
    """Old layout paths must not appear in public-ish text — they
    mislead grep and onboarding.
    """
    hits = _scan(RETIRED_PATHS)
    assert not hits, (
        "Retired path references found:\n  " + "\n  ".join(hits)
        + "\nRewrite using current paths (tests/unit/<domain>/, "
          "src/orcho_mcp/<domain>/, …)."
    )


def test_no_internal_process_markers() -> None:
    """PR numbers, REA tags, ADR refs, and one-off phase codes belong
    in commits and changelogs, not in long-lived prose.
    """
    hits = _scan(PROCESS_MARKERS)
    assert not hits, (
        "Internal process markers found:\n  " + "\n  ".join(hits)
        + "\nDrop PR / REA / ADR / phase tags from docs and docstrings; "
          "keep narrative text current-fact."
    )


def test_no_banned_public_boundary_terms() -> None:
    """Public-side text must not mention the open-core boundary or
    closed-surface framing. Same rule as the workspace's
    ``orcho-public-boundary`` skill, enforced here as a gate.
    """
    hits = _scan(BANNED_TERMS)
    assert not hits, (
        "Banned public-boundary terms found:\n  " + "\n  ".join(hits)
        + "\nThis repo is Apache-2.0 public — reword without the banned "
          "terms. Open-core boundary discussion belongs in non-public repos."
    )
