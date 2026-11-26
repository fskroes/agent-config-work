#!/usr/bin/env bash
set -u
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || printf %s "${BASH_SOURCE[0]}")")/agent-config.sh" \
  || { echo "STOP: agent-config.sh is missing next to this script, run install.sh again"; exit 1; }
REPO_PATH=${1:-}
SLUG=${2:-}
stop() { echo "STOP: $*"; exit 1; }
USAGE="usage: worktree-child.sh <repo-path> <slug> [--variant <a-z0-9, up to 8>]"
[ -n "$REPO_PATH" ] && [ -n "$SLUG" ] || stop "$USAGE"
VARIANT=
if [ $# -gt 2 ]; then
  [ $# -eq 4 ] && [ "$3" = --variant ] || stop "$USAGE"
  VARIANT=$4
  case "$VARIANT" in ''|*[!a-z0-9]*|?????????*) stop "$USAGE" ;; esac
fi

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

SID=${CLAUDE_CODE_SESSION_ID:-${COPILOT_AGENT_SESSION_ID:-}}
[ -n "$SID" ] || stop "CLAUDE_CODE_SESSION_ID and COPILOT_AGENT_SESSION_ID are unset, the worktree cannot be recorded for a session"
KEY=$(printf %s "$SID" | LC_ALL=C tr -c 'A-Za-z0-9_-' '_')

D=$REPO_PATH
while [ ! -d "$D" ]; do
  [ "$D" = "$(dirname "$D")" ] && break
  D=$(dirname "$D")
done
CHILD_COMMON=$(git -C "$D" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) \
  || stop "$REPO_PATH is not in a git repo"
CHILD_COMMON=$(cd "$CHILD_COMMON" && pwd -P)
[ "$CHILD_COMMON" != "$HOME_COMMON" ] || stop "$REPO_PATH is in the Home Repo itself: use worktree-setup.sh there"
CHILD_ROOT=$(dirname "$CHILD_COMMON")

OUT=$(cd "$CHILD_ROOT" && bash "$HERE/worktree-setup.sh" "$SLUG" 2>&1); RC=$?
STOPLINE=$(printf '%s\n' "$OUT" | grep '^STOP:' | tail -1)
if [ $RC -ne 0 ] || [ -n "$STOPLINE" ]; then
  printf '%s\n' "${STOPLINE:-STOP: worktree-setup.sh failed in $CHILD_ROOT: $OUT}"
  exit 1
fi
printf '%s\n' "$OUT" | grep -v '^\(WORKTREE\|SOLO\) '
WT=$(printf '%s\n' "$OUT" | sed -n 's/^WORKTREE //p' | tail -1)
if [ -z "$WT" ] && printf '%s\n' "$OUT" | grep -q '^SOLO '; then WT=$CHILD_ROOT; fi
[ -n "$WT" ] && [ -d "$WT" ] || stop "worktree-setup.sh gave no worktree path: $OUT"
WT=$(cd "$WT" && pwd -P)

if [ -n "$VARIANT" ]; then
  BR=$(git -C "$WT" branch --show-current)
  [ -n "$BR" ] || stop "$WT has no branch checked out, a variant cannot branch from it"
  if [ "$WT" = "$CHILD_ROOT" ]; then
    SID8=${KEY:0:8}
    VWT="$HOME/.claude-worktrees/$(basename "$CHILD_ROOT")-$(printf %s "$CHILD_ROOT" | cksum | cut -d' ' -f1)/$SID8-$VARIANT"
  else
    VWT="$WT-$VARIANT"
  fi
  VBR="$BR-$VARIANT"
  if [ -d "$VWT" ] && git -C "$WT" worktree list --porcelain | grep -qFx "worktree $(cd "$VWT" && pwd -P)"; then
    echo "re-attaching to variant worktree $VWT"
  elif [ -e "$VWT" ]; then
    stop "$VWT exists and is no worktree of $CHILD_ROOT, move it away first"
  elif git -C "$WT" show-ref -q --verify "refs/heads/$VBR"; then
    git -C "$WT" worktree add -q "$VWT" "$VBR" >/dev/null 2>&1 || stop "git worktree add $VWT $VBR failed (is $VBR checked out elsewhere?)"
    echo "variant $VARIANT: reusing branch $VBR"
  else
    mkdir -p "$(dirname "$VWT")" || stop "cannot make $(dirname "$VWT")"
    git -C "$WT" worktree add -q -b "$VBR" "$VWT" HEAD >/dev/null 2>&1 || stop "git worktree add -b $VBR $VWT failed"
    echo "variant $VARIANT: branch $VBR from $BR at $(git -C "$WT" rev-parse --short HEAD)"
  fi
  VWT=$(cd "$VWT" && pwd -P)
fi

REC="$HOME_COMMON/claude-child-worktrees/$KEY"
mkdir -p "$HOME_COMMON/claude-child-worktrees" || stop "cannot write in $HOME_COMMON"
touch "$REC"
for W in "$WT" ${VARIANT:+"$VWT"}; do
  grep -qxF "$W" "$REC" || printf '%s\n' "$W" >> "$REC"
done
[ -z "${T3CODE_THREAD_ID:-}" ] || echo "THREAD-TITLE $(basename "$CHILD_ROOT")"
echo "CHILD ${VWT:-$WT}"
