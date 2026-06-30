"""Test-layout contract.

The unit-test tree must mirror the production tree. This guard is the
executable counterpart to ``docs/architecture/mcp_boundaries.md`` and
``tests/README.md`` — moving production code without moving its tests
should fail CI, not slip past review.

What is enforced:

- Retired flat layout (``tests/mcp/``) does not come back.
- No ``__init__.py`` under ``tests/`` (namespace-package discovery
  preserves the historical "do not shadow the ``mcp`` SDK" invariant).
- Every production domain in ``DOMAIN_TEST_MAP`` has its source
  directory under ``src/orcho_mcp/`` AND a matching populated test
  directory under ``tests/unit/``.
- Every flat module in ``FLAT_MODULE_TEST_MAP`` has its source file
  AND a matching populated test directory.
- The protocol / acceptance / fixtures top-level directories are
  present.
- ``tests/README.md`` mentions each mapped domain name so the prose
  table stays in sync.

What is NOT enforced (and why):

- Exact test counts per directory — too brittle to be useful.
- Exact ``tests/README.md`` wording — the prose can be updated freely
  as long as the domain names appear somewhere in the file.
- ``__pycache__`` directories — pytest creates them at runtime; they
  are not tracked.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = REPO_ROOT / "src" / "orcho_mcp"
TESTS_ROOT = REPO_ROOT / "tests"
UNIT_ROOT = TESTS_ROOT / "unit"

# Production sub-package → unit-test directory.
DOMAIN_TEST_MAP: dict[str, str] = {
    "authoring": "authoring",
    "inspection": "inspection",
    "observe": "observe",
    "resources": "resources",
    "run_control": "run_control",
    "services": "services",
    "supervisor": "supervisor",
}

# Production sub-packages intentionally NOT mapped to a unit-test
# directory, with the reason. Keep the rationale prose — anyone
# adding an entry here is making a "this domain is exempt from the
# mirror layout" call, and the next reviewer needs the justification.
#
# ``test_every_production_subdir_is_mapped_or_exempted`` enforces
# that every directory under ``src/orcho_mcp/`` is in DOMAIN_TEST_MAP
# OR here — exemption is explicit, not a silent gap.
DOMAIN_TEST_EXEMPTIONS: dict[str, str] = {
    "schemas": (
        "Pure Pydantic models, no business logic. The wire snapshot "
        "at ``docs/mcp_schema.json`` IS the contract test; round-trip "
        "is implicitly exercised by every tool test that returns a "
        "schema instance. Adding ``tests/unit/schemas/`` would be "
        "ceremony with no signal."
    ),
}

# Flat production module → unit-test directory.
# ``prompts.py`` and ``onboarding.py`` both map to ``prompts/`` because
# they are tested together as the prompt registration surface; that is
# intentional and not a duplicate.
FLAT_MODULE_TEST_MAP: dict[str, str] = {
    "client_interactions.py": "client",
    "prompts.py": "prompts",
    "onboarding.py": "prompts",
    "workflows.py": "workflows",
    "workspace_state.py": "workspace_state",
}

# Cross-cutting test surfaces that must exist for the layered layout
# to be intact. These are the entry points the test pyramid bottoms
# out at — protocol tests for L2/L3, acceptance for L4, fixtures for
# the shared synthetic workspace + stdio plumbing.
REQUIRED_TOPLEVEL_DIRS: tuple[str, ...] = (
    "tests/integration/protocol",
    "tests/acceptance/mock_pipeline",
    "tests/fixtures",
)


def _has_test_files(directory: Path) -> bool:
    """At least one ``test_*.py`` exists directly under ``directory``."""
    if not directory.is_dir():
        return False
    return any(p.name.startswith("test_") and p.suffix == ".py"
               for p in directory.iterdir())


def test_retired_flat_test_dir_is_gone() -> None:
    """The flat ``tests/mcp/`` layout was retired during the layered
    test refactor. It must not come back — every test belongs in
    ``tests/unit/<domain>/``, ``tests/integration/``, or
    ``tests/acceptance/``.
    """
    flat = TESTS_ROOT / "mcp"
    assert not flat.exists(), (
        f"{flat} exists; the flat layout was retired. Move its "
        "contents under tests/unit/<domain>/, tests/integration/, or "
        "tests/acceptance/ and update docs/architecture/mcp_boundaries.md."
    )


def test_no_init_files_under_tests() -> None:
    """Pytest uses namespace-package discovery for the test tree.
    Adding ``__init__.py`` under ``tests/`` shadows the ``mcp`` SDK
    package at import time inside test modules — a real, previously
    fixed bug. Keep the tree namespace-only.
    """
    offenders = list(TESTS_ROOT.rglob("__init__.py"))
    assert not offenders, (
        "Found __init__.py under tests/ — this re-introduces the "
        "package-shadow bug that namespace discovery is designed to "
        "avoid:\n  " + "\n  ".join(str(p) for p in offenders)
    )


def test_production_domain_to_test_mapping_holds() -> None:
    """Every production sub-package has a populated unit-test home."""
    missing: list[str] = []
    for src_name, test_name in DOMAIN_TEST_MAP.items():
        src_dir = SRC_ROOT / src_name
        test_dir = UNIT_ROOT / test_name
        if not src_dir.is_dir():
            missing.append(f"src missing: {src_dir}")
            continue
        if not test_dir.is_dir():
            missing.append(f"test dir missing: {test_dir}")
            continue
        if not _has_test_files(test_dir):
            missing.append(f"test dir has no test_*.py: {test_dir}")
    assert not missing, (
        "Production ↔ test domain mapping broken:\n  "
        + "\n  ".join(missing)
        + "\nUpdate docs/architecture/mcp_boundaries.md and "
        "tests/README.md if the domain set has changed intentionally."
    )


def test_flat_module_to_test_mapping_holds() -> None:
    """Every flat production module has a populated unit-test home."""
    missing: list[str] = []
    for src_name, test_name in FLAT_MODULE_TEST_MAP.items():
        src_file = SRC_ROOT / src_name
        test_dir = UNIT_ROOT / test_name
        if not src_file.is_file():
            missing.append(f"src missing: {src_file}")
            continue
        if not test_dir.is_dir():
            missing.append(f"test dir missing: {test_dir}")
            continue
        if not _has_test_files(test_dir):
            missing.append(f"test dir has no test_*.py: {test_dir}")
    assert not missing, (
        "Flat module ↔ test mapping broken:\n  "
        + "\n  ".join(missing)
    )


def test_every_production_subdir_is_mapped_or_exempted() -> None:
    """No silent gaps in the production ↔ test mapping.

    When a new sub-package lands under ``src/orcho_mcp/``, the author
    has to make an explicit call: either it goes into
    ``DOMAIN_TEST_MAP`` (mirror layout, populated test directory
    required) or into ``DOMAIN_TEST_EXEMPTIONS`` (with a written
    reason). Anything else is a drift this test catches.

    Directories starting with ``_`` or ``__`` are skipped — those are
    reserved for non-production artefacts (``_onboarding/`` markdown
    only, ``__pycache__/``).
    """
    if not SRC_ROOT.is_dir():
        pytest.fail(f"src tree missing at {SRC_ROOT}")

    subdirs = sorted(
        p.name for p in SRC_ROOT.iterdir()
        if p.is_dir() and not p.name.startswith("_")
    )

    mapped = set(DOMAIN_TEST_MAP)
    exempt = set(DOMAIN_TEST_EXEMPTIONS)
    overlap = mapped & exempt
    assert not overlap, (
        "A sub-package is BOTH mapped and exempted — pick one:\n  "
        + ", ".join(sorted(overlap))
    )

    unaccounted = [d for d in subdirs if d not in mapped and d not in exempt]
    assert not unaccounted, (
        "Production sub-package(s) not in DOMAIN_TEST_MAP or "
        "DOMAIN_TEST_EXEMPTIONS:\n  "
        + "\n  ".join(unaccounted)
        + "\nAdd to one or the other in "
          "tests/unit/architecture/test_test_layout_contract.py. "
          "Exemptions require a written reason."
    )


def test_required_toplevel_test_dirs_present() -> None:
    """Protocol / acceptance / fixtures roots must exist — they are
    the entry points for L2, L3, L4 and the shared fixture surface.
    """
    missing = [
        rel for rel in REQUIRED_TOPLEVEL_DIRS
        if not (REPO_ROOT / rel).is_dir()
    ]
    assert not missing, (
        "Missing required top-level test directories:\n  "
        + "\n  ".join(missing)
    )


def test_tests_readme_mentions_each_mapped_domain() -> None:
    """``tests/README.md`` is the operator-facing version of the
    domain mapping table. Every mapped domain name must appear in it
    so prose and contract stay aligned. The check is loose — we look
    for the domain name as a substring anywhere in the file — so prose
    can be reworded freely without breaking the gate.
    """
    readme = TESTS_ROOT / "README.md"
    assert readme.is_file(), f"missing {readme}"
    text = readme.read_text(encoding="utf-8")

    expected_names = set(DOMAIN_TEST_MAP.values()) | set(FLAT_MODULE_TEST_MAP.values())
    # ``architecture`` is cross-cutting — not in the per-domain map but
    # documented in the same README. Require it explicitly.
    expected_names.add("architecture")

    missing = sorted(name for name in expected_names if name not in text)
    assert not missing, (
        f"tests/README.md does not mention these mapped names: {missing}. "
        "Update the README table when the domain set changes."
    )
