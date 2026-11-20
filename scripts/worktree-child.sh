#!/usr/bin/env bash
# Make a worktree in a child repo for a session that runs in a Home Repo. Rules in the
# root CLAUDE.md of the Home Repo, "How to work across repos from here".
#
#   worktree-child.sh <repo-path> <slug>
#
#   repo-path: any path inside the child repo
#   slug:      lowercase, dash separated, three words at most
#
# Run it from a session whose repo is a Home Repo (git config agent.home true).
# It runs worktree-setup.sh in the shared checkout of the child, and records the
# path it got for this session. guard.py (rule home-child) and worktree-guard.sh
# read the record: a Home session may write in a child repo only there.
#
# The record is one file per session in the git common dir of the Home Repo:
#   <home>/.git/claude-child-worktrees/<session id>
# One real absolute path per line. Nothing is written under ~/.claude.
#
# Last line printed: CHILD <path>, exit 0. Or STOP: <reason>, exit 1.
set -u
# agent_cfg: agent.<key>, else the old name claude.<key>. Next to this script, links followed.
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || printf %s "${BASH_SOURCE[0]}")")/agent-config.sh" \
  || { echo "STOP: agent-config.sh is missing next to this script, run install.sh again"; exit 1; }
REPO_PATH=${1:-}
SLUG=${2:-}
stop() { echo "STOP: $*"; exit 1; }
[ -n "$REPO_PATH" ] && [ -n "$SLUG" ] || stop "usage: worktree-child.sh <repo-path> <slug>"

# The folder of this script, with links followed: install.sh links it into ~/.claude/scripts,
# and on a work laptop it lives somewhere else.
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

# Claude Code sets the first, GitHub Copilot CLI the second
SID=${CLAUDE_CODE_SESSION_ID:-${COPILOT_AGENT_SESSION_ID:-}}
[ -n "$SID" ] || stop "CLAUDE_CODE_SESSION_ID and COPILOT_AGENT_SESSION_ID are unset, the worktree cannot be recorded for a session"
KEY=$(printf %s "$SID" | LC_ALL=C tr -c 'A-Za-z0-9_-' '_')

# Walk up to a folder that exists, so a path that is not made yet still works.
D=$REPO_PATH
while [ ! -d "$D" ]; do
  [ "$D" = "$(dirname "$D")" ] && break
  D=$(dirname "$D")
done
CHILD_COMMON=$(git -C "$D" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) \
  || stop "$REPO_PATH is not in a git repo"
CHILD_COMMON=$(cd "$CHILD_COMMON" && pwd -P)
[ "$CHILD_COMMON" != "$HOME_COMMON" ] || stop "$REPO_PATH is in the Home Repo itself: use worktree-setup.sh there"
CHILD_ROOT=$(dirname "$CHILD_COMMON")   # the shared checkout, as in worktree-setup.sh

OUT=$(cd "$CHILD_ROOT" && bash "$HERE/worktree-setup.sh" "$SLUG" 2>&1); RC=$?
STOPLINE=$(printf '%s\n' "$OUT" | grep '^STOP:' | tail -1)
if [ $RC -ne 0 ] || [ -n "$STOPLINE" ]; then
  printf '%s\n' "${STOPLINE:-STOP: worktree-setup.sh failed in $CHILD_ROOT: $OUT}"
  exit 1
fi
printf '%s\n' "$OUT" | grep -v '^\(WORKTREE\|SOLO\) '   # notes, such as re-attaching
WT=$(printf '%s\n' "$OUT" | sed -n 's/^WORKTREE //p' | tail -1)
if [ -z "$WT" ] && printf '%s\n' "$OUT" | grep -q '^SOLO '; then WT=$CHILD_ROOT; fi   # solo child: no worktree
[ -n "$WT" ] && [ -d "$WT" ] || stop "worktree-setup.sh gave no worktree path: $OUT"
WT=$(cd "$WT" && pwd -P)

REC="$HOME_COMMON/claude-child-worktrees/$KEY"
mkdir -p "$HOME_COMMON/claude-child-worktrees" || stop "cannot write in $HOME_COMMON"
touch "$REC"
grep -qxF "$WT" "$REC" || printf '%s\n' "$WT" >> "$REC"
echo "CHILD $WT"
