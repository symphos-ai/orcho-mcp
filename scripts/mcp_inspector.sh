#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

python_bin="${ORCHO_MCP_PYTHON:-${repo_root}/.venv/bin/python}"
workspace="${ORCHO_WORKSPACE:-$(cd "${repo_root}/.." && pwd)/workspace-orchestrator}"
worktree="${ORCHO_WORKTREE:-${workspace}/worktree}"

if [[ ! -x "${python_bin}" ]]; then
  echo "orcho-mcp Python not found or not executable: ${python_bin}" >&2
  echo "Set ORCHO_MCP_PYTHON=/path/to/python or create ${repo_root}/.venv." >&2
  exit 1
fi

if [[ ! -d "${workspace}" ]]; then
  echo "ORCHO_WORKSPACE does not exist: ${workspace}" >&2
  echo "Set ORCHO_WORKSPACE=/path/to/workspace-orchestrator." >&2
  exit 1
fi

mkdir -p "${worktree}/runs"

cat <<EOF
Starting MCP Inspector for orcho-mcp

  ORCHO_WORKSPACE=${workspace}
  ORCHO_WORKTREE=${worktree}
  command=${python_bin} -m orcho_mcp

EOF

exec npx @modelcontextprotocol/inspector \
  -e "ORCHO_WORKSPACE=${workspace}" \
  -e "ORCHO_WORKTREE=${worktree}" \
  -- \
  "${python_bin}" -m orcho_mcp
