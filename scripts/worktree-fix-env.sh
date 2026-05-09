#!/usr/bin/env bash
# Symlink the main worktree's .env into any linked worktree that is missing
# one. Existing files (regular or symlink) are left untouched and reported.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MAIN_WORKTREE="$(cd "$SCRIPT_DIR/.." && pwd)"

usage() {
  cat <<'EOF'
Usage: scripts/worktree-fix-env.sh [--dry-run]

`.env` が欠落している linked worktree を一括で symlink 修復する。
既存のファイル / symlink は触らず、SKIP / WARN として報告する。

Options:
  --dry-run  実際には ln -s せず、何が起きるかだけ表示

Env:
  VECTOR_ENV_SOURCE  symlink 元 .env のパス (default: <main worktree>/.env)
EOF
}

dry_run=0
case "${1:-}" in
  -h|--help)
    usage
    exit 0
    ;;
  --dry-run)
    dry_run=1
    ;;
  "")
    ;;
  *)
    echo "ERROR: unknown argument: $1" >&2
    usage
    exit 1
    ;;
esac

env_source="${VECTOR_ENV_SOURCE:-$MAIN_WORKTREE/.env}"

if [ ! -f "$env_source" ]; then
  echo "ERROR: env source not found: $env_source" >&2
  exit 1
fi

main_wt="$(cd "$(dirname "$env_source")" && pwd)"

linked=0
skipped_symlink=0
skipped_file=0
created=0

while read -r wt; do
  [ -z "$wt" ] && continue
  # main worktree 自身は除外
  if [ "$wt" = "$main_wt" ]; then
    continue
  fi
  linked=$((linked + 1))
  target="$wt/.env"
  if [ -L "$target" ]; then
    printf 'SKIP (symlink): %s\n' "$target"
    skipped_symlink=$((skipped_symlink + 1))
  elif [ -e "$target" ]; then
    printf 'WARN (regular file, not touching): %s\n' "$target" >&2
    skipped_file=$((skipped_file + 1))
  else
    if [ "$dry_run" -eq 1 ]; then
      printf 'WOULD LINK: %s -> %s\n' "$target" "$env_source"
    else
      ln -s "$env_source" "$target"
      printf 'LINK: %s -> %s\n' "$target" "$env_source"
    fi
    created=$((created + 1))
  fi
done < <(cd "$MAIN_WORKTREE" && git worktree list --porcelain | awk '/^worktree /{print $2}')

printf '\nSummary: %d linked worktree(s) inspected, %d new link(s)%s, %d existing symlink(s), %d existing file(s) left untouched.\n' \
  "$linked" "$created" "$([ "$dry_run" -eq 1 ] && printf ' (dry-run)' || true)" "$skipped_symlink" "$skipped_file"
