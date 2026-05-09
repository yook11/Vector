#!/usr/bin/env bash
# Wrap `git worktree add` and symlink the main worktree's .env into the new
# worktree. The .env source is resolved dynamically (main worktree by default)
# and never overwrites an existing file or symlink in the new worktree.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MAIN_WORKTREE="$(cd "$SCRIPT_DIR/.." && pwd)"

usage() {
  cat <<'EOF'
Usage: scripts/worktree-add.sh <git worktree add の引数...>

新しい worktree を作成し、main worktree の .env を symlink する。

Env:
  VECTOR_ENV_SOURCE  symlink 元 .env のパス (default: <main worktree>/.env)

Examples:
  scripts/worktree-add.sh ../Vector-foo feature/foo
  scripts/worktree-add.sh -b feature/bar ../Vector-bar main
  VECTOR_ENV_SOURCE=$HOME/secrets/vector.env scripts/worktree-add.sh ../Vector-baz
EOF
}

if [ $# -eq 0 ]; then
  usage
  exit 1
fi
case "${1:-}" in
  -h|--help)
    usage
    exit 0
    ;;
esac

env_source="${VECTOR_ENV_SOURCE:-$MAIN_WORKTREE/.env}"

if [ ! -f "$env_source" ]; then
  echo "ERROR: env source not found: $env_source" >&2
  echo "Set VECTOR_ENV_SOURCE to point at the .env file to symlink." >&2
  exit 1
fi

cd "$MAIN_WORKTREE"

before=$(git worktree list --porcelain | awk '/^worktree /{print $2}' | sort)
git worktree add "$@"
after=$(git worktree list --porcelain | awk '/^worktree /{print $2}' | sort)

new_wt=$(comm -13 <(printf '%s\n' "$before") <(printf '%s\n' "$after") | head -n 1)

if [ -z "$new_wt" ]; then
  echo "WARN: could not detect newly added worktree path; skipping .env symlink" >&2
  exit 0
fi

target="$new_wt/.env"
if [ -L "$target" ]; then
  echo "INFO: $target already a symlink; skipping" >&2
elif [ -e "$target" ]; then
  echo "WARN: $target already exists as a regular file; skipping (refusing to overwrite)" >&2
else
  ln -s "$env_source" "$target"
  echo "OK: linked $target -> $env_source"
fi
