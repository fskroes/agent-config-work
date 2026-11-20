#!/usr/bin/env bash
# Second task in the same session: new branch, same worktree, clean tree first.
# Only while the session's context is under 150k tokens; above it, guard.py rule
# fresh-session refuses this script and the next task starts in a new session.
# Never a second worktree. In a solo repo this is the whole ritual. Rules in AGENTS.md, "Branching".
#
#   worktree-next.sh <slug>
#
# Prints BRANCH <name> or STOP: <reason> (exit 1). Run it inside the session's worktree
# (or the checkout, in solo mode).
set -u
SLUG=${1:-}
case "$SLUG" in
  ''|*[!a-z0-9-]*|-*|*-|*-*-*-*) echo "STOP: usage: worktree-next.sh <slug> (lowercase letters, digits, dashes, three words at most)"; exit 1 ;;
esac
# switch -c carries uncommitted work into task two.
git status --porcelain | grep -q . && { echo "STOP: commit or stash first"; exit 1; }
# Base resolved as setup does: an unset origin/HEAD would put task two on task
# one's branch, and its PR.
git fetch -q origin 2>/dev/null
git remote set-head -a origin >/dev/null 2>&1
BASE=$(git symbolic-ref -q --short refs/remotes/origin/HEAD)
for c in origin/main origin/master main master; do
  [ -n "$BASE" ] && break
  git rev-parse -q --verify "$c" >/dev/null && BASE=$c
done
[ -n "$BASE" ] || { echo "STOP: cannot find a base branch"; exit 1; }
if [ "$(git config claude.mode)" = solo ]; then
  NAME=$SLUG
else
  OWNER=$(git config claude.branchPrefix)
  [ -n "$OWNER" ] || { echo "STOP: git config claude.branchPrefix is unset, run worktree-setup.sh first"; exit 1; }
  SID=${CLAUDE_CODE_SESSION_ID:-${COPILOT_AGENT_SESSION_ID:-}}   # Claude Code, else Copilot CLI
  SID=${SID:0:8}
  [ -n "$SID" ] || { echo "STOP: CLAUDE_CODE_SESSION_ID and COPILOT_AGENT_SESSION_ID are unset"; exit 1; }
  NAME="$OWNER/$SLUG-$SID"
fi
git switch -qc "$NAME" "$BASE" || exit 1
echo "BRANCH $NAME"
