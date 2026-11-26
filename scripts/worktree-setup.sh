#!/usr/bin/env bash
set -u
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || printf %s "${BASH_SOURCE[0]}")")/agent-config.sh" \
  || { echo "STOP: agent-config.sh is missing next to this script, run install.sh again"; exit 1; }
SLUG=${1:-}
case "$SLUG" in
  ''|*[!a-z0-9-]*|-*|*-|*-*-*-*) echo "STOP: usage: worktree-setup.sh <slug> (lowercase letters, digits, dashes, three words at most)"; exit 1 ;;
esac

git fetch -q origin 2>/dev/null
git remote set-head -a origin >/dev/null 2>&1
BASE=$(git symbolic-ref -q --short refs/remotes/origin/HEAD)
for c in origin/main origin/master main master; do
  [ -n "$BASE" ] && break
  git rev-parse -q --verify "$c" >/dev/null && BASE=$c
done
[ -n "$BASE" ] || { echo "STOP: cannot find a base branch"; exit 1; }

if [ "$(agent_cfg mode)" = solo ]; then
  [ "$(git branch --show-current)" = "$SLUG" ] && { echo "SOLO $SLUG"; exit 0; }
  git status --porcelain | grep -q . && { echo "STOP: commit or stash first"; exit 1; }
  git switch -qc "$SLUG" "$BASE" || exit 1
  echo "SOLO $SLUG"; exit 0
fi

SID=${CLAUDE_CODE_SESSION_ID:-${COPILOT_AGENT_SESSION_ID:-}}
SID=$(printf %s "$SID" | LC_ALL=C tr -c 'A-Za-z0-9_-' '_')
SID=${SID:0:8}
[ -n "$SID" ] || { echo "STOP: CLAUDE_CODE_SESSION_ID and COPILOT_AGENT_SESSION_ID are unset, every session would share one worktree"; exit 1; }
ROOT=$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")
KEY=$(basename "$ROOT")-$(printf %s "$ROOT" | cksum | cut -d' ' -f1)

PRIMARY="$HOME/.claude-worktrees"
FALLBACK="$HOME/.local/.claude-worktrees"
PROBE=${WORKTREE_PROBE_SECONDS:-5}
answers() {
  ( mkdir -p "$1" && : > "$1/.probe-$SID" && rm -f "$1/.probe-$SID" ) </dev/null >/dev/null 2>&1 &
  local p=$! i=0
  while [ $i -lt $((PROBE * 10)) ]; do
    kill -0 "$p" 2>/dev/null || { wait "$p"; return; }
    sleep 0.1; i=$((i + 1))
  done
  pkill -9 -P "$p" 2>/dev/null; kill -9 "$p" 2>/dev/null; disown "$p" 2>/dev/null
  return 1
}
registered() {
  [ -d "$1" ] && git worktree list --porcelain | grep -qFx "worktree $(cd "$1" && pwd -P)"
}
if registered "$FALLBACK/$KEY/$SID"; then
  WT="$FALLBACK/$KEY/$SID"
elif answers "$PRIMARY/$KEY"; then
  WT="$PRIMARY/$KEY/$SID"
else
  WT="$FALLBACK/$KEY/$SID"
  echo "NOTE: $PRIMARY did not answer in ${PROBE}s, using $FALLBACK" >&2
fi

OWNER=$(agent_cfg branchPrefix)
[ -n "$OWNER" ] || OWNER=$(gh api user --jq .login 2>/dev/null)
[ -n "$OWNER" ] || OWNER=$(git config user.email | cut -d@ -f1)
[ -n "$OWNER" ] || { echo "STOP: git config agent.branchPrefix YOUR_HANDLE"; exit 1; }
git config agent.branchPrefix "$OWNER"

BRANCH="$OWNER/$SLUG-$SID"
if registered "$WT"; then
  echo "re-attaching to existing worktree"
elif git show-ref -q --verify "refs/heads/$BRANCH"; then
  HELD=$(git worktree list --porcelain | awk -v b="branch refs/heads/$BRANCH" '/^worktree /{w=substr($0,10)} $0==b{print w}')
  [ -z "$HELD" ] || { echo "STOP: $BRANCH is checked out at $HELD, which this setup cannot use; if its drive is not answering, remount it, or start this task under a new slug"; exit 1; }
  echo "reusing branch $BRANCH"
  git worktree add "$WT" "$BRANCH" || { echo "STOP: git worktree add $WT $BRANCH failed"; exit 1; }
else
  git worktree add -b "$BRANCH" "$WT" "$BASE" || { echo "STOP: git worktree add $WT failed"; exit 1; }
fi
echo "WORKTREE $WT"
