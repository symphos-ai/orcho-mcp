"""orcho_mcp.supervisor.paths — workspace / project path resolution.

Centralises the path-math the spawn / resume / cancel / recover paths
share: where the runs directory lives, how to derive the workspace
root from a runs dir, and the project_dir-once-resolution-rule that
prevents the workspace-relative path doubling regression.
"""
from __future__ import annotations

from pathlib import Path

from core.infra import config as _core_config

from orcho_mcp.errors import (
    PipelineSpawnError,
    WorkspaceNotResolvedError,
)

_TASK_FILES_DIR = Path(".orcho") / ".task-files"


def resolve_runs_dir() -> Path:
    """Resolve workspace runs dir or raise WorkspaceNotResolvedError."""
    try:
        return _core_config.get_runs_dir()
    except Exception as e:  # noqa: BLE001
        raise WorkspaceNotResolvedError(
            f"could not resolve runs directory: {e}. "
            "Set $ORCHO_WORKSPACE or run from inside an orcho workspace."
        ) from e


def workspace_from_runs_dir(runs_dir: Path) -> str:
    """Return the workspace directory for ``<workspace>/runspace/runs``."""
    return str(runs_dir.parent.parent)


def resolve_project_dir(project_dir: str) -> str:
    """Resolve the caller's ``project_dir`` to an absolute path once.

    The supervisor passes project_dir to subprocess.Popen as both ``cwd=``
    and (via ``--project``) to the orchestrator's argv. If we keep a
    relative input, Popen interprets cwd relative to the MCP server's
    cwd while orcho-core re-resolves ``--project`` relative to the
    ALREADY-CHANGED subprocess cwd, doubling the segment
    (e.g. ``orcho-web`` → ``orcho-web/orcho-web``). Resolve once here so
    cwd and ``--project`` agree on the same absolute path.
    """
    if not project_dir or not project_dir.strip():
        raise PipelineSpawnError(
            "project_dir is required and must be a non-empty path"
        )
    resolved = Path(project_dir).expanduser().resolve()
    if not resolved.is_dir():
        raise PipelineSpawnError(
            f"project_dir does not exist or is not a directory: "
            f"{project_dir!r} (resolved to {resolved})"
        )
    return str(resolved)


def _task_file_lookup_dirs(project_dir: Path) -> tuple[Path, ...]:
    return tuple(
        dict.fromkeys(
            ancestor / _TASK_FILES_DIR
            for ancestor in (project_dir, *project_dir.parents)
        )
    )


def _missing_task_file_message(
    task_file: str,
    resolved: Path,
    *,
    project_dir: Path,
) -> str:
    path = Path(task_file).expanduser()
    if path.is_absolute() or path.parent != Path(".") or path.suffix.lower() != ".md":
        return (
            f"--task-file not found: {resolved}. Pass an existing file path, "
            f"or use a short .md name stored under {_TASK_FILES_DIR}."
        )

    search_dirs = "\n".join(
        f"  - {task_dir}" for task_dir in _task_file_lookup_dirs(project_dir)
    )
    return (
        f"--task-file short name not found: {task_file}\n"
        f"Orcho treated {path.name!r} as a short task-file name (a bare *.md "
        f"name with no path), so it looked for it only in the reserved "
        f"{_TASK_FILES_DIR} directories:\n{search_dirs}\n"
        f"To fix this, either put {path.name!r} into one of those "
        f"{_TASK_FILES_DIR} directories, or pass a direct relative/absolute "
        f"path to the task file."
    )


def resolve_task_file(task_file: str | None, *, project_dir: str) -> str | None:
    """Resolve ``task_file`` before spawning a pipeline subprocess.

    This mirrors the core CLI's short-name convention for bare ``*.md``
    files while failing fast at the MCP boundary. Without this preflight,
    the supervisor can create a run directory and only then watch the
    subprocess die before it emits events.
    """
    if task_file is None:
        return None
    if not task_file.strip():
        raise PipelineSpawnError(
            "task_file is required and must be a non-empty path"
        )

    project_path = Path(project_dir).expanduser().resolve()
    path = Path(task_file).expanduser()
    resolved = path if path.is_absolute() else (project_path / path)

    if (
        not path.is_absolute()
        and path.parent == Path(".")
        and path.suffix.lower() == ".md"
    ):
        for task_dir in _task_file_lookup_dirs(project_path):
            candidate = task_dir / path.name
            if candidate.is_file():
                return str(candidate.resolve())

    if resolved.is_file():
        return str(resolved.resolve())

    if resolved.exists() and not resolved.is_file():
        raise PipelineSpawnError(
            f"--task-file is not a file: {resolved.resolve()}"
        )

    raise PipelineSpawnError(
        _missing_task_file_message(
            task_file,
            resolved,
            project_dir=project_path,
        )
    )


__all__ = [
    "resolve_task_file",
    "resolve_project_dir",
    "resolve_runs_dir",
    "workspace_from_runs_dir",
]
