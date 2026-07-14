#!/usr/bin/env bash

set -euo pipefail

readonly ZERO_SHA="0000000000000000000000000000000000000000"

is_sha() {
  [[ "$1" =~ ^[0-9a-f]{40}$ ]]
}

require_commit() {
  local sha="$1"
  local label="$2"

  if ! is_sha "$sha"; then
    echo "::error::$label must be a 40-character lowercase commit SHA" >&2
    exit 2
  fi
  if ! git cat-file -e "${sha}^{commit}" 2>/dev/null; then
    echo "::error::$label commit is unavailable; fetch-depth must be 0" >&2
    exit 2
  fi
}

scan_all_refs() {
  echo "Scanning patch history for all fetched refs"
  gitleaks git --redact --no-banner \
    --log-opts="--full-history --all --diff-filter=tuxdb --diff-merges=remerge" .

  echo "Scanning commit messages for all fetched refs"
  git log --all --no-patch --format='%H%n%B%n' -- |
    gitleaks stdin --redact --no-banner
}

scan_revision() {
  local revision="$1"
  local label="$2"

  echo "Scanning patch history for $label"
  gitleaks git --redact --no-banner \
    --log-opts="--full-history --diff-filter=tuxdb --diff-merges=remerge $revision" .

  echo "Scanning commit messages for $label"
  git log --no-patch --format='%H%n%B%n' "$revision" -- |
    gitleaks stdin --redact --no-banner
}

event_name="${GITLEAKS_EVENT_NAME:-}"
case "$event_name" in
  pull_request)
    base_sha="${GITLEAKS_BASE_SHA:-}"
    head_sha="${GITLEAKS_HEAD_SHA:-}"
    require_commit "$base_sha" "pull request base"
    require_commit "$head_sha" "pull request head"
    scan_revision "${base_sha}..${head_sha}" "pull request range"
    ;;
  push)
    if [[ "${GITLEAKS_PUSH_DELETED:-false}" == "true" ]]; then
      echo "Deleted ref has no introduced history; skipping scan"
      exit 0
    fi

    after_sha="${GITLEAKS_AFTER_SHA:-}"
    require_commit "$after_sha" "push after"
    before_sha="${GITLEAKS_BEFORE_SHA:-}"

    if [[ "${GITLEAKS_PUSH_CREATED:-false}" == "true" || "$before_sha" == "$ZERO_SHA" ]]; then
      scan_all_refs
      exit 0
    fi
    if ! is_sha "$before_sha"; then
      echo "::error::push before must be a 40-character lowercase commit SHA" >&2
      exit 2
    fi
    if git cat-file -e "${before_sha}^{commit}" 2>/dev/null; then
      scan_revision "${before_sha}..${after_sha}" "push range"
    else
      echo "::warning::push before commit is unavailable; scanning all history reachable from after"
      scan_revision "$after_sha" "push after history"
    fi
    ;;
  *)
    echo "::error::unsupported event for history scan: ${event_name:-<empty>}" >&2
    exit 2
    ;;
esac
