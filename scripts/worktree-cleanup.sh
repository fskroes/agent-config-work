#!/usr/bin/env bash
# Remove session worktrees whose work has landed. A manual command, never part of
# setup: a sweep that runs automatically eventually runs while somebody is mid-task.
# Run it from the shared checkout when Fernando asks. Rules in AGENTS.md, "Branching".
#
#   worktree-cleanup.sh            remove landed, clean, pushed worktrees; print what it skipped
#
# Two kinds of worktree: a session's, under ~/.claude-worktrees/ (or the target of that link), and an Agent-tool
# builder's, under <repo>/.claude/worktrees/ (also inside a session worktree).
#
# What it removes:
# - Only worktrees that were pushed under their own branch name (an upstream alone
#   proves nothing, `worktree add` sets one at once), whose work has landed by
#   ancestry or by patch id (rebase and squash merges replay the work as a new
#   commit), and that hold no modified, untracked or ignored files (`worktree remove`
#   deletes ignored files without complaint, and ignored is where bootstrap put .env).
# - Four kinds of ignored path hold no work and do not keep a worktree: the caches
#   __pycache__/, .pytest_cache/ and SwiftPM .build/, which every test run rebuilds;
#   .venv, as a directory `uv sync` rebuilds or as a link to a shared venv (a plain
#   file named .venv still keeps the worktree); node_modules, as the bootstrap symlink
#   or as an install the lockfile rebuilds (under a node_modules/ or .venv/ rule a link
#   is untracked, so the script deletes the link itself, never its target, just before
#   the remove); and a folder with no file in it. For any other ignored file it names
#   the first three paths, so a .env and a generated project are told apart.
# - It needs a remote named origin, and stops with the remote names when there is none.
# - A multi-commit squash matches no single patch, so for that case it asks gh for a
#   PR from this repo, merged into the base, whose head is the branch's local tip;
#   without gh such a worktree is kept.
# - Agent-tool worktrees are never pushed, so for them the push rule becomes a commit
#   rule: the branch reflog must show a commit of its own, or a builder that just
#   started, still on the base, would count as landed and lose its tree.
# - It never removes a locked worktree (a lock can mean a running agent) and prints the
#   `git worktree unlock` command. It never removes the tree you stand in.
#
# Each `continue` is a bug that bit.
set -u
HERE=$(git rev-parse --show-toplevel)
# Without a remote named origin, origin/HEAD can never resolve, and "origin/HEAD is
# unset" sent the reader to the wrong fix (one repo has only `mini`,
# 2026-10-23). Name the remotes; never guess which one is the base.
if ! git remote get-url origin >/dev/null 2>&1; then
  r=$(git remote | paste -sd, - | sed 's/,/, /g')
  echo "STOP: no remote named origin (remotes: ${r:-none})"; exit 1
