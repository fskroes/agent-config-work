#!/usr/bin/env bash
set -u
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || printf %s "${BASH_SOURCE[0]}")")/agent-config.sh" \
  || { echo "STOP: agent-config.sh is missing next to this script, run install.sh again"; exit 1; }
if [ $# -gt 0 ]; then
  P=$1
  while [ ! -d "$P" ]; do
    [ "$P" = "$(dirname "$P")" ] && break
    P=$(dirname "$P")
  done
  [ -d "$P" ] || P=.
  TOP=$(git -C "$P" rev-parse --show-toplevel 2>/dev/null) || { echo "OK $P (not in a git work tree)"; exit 0; }
  if [ "$(agent_cfg --bool home)" = true ]; then
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
  cd "$P" || exit 1
fi
if [ "$(agent_cfg mode)" = solo ]; then
  echo "OK $(git rev-parse --show-toplevel) (solo repo)"; exit 0
fi
if [ "$(git rev-parse --path-format=absolute --git-dir)" != "$(git rev-parse --path-format=absolute --git-common-dir)" ]; then
  echo "OK $(git rev-parse --show-toplevel) (linked worktree)"; exit 0
fi
echo "WRONG TREE: you are in the shared checkout, set up a worktree (worktree-setup.sh <slug>)"
exit 1
