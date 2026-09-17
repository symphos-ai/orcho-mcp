"""Criterion-contract boundary (ADR 0188) — MCP consumes, never derives.

orcho-core owns criterion identity, verification classes, the state algebra,
receipt freshness, gate selection, executors, blocking consequences,
readiness, and the durable decision log. MCP projects those facts onto the
wire. That split is easy to state and easy to erode: one convenient
``if state == "proven"`` in a projector, one ``ready = not blockers`` in a
next-action builder, and two surfaces start disagreeing about whether a run
is releasable.

This guard makes the split structural, so it survives refactors and reviewer
fatigue. It is the executable half of the repo-wide consumer census recorded
in ``docs/architecture/mcp_boundaries.md``:

1. the criterion SDK slice is imported in exactly ONE module — the single
   projection path;
2. no other module carries a local criterion-state table (the shape a
   re-derivation of state / readiness / owners takes);
3. no wire model reintroduces string criteria;
4. a criterion payload that is *absent* is an omitted key, never ``null`` —
   the field declares the non-nullable schema policy AND the owning model
   applies the matching dump policy.

Update protocol: relaxing any rule here means the boundary itself moved.
Change the guard and the architecture doc in the same diff, and say plainly
what the new boundary is.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

PKG_ROOT = Path(__file__).resolve().parents[3] / "src" / "orcho_mcp"

#: The one module allowed to call the criterion SDK slice.
PROJECTION_PATH = "services/criterion_projection.py"

#: The one module allowed to mirror core's state vocabulary (as a wire enum).
WIRE_MODEL_PATH = "schemas/criteria.py"

#: Core SDK symbols that carry criterion facts. Importing one is a claim to be
#: the projection path.
CRITERION_SDK_SYMBOLS = frozenset({
    "get_criterion_matrix",
    "record_criterion_decision",
    "list_criterion_decisions",
    "CRITERION_STATE_ORDER",
    "EXECUTABLE_STATE_PRECEDENCE",
    "CRITERION_MATRIX_KEY",
    "canonical_criterion_json",
    "criterion_matrix_example",
    "human_decision_chain_example",
})

#: Core's criterion states. Several of these words also belong to unrelated
#: MCP vocabularies (a *finding* is ``advisory``, a *release* is ``rejected``,
#: a *gate* is ``stale``), so the state set alone is not a signal.
CRITERION_STATES = frozenset({
    "proven", "failed", "stale", "missing", "not_selected",
    "advisory", "accepted", "rejected", "pending",
})

#: The two states that belong to NO other MCP vocabulary. A module naming one
#: of these is unambiguously talking about criteria; that plus a plural state
#: set is a local state table. Keeping the marker requirement is what stops
#: the guard firing on ``read.py``'s unrelated ``failed`` / ``missing`` /
#: ``pending`` literals.
CRITERION_ONLY_STATES = frozenset({"proven", "not_selected"})

#: Wire fields whose absence must be an omitted key, never ``null``. Includes
#: the decision result's ``matrix`` / ``matrix_error`` pair: a recorded
#: decision whose readback failed reports an ABSENT matrix and a named reason,
#: and a ``null`` there would read as "the matrix is empty" rather than "the
#: matrix could not be read".
ABSENT_NOT_NULL_FIELDS = frozenset({
    "criterion_matrix", "criterion_readiness", "matrix", "matrix_error",
})


def _python_files() -> list[Path]:
    return sorted(
        p for p in PKG_ROOT.rglob("*.py") if "__pycache__" not in p.parts
    )


def _rel(path: Path) -> str:
    return path.relative_to(PKG_ROOT).as_posix()


@pytest.mark.parametrize("path", _python_files(), ids=_rel)
def test_criterion_sdk_has_one_call_site(path: Path) -> None:
    """Only the projection path may import the criterion SDK slice."""
    rel = _rel(path)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.module != "sdk":
            continue
        leaked = sorted(
            {a.name for a in node.names} & CRITERION_SDK_SYMBOLS,
        )
        if leaked and rel != PROJECTION_PATH:
            pytest.fail(
                f"{rel}:{node.lineno} imports the criterion SDK slice "
                f"{leaked}. There is exactly one projection path — "
                f"``{PROJECTION_PATH}`` — so plan, evidence, status, "
                "diagnose, and delivery cannot disagree about criterion "
                "blockers. Call into that module instead of re-reading the "
                "SDK here.",
            )


@pytest.mark.parametrize("path", _python_files(), ids=_rel)
def test_no_local_criterion_state_table(path: Path) -> None:
    """No module may carry its own copy of core's state vocabulary.

    A local state table is how re-derivation starts: once a module can name
    ``proven`` / ``failed`` / ``pending`` itself, deciding blocking or
    readiness from them is a one-line step. The wire enum in
    ``schemas/criteria.py`` is the single mirror, and
    ``test_criterion_wire_equivalence`` pins it to core's own constant.

    Only *code* is scanned — docstrings and comments explain the vocabulary
    freely, which is where the explanation belongs.
    """
    rel = _rel(path)
    if rel == WIRE_MODEL_PATH:
        return
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value in CRITERION_STATES
        and not _is_docstring(tree, node)
    }
    if len(found) >= 3 and found & CRITERION_ONLY_STATES:
        pytest.fail(
            f"{rel} names {len(found)} criterion states in code "
            f"({sorted(found)}) — that is a local state table. Criterion "
            "state, readiness, executors, and blocking consequences come "
            f"from orcho-core through ``{PROJECTION_PATH}``; MCP forwards "
            f"them. The only wire mirror lives in ``{WIRE_MODEL_PATH}``.",
        )


def _is_docstring(tree: ast.AST, target: ast.Constant) -> bool:
    """Whether ``target`` is a module / class / function docstring."""
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
        ):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and body[0].value is target
            ):
                return True
    return False


@pytest.mark.parametrize("path", _python_files(), ids=_rel)
def test_no_string_acceptance_criteria_on_the_wire(path: Path) -> None:
    """``acceptance_criteria`` is typed criterion objects, never prose."""
    rel = _rel(path)
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, ast.AnnAssign):
            continue
        if not isinstance(node.target, ast.Name):
            continue
        if node.target.id != "acceptance_criteria":
            continue
        annotation = ast.unparse(node.annotation)
        if "str" in annotation and "Criterion" not in annotation:
            pytest.fail(
                f"{rel}:{node.lineno} declares "
                f"``acceptance_criteria: {annotation}``. ADR 0188 criteria "
                "are typed objects with a stable id, a verification class, "
                "and complete gate identities; stringifying them destroys "
                "the traceability the matrix is built on. Use "
                "``list[PlanCriterionRecord]``.",
            )


@pytest.mark.parametrize("path", _python_files(), ids=_rel)
def test_absent_criterion_payloads_are_omitted_not_null(path: Path) -> None:
    """An absent matrix / readiness is an omitted key, declared and dumped.

    Both halves must be present on the owning model: the JSON Schema policy
    (so the published contract does not advertise ``null``) and the dump
    policy (so the serializer does not emit one).
    """
    rel = _rel(path)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        omitted = _omitted_keys(cls)
        for node in cls.body:
            if not isinstance(node, ast.AnnAssign):
                continue
            if not isinstance(node.target, ast.Name):
                continue
            name = node.target.id
            if name not in ABSENT_NOT_NULL_FIELDS:
                continue
            declaration = ast.unparse(node)
            assert "non_nullable_optional" in declaration or (
                "CriterionReadinessField" in declaration
            ), (
                f"{rel}:{node.lineno} — ``{cls.name}.{name}`` must declare "
                "the ``non_nullable_optional`` JSON Schema policy so the "
                "published contract never advertises ``null`` for an absent "
                "criterion payload."
            )
            assert name in omitted, (
                f"{rel}:{cls.name} declares ``{name}`` but does not pass it "
                "to ``omit_absent_keys(...)``. Without the dump policy the "
                "model serializes an absent criterion payload as ``null``, "
                "which reads as 'unknown' rather than 'no criterion "
                "contract'."
            )


def _omitted_keys(cls: ast.ClassDef) -> set[str]:
    """Field names the class passes to ``omit_absent_keys(...)``."""
    keys: set[str] = set()
    for node in ast.walk(cls):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "omit_absent_keys":
            keys.update(
                a.value for a in node.args
                if isinstance(a, ast.Constant) and isinstance(a.value, str)
            )
    return keys
