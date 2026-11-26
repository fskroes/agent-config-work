#!/usr/bin/env bash
set -u
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || printf %s "${BASH_SOURCE[0]}")")" && pwd)"
git rev-parse --git-common-dir >/dev/null 2>&1 || { echo "STOP: run this inside a git repo"; exit 1; }
apply=--apply
args=()
for a in "$@"; do
  case "$a" in
    --dry-run) apply="" ;;
    *) args+=("$a") ;;
  esac
done
exec python3 "$HERE/reaper.py" ${apply:+"$apply"} --volumes --only worktrees --repo . ${args[@]+"${args[@]}"}
