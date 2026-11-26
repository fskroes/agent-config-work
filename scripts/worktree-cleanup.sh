#!/usr/bin/env bash
set -u
HERE=$(git rev-parse --show-toplevel)
if ! git remote get-url origin >/dev/null 2>&1; then
  r=$(git remote | paste -sd, - | sed 's/,/, /g')
  echo "STOP: no remote named origin (remotes: ${r:-none})"; exit 1
fi
git fetch -q origin 2>/dev/null
git remote set-head -a origin >/dev/null 2>&1
BASE=$(git symbolic-ref -q --short refs/remotes/origin/HEAD) || { echo "STOP: origin/HEAD is unset"; exit 1; }
ROOTLINK=$(readlink "$HOME/.claude-worktrees" 2>/dev/null)
case "$ROOTLINK" in ""|/) ROOTLINK="" ;; /*) ROOTLINK=${ROOTLINK%/} ;; *) ROOTLINK="$HOME/${ROOTLINK%/}" ;; esac
git worktree list --porcelain | awk '
  /^worktree /{w=substr($0,10); l="-"}
  /^locked/{l=(length($0)>7 ? substr($0,8) : "locked")}
  /^$/{if (w!="") print w "\t" l; w=""}
  END{if (w!="") print w "\t" l}' |
  awk -v r="$ROOTLINK/" -F'\t' 'index($1, "/.claude-worktrees/") || index($1, "/.claude/worktrees/") ||
    (r != "/" && index($1, r) == 1)' | while IFS=$'\t' read -r w lock; do
    [ "$w" = "$HERE" ] && continue
    b=$(git -C "$w" branch --show-current); [ -n "$b" ] || { echo "kept $w (detached)"; continue; }
    if [[ "$w" == */.claude/worktrees/* ]]; then
      log=$(git reflog show --format=%gs "refs/heads/$b" 2>/dev/null)
      [ -n "$log" ] || { echo "kept $w (agent worktree, no reflog for $b)"; continue; }
      printf '%s\n' "$log" | grep -qE '^(commit|cherry-pick|revert)' \
        || { echo "kept $w (agent worktree, no commits of its own)"; continue; }
    else
      up=$(git -C "$w" rev-parse --abbrev-ref "@{upstream}" 2>/dev/null)
      [ "$up" = "origin/$b" ] || { echo "kept $w (never pushed as $b)"; continue; }
    fi
    tip=$(git rev-parse "$b")
    git merge-base --is-ancestor "$b" "$BASE" \
      || [ -z "$(git cherry "$BASE" "$b" | grep '^+')" ] \
      || gh pr list --head "$b" --base "${BASE#origin/}" --state merged --limit 100 \
           --json headRefOid,isCrossRepository \
           --jq '.[] | select(.isCrossRepository | not) | .headRefOid' 2>/dev/null </dev/null | grep -qx "$tip" \
      || { echo "kept $w (not landed yet)"; continue; }
    left="" links=""
    while IFS= read -r -d '' e; do
      st=${e:0:2}; f=${e:3}
      [[ "$st" == [RC]? ]] && IFS= read -r -d '' _
      if [[ "$f" =~ (^|/)(node_modules|\.venv)/?$ && "$f" != *$'\n'* ]]; then
        n="$w/${f%/}"
        [ "$st" = "??" ] && [ -L "$n" ] && { links="$links${links:+$'\n'}${f%/}"; continue; }
        [ "$st" = "!!" ] && { [ -L "$n" ] || [ -d "$n" ]; } && continue
      fi
      if [ "$st" = "!!" ]; then
        [[ "$f" =~ (^|/)(__pycache__|\.pytest_cache|\.ruff_cache|\.build|\.venv)/ ]] && continue
        [[ "$f" == */ ]] && [ -z "$(find "$w/$f" ! -type d -print 2>/dev/null | head -1)" ] && continue
      fi
      left="$left${left:+$'\n'}$f"
    done < <(git -C "$w" status --porcelain -z --ignored=matching)
    [ -n "$left" ] && { echo "kept $w (files left behind: $(printf '%s\n' "$left" | head -3 | paste -sd, - | sed 's/,/, /g'))"; continue; }
    [ "$lock" = "-" ] || { echo "kept $w (locked: $lock; git worktree unlock \"$w\" once its agent is done)"; continue; }
    printf '%s\n' "$links" | while IFS= read -r l; do
      [[ "$l" =~ (^|/)(node_modules|\.venv)$ ]] && [ -L "${w:?}/$l" ] && rm "${w:?}/$l"
    done
    if git worktree remove "$w"; then echo "removed $w"
    elif [ -n "$links" ]; then echo "kept $w (worktree remove failed; its node_modules or .venv link was deleted)"; fi
  done
git worktree prune
