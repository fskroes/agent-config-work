#!/usr/bin/env bash
set -u
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || printf %s "${BASH_SOURCE[0]}")")/agent-config.sh" \
  || { echo "STOP: agent-config.sh is missing next to this script, run install.sh again"; exit 1; }
SLUG=${1:-}
case "$SLUG" in
  ''|*[!a-z0-9-]*|-*|*-|*-*-*-*) echo "STOP: usage: worktree-next.sh <slug> (lowercase letters, digits, dashes, three words at most)"; exit 1 ;;
esac
git status --porcelain | grep -q . && { echo "STOP: commit or stash first"; exit 1; }
git fetch -q origin 2>/dev/null
git remote set-head -a origin >/dev/null 2>&1
BASE=$(git symbolic-ref -q --short refs/remotes/origin/HEAD)
for c in origin/main origin/master main master; do
  [ -n "$BASE" ] && break
  git rev-parse -q --verify "$c" >/dev/null && BASE=$c
done
[ -n "$BASE" ] || { echo "STOP: cannot find a base branch"; exit 1; }
if [ "$(agent_cfg mode)" = solo ]; then
  NAME=$SLUG
else
  OWNER=$(agent_cfg branchPrefix)
  [ -n "$OWNER" ] || { echo "STOP: git config agent.branchPrefix is unset, run worktree-setup.sh first"; exit 1; }
  SID=${CLAUDE_CODE_SESSION_ID:-${COPILOT_AGENT_SESSION_ID:-}}
  SID=$(printf %s "$SID" | LC_ALL=C tr -c 'A-Za-z0-9_-' '_')
  SID=${SID:0:8}
  [ -n "$SID" ] || { echo "STOP: CLAUDE_CODE_SESSION_ID and COPILOT_AGENT_SESSION_ID are unset"; exit 1; }
  NAME="$OWNER/$SLUG-$SID"
fi
git switch -qc "$NAME" "$BASE" || exit 1
echo "BRANCH $NAME"
