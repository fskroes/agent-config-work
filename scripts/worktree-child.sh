#!/usr/bin/env bash
set -u
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || printf %s "${BASH_SOURCE[0]}")")/agent-config.sh" \
  || { echo "STOP: agent-config.sh is missing next to this script, run install.sh again"; exit 1; }
REPO_PATH=${1:-}
SLUG=${2:-}
stop() { echo "STOP: $*"; exit 1; }
[ -n "$REPO_PATH" ] && [ -n "$SLUG" ] || stop "usage: worktree-child.sh <repo-path> <slug>"

SRC=${BASH_SOURCE[0]}
while [ -L "$SRC" ]; do
  DIR=$(cd "$(dirname "$SRC")" && pwd)
  SRC=$(readlink "$SRC")
  case "$SRC" in /*) ;; *) SRC="$DIR/$SRC" ;; esac
done
HERE=$(cd "$(dirname "$SRC")" && pwd)

HOME_COMMON=$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null) \
  || stop "this folder is not in a git repo: start the session in the Home Repo"
[ "$(agent_cfg --bool home)" = true ] \
  || stop "this repo is not a Home Repo. Fernando runs: git config agent.home true"
HOME_COMMON=$(cd "$HOME_COMMON" && pwd -P)

SID=${CLAUDE_CODE_SESSION_ID:-${COPILOT_AGENT_SESSION_ID:-}}
[ -n "$SID" ] || stop "CLAUDE_CODE_SESSION_ID and COPILOT_AGENT_SESSION_ID are unset, the worktree cannot be recorded for a session"
KEY=$(printf %s "$SID" | LC_ALL=C tr -c 'A-Za-z0-9_-' '_')

D=$REPO_PATH
while [ ! -d "$D" ]; do
  [ "$D" = "$(dirname "$D")" ] && break
  D=$(dirname "$D")
done
CHILD_COMMON=$(git -C "$D" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) \
  || stop "$REPO_PATH is not in a git repo"
CHILD_COMMON=$(cd "$CHILD_COMMON" && pwd -P)
[ "$CHILD_COMMON" != "$HOME_COMMON" ] || stop "$REPO_PATH is in the Home Repo itself: use worktree-setup.sh there"
CHILD_ROOT=$(dirname "$CHILD_COMMON")

OUT=$(cd "$CHILD_ROOT" && bash "$HERE/worktree-setup.sh" "$SLUG" 2>&1); RC=$?
STOPLINE=$(printf '%s\n' "$OUT" | grep '^STOP:' | tail -1)
if [ $RC -ne 0 ] || [ -n "$STOPLINE" ]; then
  printf '%s\n' "${STOPLINE:-STOP: worktree-setup.sh failed in $CHILD_ROOT: $OUT}"
  exit 1
fi
printf '%s\n' "$OUT" | grep -v '^\(WORKTREE\|SOLO\) '
WT=$(printf '%s\n' "$OUT" | sed -n 's/^WORKTREE //p' | tail -1)
if [ -z "$WT" ] && printf '%s\n' "$OUT" | grep -q '^SOLO '; then WT=$CHILD_ROOT; fi
[ -n "$WT" ] && [ -d "$WT" ] || stop "worktree-setup.sh gave no worktree path: $OUT"
WT=$(cd "$WT" && pwd -P)

REC="$HOME_COMMON/claude-child-worktrees/$KEY"
mkdir -p "$HOME_COMMON/claude-child-worktrees" || stop "cannot write in $HOME_COMMON"
touch "$REC"
grep -qxF "$WT" "$REC" || printf '%s\n' "$WT" >> "$REC"
[ -z "${T3CODE_THREAD_ID:-}" ] || echo "THREAD-TITLE $(basename "$CHILD_ROOT")"
echo "CHILD $WT"
