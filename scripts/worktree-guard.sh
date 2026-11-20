#!/usr/bin/env bash
# The guard: am I about to write in the shared checkout? Run before the first write of
# every task and after any compaction. Rules in AGENTS.md, "Branching".
#
#   worktree-guard.sh [path]
#
# Exit 0 and print OK <dir> when writing here is allowed (solo repo, or a linked worktree).
# Exit 1 and print WRONG TREE when this is the shared checkout of a team repo.
#
# With a path, the checks run on the tree of that path. One more case: in a Home session
# (the repo of the current folder has claude.home true), a path in another repo is OK only
# when its work tree root is in this session's record, made by worktree-child.sh. Else it
# is WRONG TREE. A path in the Home Repo itself is checked like any other. A path outside
# any git work tree is OK: no rule covers it.
#
# In the shared checkout git-dir and git-common-dir are the same directory; in any
# linked worktree they differ. No cd, no $HOME, so a symlinked path cannot fool it.
# --path-format=absolute is load-bearing: from a subdirectory the common dir comes
# back relative ("../.git"), the two stop matching, and the guard goes silent in
# the shared checkout, which is the one direction that must never happen.
set -u
if [ $# -gt 0 ]; then
  P=$1
  while [ ! -d "$P" ]; do   # a file or a path not made yet: its nearest folder
    [ "$P" = "$(dirname "$P")" ] && break
    P=$(dirname "$P")
  done
  [ -d "$P" ] || P=.
  TOP=$(git -C "$P" rev-parse --show-toplevel 2>/dev/null) || { echo "OK $P (not in a git work tree)"; exit 0; }
  if [ "$(git config --bool claude.home 2>/dev/null)" = true ]; then
    HOME_COMMON=$(git rev-parse --path-format=absolute --git-common-dir)
    THERE_COMMON=$(git -C "$P" rev-parse --path-format=absolute --git-common-dir)
    HOME_COMMON=$(cd "$HOME_COMMON" && pwd -P)
    if [ "$HOME_COMMON" != "$(cd "$THERE_COMMON" && pwd -P)" ]; then
      SID=${CLAUDE_CODE_SESSION_ID:-${COPILOT_AGENT_SESSION_ID:-}}
      KEY=$(printf %s "$SID" | LC_ALL=C tr -c 'A-Za-z0-9_-' '_')
      TOP=$(cd "$TOP" && pwd -P)
      if [ -n "$KEY" ] && grep -qxF "$TOP" "$HOME_COMMON/claude-child-worktrees/$KEY" 2>/dev/null; then
        echo "OK $TOP (child worktree)"; exit 0
      fi
      echo "WRONG TREE: $TOP is in another repo and this session has no worktree there, run: bash ~/.claude/scripts/worktree-child.sh $TOP <slug>"
      exit 1
    fi
  fi
  cd "$P" || exit 1   # the checks below run on the tree of the path
fi
if [ "$(git config claude.mode)" = solo ]; then
  echo "OK $(git rev-parse --show-toplevel) (solo repo)"; exit 0
fi
if [ "$(git rev-parse --path-format=absolute --git-dir)" != "$(git rev-parse --path-format=absolute --git-common-dir)" ]; then
  echo "OK $(git rev-parse --show-toplevel) (linked worktree)"; exit 0
fi
echo "WRONG TREE: you are in the shared checkout, set up a worktree (worktree-setup.sh <slug>)"
exit 1
