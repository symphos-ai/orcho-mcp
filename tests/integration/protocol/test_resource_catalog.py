"""Resource catalog contract.

Freezes the set of MCP resource URIs and templates the server
publishes. Adds, removes, or renames of any URI must update the
expected set in this file in the same diff — making the catalog a
deliberate change, not a silent one.

This is the human-readable counterpart to ``docs/mcp_schema.json``
(which is the full wire snapshot). The schema snapshot answers
"what does the wire look like?"; this test answers "what URIs does
the server publish?" — at a glance, without diffing JSON.

Implementation note: we drive the catalog via
``orcho_mcp.discovery.collect_catalog()``. ``collect_catalog`` calls
``server._register_handlers()`` internally so every registration
path the live server runs (``resources/`` side-effect import,
``tools.py`` side-effect import, ``register_all_prompts()``,
``onboarding`` + ``workflows`` side-effect imports) executes
before the catalog is collected. A naive ``import orcho_mcp.resources``
would omit ``orcho://docs/getting-started`` (registered by
``orcho_mcp.onboarding``) and the test would falsely fail.

The runtime cost is small — ``collect_catalog`` runs in-process,
no subprocess, ~50ms. Lives under ``tests/integration/protocol/``
rather than ``tests/unit/`` because it exercises the FastMCP
list_resources / list_resource_templates surface, which is L2
territory.
"""
from __future__ import annotations

from orcho_mcp.discovery import collect_catalog

# Static resource URIs the server publishes.
EXPECTED_RESOURCES: frozenset[str] = frozenset({
    "orcho://workspace",
    "orcho://runs",
    "orcho://profiles",
    "orcho://workflows",
    "orcho://docs/getting-started",
})

# URI templates (parameterised resources). The exact placeholder
# syntax is from FastMCP's resource-template registration —
# ``{name}`` parts are literal placeholders, not Python format
# strings.
EXPECTED_RESOURCE_TEMPLATES: frozenset[str] = frozenset({
    "orcho://runs/{run_id}/meta",
    "orcho://runs/{run_id}/metrics",
    "orcho://runs/{run_id}/events",
    "orcho://runs/{run_id}/summary",
    "orcho://runs/{run_id}/parsed_plan.json",
    "orcho://runs/{run_id}/evidence",
    "orcho://runs/{run_id}/diff.patch",
    "orcho://runs/{run_id}/phases/{phase}/diff.patch",
    "orcho://profiles/{name}",
    "orcho://projects/{project_b64}/skills",
})


def _resource_uris(catalog: dict) -> set[str]:
    """Pull the URI set from the catalog dict."""
    return {entry["uri"] for entry in catalog["resources"]}


def _template_uris(catalog: dict) -> set[str]:
    """Pull the template-URI set from the catalog dict."""
    return {entry["uriTemplate"] for entry in catalog["resourceTemplates"]}


def test_static_resources_match_expected_set() -> None:
    """Every static resource URI is exactly the expected set.

    Failure modes:
    - Added URI: a new ``@mcp.resource`` registration appeared
      without updating ``EXPECTED_RESOURCES`` here. If the addition
      is intentional, add the URI to the expected set in the same
      diff; if accidental, remove the registration.
    - Removed URI: a registration disappeared. Same protocol —
      remove from expected if intentional, restore otherwise.
    - Renamed URI: surfaces as one add + one remove. Update both
      sides in the same diff.
    """
    catalog = collect_catalog()
    actual = _resource_uris(catalog)
    missing = EXPECTED_RESOURCES - actual
    unexpected = actual - EXPECTED_RESOURCES
    assert not missing and not unexpected, (
        "Resource catalog drift.\n"
        f"  missing (expected but not registered): {sorted(missing)}\n"
        f"  unexpected (registered but not in expected): {sorted(unexpected)}\n"
        "Update EXPECTED_RESOURCES in this file to match the new "
        "catalog in the SAME diff that changes the registrations."
    )


def test_resource_templates_match_expected_set() -> None:
    """Every parameterised resource template URI is exactly the
    expected set. Same failure protocol as the static-resources
    test above.
    """
    catalog = collect_catalog()
    actual = _template_uris(catalog)
    missing = EXPECTED_RESOURCE_TEMPLATES - actual
    unexpected = actual - EXPECTED_RESOURCE_TEMPLATES
    assert not missing and not unexpected, (
        "Resource template catalog drift.\n"
        f"  missing (expected but not registered): {sorted(missing)}\n"
        f"  unexpected (registered but not in expected): {sorted(unexpected)}\n"
        "Update EXPECTED_RESOURCE_TEMPLATES in this file to match "
        "the new catalog in the SAME diff that changes the registrations."
    )
