#!/usr/bin/env bash
# The guard: am I about to write in the shared checkout? Run before the first write of
# every task and after any compaction. Rules in AGENTS.md, "Branching".
#
# Exit 0 and print OK <dir> when writing here is allowed (solo repo, or a linked worktree).
# Exit 1 and print WRONG TREE when this is the shared checkout of a team repo.
#
# In the shared checkout git-dir and git-common-dir are the same directory; in any
# linked worktree they differ. No cd, no $HOME, so a symlinked path cannot fool it.
# --path-format=absolute is load-bearing: from a subdirectory the common dir comes
# back relative ("../.git"), the two stop matching, and the guard goes silent in
# the shared checkout, which is the one direction that must never happen.
set -u
if [ "$(git config claude.mode)" = solo ]; then
  echo "OK $(git rev-parse --show-toplevel) (solo repo)"; exit 0
fi
if [ "$(git rev-parse --path-format=absolute --git-dir)" != "$(git rev-parse --path-format=absolute --git-common-dir)" ]; then
  echo "OK $(git rev-parse --show-toplevel) (linked worktree)"; exit 0
fi
echo "WRONG TREE: you are in the shared checkout, set up a worktree (worktree-setup.sh <slug>)"
exit 1
