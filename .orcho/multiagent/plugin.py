"""Project-local Orcho configuration for orcho-mcp development.

The verification contract is intentionally a fine-tuning layer, not an
onboarding requirement. It pins the recurring MCP/source-under-test invariant:
MCP checks must run the current MCP checkout while importing the workspace
orcho-core dependency, not a stale stable install.
"""

PLUGIN = {
    "name": "orcho-mcp",
    "language": "Python 3.12",
    "architecture": (
        "MCP server exposing Orcho run control, observation, resources, "
        "authoring helpers, and supervisor-backed lifecycle tools."
    ),
    "file_hints": [
        "src/orcho_mcp/",
        "tests/unit/",
        "tests/acceptance/mock_pipeline/",
        "docs/mcp_schema.json",
    ],
    "dependency_repos": {
        "orcho-core": {
            "path": "../orcho-core",
            "required": True,
        },
    },
    "work_mode": "pro",
    "verification_envs": {
        "mcp-local-core": {
            "python": "{project}/.venv/bin/python",
            "cwd": "{checkout}",
            "env": {
                "PYTHONPATH": "{checkout}/src:{checkout}",
            },
            "assertions": [
                {"file_exists": "{project}/.venv/bin/python"},
                {
                    "import": "pipeline",
                    "path_equals": "{dependency:orcho-core}/pipeline/__init__.py",
                },
                {
                    "import": "orcho_mcp",
                    "path_under": "{checkout}/src/orcho_mcp",
                },
                {"file_exists": "tests/fixtures/mcp_workspace.py"},
                {"version": ["python", "--version"], "contains": "Python 3.12"},
            ],
        },
    },
    "verification": {
        "default_env": "mcp-local-core",
        "delivery_policy": "warn",
        "required": [
            "env-provenance",
            "lint",
        ],
        "commands": {
            "env-provenance": {
                "env": "mcp-local-core",
                "cheap": True,
                "run": [
                    "python",
                    "-c",
                    (
                        "import pipeline, orcho_mcp; "
                        "print('pipeline', pipeline.__file__); "
                        "print('orcho_mcp', orcho_mcp.__file__)"
                    ),
                ],
            },
            "lint": {
                "env": "mcp-local-core",
                "cheap": True,
                "run": ["python", "-m", "ruff", "check", "."],
            },
            "run-control-unit": {
                "env": "mcp-local-core",
                "parity": "differential",
                "run": [
                    "python",
                    "-m",
                    "pytest",
                    "-q",
                    "tests/unit/run_control",
                    "tests/unit/observe",
                    "tests/unit/services",
                    "tests/unit/resources",
                ],
            },
            "mcp-mock-smoke": {
                "env": "mcp-local-core",
                "parity": "differential",
                "run": [
                    "python",
                    "-m",
                    "pytest",
                    "-q",
                    "tests/acceptance/mock_pipeline/test_smoke_matrix.py",
                    "-m",
                    "mcp_integration",
                    "-o",
                    "addopts=",
                ],
            },
        },
        "gate_sets": {
            "baseline": {
                "commands": ["env-provenance", "lint"],
                "default_policy": "warn",
                "default_cheap": True,
            },
            "mcp-runtime": {
                "commands": ["env-provenance", "lint", "run-control-unit"],
                "default_policy": "warn",
                "default_cheap": False,
            },
            "mcp-smoke": {
                "commands": ["mcp-mock-smoke"],
                "default_policy": "suggest",
                "default_cheap": False,
            },
        },
        "selection": [
            {"always": ["baseline"]},
            {
                "paths": [
                    "src/orcho_mcp/**",
                    "tests/unit/run_control/**",
                    "tests/unit/observe/**",
                    "tests/unit/services/**",
                    "tests/unit/resources/**",
                ],
                "include": ["mcp-runtime"],
            },
            {
                "paths": [
                    "tests/acceptance/mock_pipeline/**",
                    "docs/mcp_schema.json",
                ],
                "include": ["mcp-smoke"],
            },
        ],
        "schedule": [
            {
                "before_phase": "implement",
                "gate_sets": ["baseline"],
                "policy": "warn",
                "action": "continue_warn",
            },
            {
                "after_phase": "implement",
                "gate_sets": ["baseline", "mcp-runtime", "mcp-smoke"],
                "policy": "require",
                "action": "repair_loop",
            },
            {
                "before_phase": "final_acceptance",
                "gate_sets": ["baseline", "mcp-runtime"],
                "policy": "warn",
            },
            {
                "before_delivery": True,
                "gate_sets": ["baseline", "mcp-runtime", "mcp-smoke"],
                "policy": "warn",
                "action": "handoff",
            },
        ],
    },
}
