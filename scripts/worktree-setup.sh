#!/usr/bin/env bash
# Start a task: one worktree per session, one branch per task. Rules in AGENTS.md, "Branching".
#
#   worktree-setup.sh <slug>      slug: lowercase, dash separated, three words at most
#
# Prints one of:
#   SOLO <branch>        solo repo (git config claude.mode solo): branch in this checkout, no worktree
#   WORKTREE <path>      team repo: the session's worktree, created or re-attached. Enter it next.
#   STOP: <reason>       nothing was created. Exit 1.
# Run it from the shared checkout, before the first write of the session.
set -u
SLUG=${1:-}
case "$SLUG" in
  ''|*[!a-z0-9-]*|-*|*-|*-*-*-*) echo "STOP: usage: worktree-setup.sh <slug> (lowercase letters, digits, dashes, three words at most)"; exit 1 ;;
esac

# Remote default branch, never local HEAD (that inherits another session's work).
# The ladder is because plenty of repos are master and origin/HEAD is often unset.
# Resolved before the mode split: solo needs the same base, or task two stacks
# on task one and lands both in one merge.
git fetch -q origin 2>/dev/null
git remote set-head -a origin >/dev/null 2>&1
BASE=$(git symbolic-ref -q --short refs/remotes/origin/HEAD)
for c in origin/main origin/master main master; do
  [ -n "$BASE" ] && break
  git rev-parse -q --verify "$c" >/dev/null && BASE=$c
done
[ -n "$BASE" ] || { echo "STOP: cannot find a base branch"; exit 1; }

# solo: no worktree, no owner prefix, no session id, so it also works under
# agents that set no session variable. switch -c would carry uncommitted work
# onto the new branch, which is what the clean check is for.
if [ "$(git config claude.mode)" = solo ]; then
  git status --porcelain | grep -q . && { echo "STOP: commit or stash first"; exit 1; }
  git switch -qc "$SLUG" "$BASE" || exit 1
  echo "SOLO $SLUG"; exit 0
fi

# Claude Code sets the first, GitHub Copilot CLI the second (work profile)
SID=${CLAUDE_CODE_SESSION_ID:-${COPILOT_AGENT_SESSION_ID:-}}
SID=${SID:0:8}
[ -n "$SID" ] || { echo "STOP: CLAUDE_CODE_SESSION_ID and COPILOT_AGENT_SESSION_ID are unset, every session would share one worktree"; exit 1; }
ROOT=$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")
KEY=$(basename "$ROOT")-$(printf %s "$ROOT" | cksum | cut -d' ' -f1)   # unique per repo PATH

# ~/.claude-worktrees can be a link to an external drive. A drive that stopped
# answering blocks every mkdir and stat under it for good, `[ -d ]` included, so
# the root is only touched in a child that is killed after PROBE seconds. The
# fallback is under $HOME on the boot disk; cleanup finds it by the same
# /.claude-worktrees/ path segment.
PRIMARY="$HOME/.claude-worktrees"
FALLBACK="$HOME/.local/.claude-worktrees"
PROBE=${WORKTREE_PROBE_SECONDS:-5}
answers() {   # 0 when the directory $1 can be made and written within PROBE seconds
  # no inherited stdout: a child that holds the caller's $(...) pipe blocks the caller with it
  ( mkdir -p "$1" && : > "$1/.probe-$SID" && rm -f "$1/.probe-$SID" ) </dev/null >/dev/null 2>&1 &
  local p=$! i=0
  while [ $i -lt $((PROBE * 10)) ]; do
    kill -0 "$p" 2>/dev/null || { wait "$p"; return; }
    sleep 0.1; i=$((i + 1))
  done
  # A process in uninterruptible I/O can outlive SIGKILL, so never wait for it. disown
  # also keeps bash from printing a "Killed: 9" job line.
  pkill -9 -P "$p" 2>/dev/null; kill -9 "$p" 2>/dev/null; disown "$p" 2>/dev/null
  return 1
}
registered() {   # git prints real paths; a $HOME under /var is /private/var
  [ -d "$1" ] && git worktree list --porcelain | grep -qFx "worktree $(cd "$1" && pwd -P)"
}
if registered "$FALLBACK/$KEY/$SID"; then   # checked first: the boot disk never blocks
  WT="$FALLBACK/$KEY/$SID"
elif answers "$PRIMARY/$KEY"; then
  WT="$PRIMARY/$KEY/$SID"
else
  WT="$FALLBACK/$KEY/$SID"
  echo "NOTE: $PRIMARY did not answer in ${PROBE}s, using $FALLBACK" >&2
fi

# GitHub login, not the email local-part (often a stale handle). Cached per repo,
# never empty: git config succeeds on "" and would poison the repo until unset.
OWNER=$(git config claude.branchPrefix)
[ -n "$OWNER" ] || OWNER=$(gh api user --jq .login 2>/dev/null)
[ -n "$OWNER" ] || OWNER=$(git config user.email | cut -d@ -f1)
[ -n "$OWNER" ] || { echo "STOP: git config claude.branchPrefix YOUR_HANDLE"; exit 1; }
git config claude.branchPrefix "$OWNER"

BRANCH="$OWNER/$SLUG-$SID"
if registered "$WT"; then   # resumed session
  echo "re-attaching to existing worktree"
elif git show-ref -q --verify "refs/heads/$BRANCH"; then
  # Read from git's own records, never from the tree, which may be on the dead drive.
  HELD=$(git worktree list --porcelain | awk -v b="branch refs/heads/$BRANCH" '/^worktree /{w=substr($0,10)} $0==b{print w}')
  [ -z "$HELD" ] || { echo "STOP: $BRANCH is checked out at $HELD, which this setup cannot use; if its drive is not answering, remount it, or start this task under a new slug"; exit 1; }
  # a run killed inside `git worktree add` (a drive that stopped answering) has
  # made the branch and no tree; -b would now refuse forever
  echo "reusing branch $BRANCH"
  git worktree add "$WT" "$BRANCH" || { echo "STOP: git worktree add $WT $BRANCH failed"; exit 1; }
else
  # never report a tree we failed to make
  git worktree add -b "$BRANCH" "$WT" "$BASE" || { echo "STOP: git worktree add $WT failed"; exit 1; }
fi
echo "WORKTREE $WT"