fi
git fetch -q origin 2>/dev/null
git remote set-head -a origin >/dev/null 2>&1
BASE=$(git symbolic-ref -q --short refs/remotes/origin/HEAD) || { echo "STOP: origin/HEAD is unset"; exit 1; }
# ~/.claude-worktrees can be a link (to /Volumes/Data/claude-worktrees), and git lists a
# worktree made through the target by the target's path, which has no .claude-worktrees in
# it: 13 were skipped with no word on 2026-11-15. readlink, not cd: it reads the link in
# $HOME and never touches the target, so a drive that stopped answering cannot block here.
ROOTLINK=$(readlink "$HOME/.claude-worktrees" 2>/dev/null)
case "$ROOTLINK" in ""|/) ROOTLINK="" ;; /*) ROOTLINK=${ROOTLINK%/} ;; *) ROOTLINK="$HOME/${ROOTLINK%/}" ;; esac
# One line per worktree: path, a tab, the lock reason ("-" when not locked; a lock
# with no reason prints "locked").
git worktree list --porcelain | awk '
  /^worktree /{w=substr($0,10); l="-"}
  /^locked/{l=(length($0)>7 ? substr($0,8) : "locked")}
  /^$/{if (w!="") print w "\t" l; w=""}
  END{if (w!="") print w "\t" l}' |
  awk -v r="$ROOTLINK/" -F'\t' 'index($1, "/.claude-worktrees/") || index($1, "/.claude/worktrees/") ||
    (r != "/" && index($1, r) == 1)' | while IFS=$'\t' read -r w lock; do
    [ "$w" = "$HERE" ] && continue                       # never the tree you are standing in
    b=$(git -C "$w" branch --show-current); [ -n "$b" ] || { echo "kept $w (detached)"; continue; }
    if [[ "$w" == */.claude/worktrees/* ]]; then
      # An Agent-tool builder never pushes, so the push test below would keep every
      # one: 12 tournament worktrees in one repo stayed with no word on
      # 2026-10-23. Its own commits are the proof of work: a builder that just
      # started sits on the base, counts as landed by ancestry, and would lose its
      # tree mid-task. The branch reflog says whether it ever committed; a branch
      # with no reflog left (deleted, or expired by gc) proves nothing and stays.
      log=$(git reflog show --format=%gs "refs/heads/$b" 2>/dev/null)
      [ -n "$log" ] || { echo "kept $w (agent worktree, no reflog for $b)"; continue; }
      printf '%s\n' "$log" | grep -qE '^(commit|cherry-pick|revert)' \
        || { echo "kept $w (agent worktree, no commits of its own)"; continue; }
    else
      # Not merely "has an upstream": worktree add sets it immediately, so the weak
      # test passes for a session that has done nothing and the sweep eats live work.
      up=$(git -C "$w" rev-parse --abbrev-ref "@{upstream}" 2>/dev/null)
      [ "$up" = "origin/$b" ] || { echo "kept $w (never pushed as $b)"; continue; }
    fi
    # "Landed" is not "is an ancestor": rebase and squash merges replay the work as
    # a new commit, so ancestry alone keeps every merged worktree forever. cherry
    # compares patch ids and still prints + for unlanded work. A multi-commit squash
    # matches no single patch, so it kept all 30 FriendZone worktrees on 2026-10-22.
    # Then ask GitHub: a PR from this repo, merged into the base, whose head is the
    # local tip. A commit after the merge moves the tip and keeps the tree; a PR
    # merged into another branch, or from a fork with the same branch name, does
    # not count. No gh, or gh failing, prints nothing and keeps the tree.
    tip=$(git rev-parse "$b")
    git merge-base --is-ancestor "$b" "$BASE" \
      || [ -z "$(git cherry "$BASE" "$b" | grep '^+')" ] \
      || gh pr list --head "$b" --base "${BASE#origin/}" --state merged --limit 100 \
           --json headRefOid,isCrossRepository \
           --jq '.[] | select(.isCrossRepository | not) | .headRefOid' 2>/dev/null </dev/null | grep -qx "$tip" \
      || { echo "kept $w (not landed yet)"; continue; }
    # worktree remove refuses on modified/untracked files but deletes IGNORED ones
    # without complaint, and ignored is exactly where bootstrap put .env. Four
    # kinds of ignored path hold no work and do not keep a worktree:
    # - caches every run rebuilds: __pycache__/ (keeping a landed worktree for it
    #   meant the sweep kept every Python worktree forever), .pytest_cache/, and
    #   SwiftPM's .build/ (every builder of an iOS app had one);
    # - .venv: a directory `uv sync` rebuilds, or a link to a shared venv (remove
    #   deletes the link, not what it points at). A Python repo kept the
    #   worktrees of 3 merged PRs for services/*/.venv/ on 2026-10-18. A link
    #   prints with no slash, like a file, so only a real link passes: a plain
    #   file named .venv is pipenv's pointer, which a person writes;
    # - node_modules: the bootstrap symlink (remove deletes the link, not what it
    #   points at) or an install the lockfile rebuilds (one repo, 2026-10-23);
    # - a folder with no file in it: =matching lists an ignored folder even when
    #   it is empty, and an empty reports/ kept 4 worktrees of one repo.
    # Under a `node_modules/` or `.venv/` rule a link is a file to git, so it
    # shows as untracked (`??`), and worktree remove refuses untracked files: the
    # link is collected here and deleted, the link alone, just before the remove.
    # =matching prints the path that matched the ignore rule (tests/__pycache__/
    # for a `__pycache__/` rule, tests/__pycache__/x.pyc for a `*.pyc` rule); the
    # default collapses a directory with nothing tracked in it to `tests/`. -z
    # keeps a path with a space unquoted. The first three paths go in the message:
    # "files left behind" alone does not say whether it is a .env or a generated
    # project.
    left="" links=""
    while IFS= read -r -d '' e; do
      st=${e:0:2}; f=${e:3}
      # -z gives a rename or copy a second record, the old path, with no status
      [[ "$st" == [RC]? ]] && IFS= read -r -d '' _
      # The links are kept one per line, so a path with a newline in it would
      # split into other paths for the rm below: it is never exempt.
      if [[ "$f" =~ (^|/)(node_modules|\.venv)/?$ && "$f" != *$'\n'* ]]; then
        n="$w/${f%/}"
        [ "$st" = "??" ] && [ -L "$n" ] && { links="$links${links:+$'\n'}${f%/}"; continue; }
        [ "$st" = "!!" ] && { [ -L "$n" ] || [ -d "$n" ]; } && continue
      fi
      if [ "$st" = "!!" ]; then
        [[ "$f" =~ (^|/)(__pycache__|\.pytest_cache|\.build|\.venv)/ ]] && continue
        [[ "$f" == */ ]] && [ -z "$(find "$w/$f" ! -type d -print 2>/dev/null | head -1)" ] && continue
      fi
      left="$left${left:+$'\n'}$f"
    done < <(git -C "$w" status --porcelain -z --ignored=matching)
    [ -n "$left" ] && { echo "kept $w (files left behind: $(printf '%s\n' "$left" | head -3 | paste -sd, - | sed 's/,/, /g'))"; continue; }
    # A lock can mean a running agent. worktree remove refuses it with a message
    # on stderr only, so say what holds it and how to let it go.
    [ "$lock" = "-" ] || { echo "kept $w (locked: $lock; git worktree unlock \"$w\" once its agent is done)"; continue; }
    printf '%s\n' "$links" | while IFS= read -r l; do
      [[ "$l" =~ (^|/)(node_modules|\.venv)$ ]] && [ -L "${w:?}/$l" ] && rm "${w:?}/$l"
    done
    if git worktree remove "$w"; then echo "removed $w"
    elif [ -n "$links" ]; then echo "kept $w (worktree remove failed; its node_modules or .venv link was deleted)"; fi
  done
git worktree prune
