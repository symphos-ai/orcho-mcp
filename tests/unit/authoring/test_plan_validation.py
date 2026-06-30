"""Unit tests for the ``orcho_plan_validate`` MCP tool.

Wraps ``pipeline.plan_parser.parse_plan``. Returns ``ok=False`` with a
human-readable error on parse / DAG failures; ``InvalidPlanError`` is
reserved for invalid arguments.
"""
from __future__ import annotations

import pytest

from orcho_mcp.errors import InvalidPlanError
from orcho_mcp.tools import orcho_plan_validate

_VALID_JSON_PLAN = """
Some preamble.

```json
{
  "short_summary": "do the thing",
  "planning_context": "do the thing with two ordered tasks",
  "tasks": [
    {"id": "t1", "goal": "do step 1", "depends_on": []},
    {"id": "t2", "goal": "do step 2", "depends_on": ["t1"]}
  ]
}
```
"""


def test_plan_validate_ok_json_fence():
    r = orcho_plan_validate(markdown=_VALID_JSON_PLAN)
    assert r.ok is True
    assert r.source == "json"
    assert r.short_summary == "do the thing"
    assert r.planning_context == "do the thing with two ordered tasks"
    assert [t.id for t in r.subtasks] == ["t1", "t2"]
    assert r.subtasks[1].depends_on == ["t1"]


def test_plan_validate_returns_error_on_cycle():
    bad = """
```json
{
  "short_summary": "cyclic",
  "planning_context": "cyclic plan context",
  "tasks": [
    {"id": "a", "goal": "x", "depends_on": ["b"]},
    {"id": "b", "goal": "y", "depends_on": ["a"]}
  ]
}
```
"""
    r = orcho_plan_validate(markdown=bad)
    assert r.ok is False
    assert r.error is not None and "cycle" in r.error.lower()


def test_plan_validate_requires_exactly_one_source():
    with pytest.raises(InvalidPlanError):
        orcho_plan_validate()
    with pytest.raises(InvalidPlanError):
        orcho_plan_validate(markdown="x", path="/tmp/y")


def test_plan_validate_path_input(tmp_path):
    p = tmp_path / "plan.md"
    p.write_text(_VALID_JSON_PLAN, encoding="utf-8")
    r = orcho_plan_validate(path=str(p))
    assert r.ok is True


def test_plan_validate_missing_path():
    with pytest.raises(InvalidPlanError):
        orcho_plan_validate(path="/nope/never.md")
