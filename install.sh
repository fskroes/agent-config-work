#!/usr/bin/env bash
# Install the work slice of agent-config on a laptop that has GitHub Copilot CLI only.
# This file is install.sh of the export (agent-config: copilot/work-install.sh). It runs
# from a clone of agent-config-work, and from the export that `install.sh --profile work`
# of agent-config writes, so the two installs are one code path.
#
#   ./install.sh    link the skills, hooks and scripts of this folder, write the
#                   instructions file, merge the settings keys
#
# Writes only: ~/.copilot/skills/<each folder in skills/>, ~/.copilot/copilot-instructions.md,
# ~/.copilot/hooks/{guard.json,guard.py,agent_config.py}, ~/.claude/scripts/<each file in scripts/>, the keys of
# work-settings.json in ~/.copilot/settings.json, and the marker ~/.copilot/.agent-config-profile
# (`work`: the personal install.sh of agent-config stops on this machine). Idempotent. Anything
# it replaces is moved to ~/agent-config-work-backup-<timestamp>/, except the marker, which is
# never backed up or removed. It never moves anything into this folder.
#
# Updates: the LaunchAgent com.fskroes.agent-config-work-update runs `git pull --ff-only` in
# this folder every hour, then this script when HEAD differs from the last installed commit
# (state: ~/.local/state/agent-config-work/{status,update.log,installed-sha}). The clone reads
# GitHub over HTTPS with a fine-grained token from the Keychain (service agent-config-work-ro),
# through a credential helper in .git/config; the work network blocks SSH to GitHub.
set -euo pipefail
shopt -s dotglob nullglob

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP="$HOME/agent-config-work-backup-$(date +%Y%m%d-%H%M%S)"
case "${1:-}" in
  "") ;;
  -h|--help) sed -n '2,21p' "${BASH_SOURCE[0]}"; exit 0 ;;
  *) echo "unknown option: $1 (this installer takes no options)" >&2; exit 2 ;;
esac

CSET="$HOME/.copilot/settings.json"
WSET="$REPO/work-settings.json"

# Check everything first. A broken export must stop the run before any write.
command -v python3 >/dev/null || { echo "  STOP: python3 is needed to edit settings.json" >&2; exit 1; }
for f in copilot-instructions.md work-settings.json hooks/guard.json hooks/guard.py hooks/agent_config.py \
         scripts/agent-config.sh; do
  [[ -f "$REPO/$f" ]] || { echo "  STOP: missing in this export: $f" >&2; exit 1; }
done
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if isinstance(d, dict) and d else 1)' "$WSET" 2>/dev/null \
  || { echo "  STOP: work-settings.json is not a JSON object with keys" >&2; exit 1; }
WORK_NAMES=()
# Dot entries (Finder's .DS_Store in a clone) are not skills; every other entry must be one.
for d in "$REPO"/skills/*; do
  [[ "$(basename "$d")" == .* ]] && continue
  [[ -f "$d/SKILL.md" ]] || { echo "  STOP: skills/$(basename "$d") has no SKILL.md" >&2; exit 1; }
  WORK_NAMES+=("$(basename "$d")")
done
((${#WORK_NAMES[@]})) || { echo "  STOP: this export holds no skill" >&2; exit 1; }

backup() {  # backup <path>
  local p="$1" rel="${1#"$HOME"/}"
  mkdir -p "$BACKUP/$(dirname "$rel")"
  mv "$p" "$BACKUP/$rel"
  echo "  backed up $rel"
}

link() {  # link <path-in-export> <path-in-home>
  local src="$REPO/$1" dst="$2"
  [[ -e "$src" ]] || { echo "  MISSING in export: $1" >&2; return 1; }
  if [[ -L "$dst" && "$(readlink "$dst")" == "$src" ]]; then
    echo "  ok      ${dst#"$HOME"/}"
    return
  fi
  [[ -e "$dst" || -L "$dst" ]] && backup "$dst"
  mkdir -p "$(dirname "$dst")"
  ln -s "$src" "$dst"
  echo "  linked  ${dst#"$HOME"/}"
}

echo "export:  $REPO"
echo "backups: $BACKUP"
echo "profile: work (GitHub Copilot CLI only)"

echo "profile marker:"
# The personal profile of agent-config's install.sh reads this file and stops when it holds
# `work`. It is written first, so an install that stops halfway still guards the machine.
# It is never backed up and never removed: removing it would let the personal install run here.
MARKER="$HOME/.copilot/.agent-config-profile"
if [[ -f "$MARKER" && "$(cat "$MARKER")" == work ]]; then
  echo "  ok      .copilot/.agent-config-profile"
else
  mkdir -p "$(dirname "$MARKER")"
  printf 'work\n' > "$MARKER"
  echo "  wrote   .copilot/.agent-config-profile (work)"
fi

echo "copilot skills:"
# ours <link>: a skill link this install, or an earlier one, made:
# - a link into this folder;
# - a link into an agent-config clone (.../claude/skills/<name>, .../codex/skills/<name>): what
#   the work profile linked before the export existed. Dead: that clone moved or is gone. Live:
#   only when the target folder is an agent-config clone (it has copilot/work-skills.txt), so a
#   link into another repo with the same layout stays. The old clone is deleted after this
#   install has moved its links here;
# - a dead link into an export that moved (a clone of agent-config-work, or .work-export).
# A live link anywhere else, or a real folder, belongs to another tool.
ours() {
  [[ -L "$1" ]] || return 1
  local t; t="$(readlink "$1")"
  [[ "$t" == /* ]] || t="$(dirname "$1")/$t"   # a relative target is relative to the link's folder
  case "$t" in
    "$REPO"/*) return 0 ;;
    */claude/skills/*|*/codex/skills/*) [[ ! -e "$1" || -f "${t%/*/skills/*}/copilot/work-skills.txt" ]] ;;
    *agent-config-work*/skills/*|*/.work-export/skills/*) [[ ! -e "$1" ]] ;;
    *) return 1 ;;
  esac
}
for name in "${WORK_NAMES[@]}"; do
  dst="$HOME/.copilot/skills/$name"
  # A skill of the same name that is not ours was installed by someone else
  # (a plugin, the employer, by hand). It keeps its place.
  if [[ -e "$dst" || -L "$dst" ]] && ! ours "$dst"; then
    echo "  WARN    .copilot/skills/$name is taken by a skill that is not from this export, left alone" >&2
    continue
  fi
  link "skills/$name" "$dst"
done
# Our links that are not in this export go away. Anything else in the
# directory (real folders, live links into other tools) is not ours.
for e in "$HOME/.copilot/skills"/*; do
  ours "$e" || continue
  name="$(basename "$e")"
  listed=0
  for n in "${WORK_NAMES[@]}"; do [[ "$n" == "$name" ]] && listed=1; done
  ((listed)) && continue
  rm "$e"
  echo "  removed .copilot/skills/$name (our link, not in this export)"
done

echo "copilot instructions:"
# A real file, not a link: Copilot must find it when this folder moves.
CI="$HOME/.copilot/copilot-instructions.md"
if [[ -f "$CI" && ! -L "$CI" ]] && cmp -s "$REPO/copilot-instructions.md" "$CI"; then
  echo "  ok      .copilot/copilot-instructions.md"
else
  [[ -e "$CI" || -L "$CI" ]] && backup "$CI"
  mkdir -p "$(dirname "$CI")"
  cp "$REPO/copilot-instructions.md" "$CI"
  echo "  wrote   .copilot/copilot-instructions.md"
fi

echo "copilot hooks, worktree scripts, capped_run.py:"
link hooks/guard.json "$HOME/.copilot/hooks/guard.json"
link hooks/guard.py   "$HOME/.copilot/hooks/guard.py"
link hooks/agent_config.py "$HOME/.copilot/hooks/agent_config.py"   # guard.py imports it
for f in "$REPO"/scripts/*; do
  [[ "$(basename "$f")" == .* ]] && continue
  link "scripts/$(basename "$f")" "$HOME/.claude/scripts/$(basename "$f")"
done

echo "copilot settings.json:"
# work-settings.json is merged in, key by key: a nested object is merged,
# any other value replaces the one in settings.json. Keys it does not name stay.
# - includeCoAuthoredBy: false. Copilot adds a "Co-authored-by: Copilot" trailer
#   to commits; the guard denies it, so without this key every commit is blocked.
# - subagents.agents.<name>: model and effort of each built-in sub-agent. With no
#   entry, Copilot picks the sub-agent model itself. There is no key for all
#   sub-agents at once, so each one is named.
status=0
NEW="$(python3 - "$CSET" "$WSET" <<'PY'
import copy, json, os, sys
path, want = sys.argv[1], json.load(open(sys.argv[2]))
if not os.path.exists(path) or not open(path).read().strip():   # missing or empty: start fresh
    data = {}
else:
    try:
        data = json.load(open(path))
    except ValueError:
        sys.exit(20)
    if not isinstance(data, dict):
        sys.exit(20)

def merge(dst, src):
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            merge(dst[k], v)
        else:
            dst[k] = copy.deepcopy(v)

old = json.dumps(data, sort_keys=True)   # text, not ==: in Python 0 == False and 1 == True
merge(data, want)
if json.dumps(data, sort_keys=True) == old:
    sys.exit(0)
print(json.dumps(data, indent=2, ensure_ascii=False))
sys.exit(10)
PY
)" || status=$?
case "$status" in
  0)  echo "  ok      .copilot/settings.json" ;;
  10) [[ -e "$CSET" ]] && backup "$CSET"
      mkdir -p "$(dirname "$CSET")"
      printf '%s\n' "$NEW" > "$CSET"
      echo "  wrote   .copilot/settings.json (keys from work-settings.json)" ;;
  20) echo "  WARN    .copilot/settings.json is not valid JSON, left alone. Merge work-settings.json into it by hand." >&2 ;;
  *)  echo "  WARN    could not read .copilot/settings.json (python3 exit $status), left alone." >&2 ;;
esac

[[ -d "$BACKUP" ]] || echo "(nothing needed backing up)"
echo
echo "Done. Check these by hand on the work laptop (Copilot needs a login):"
echo "  1. copilot -> /login"
echo "  2. /skills list shows exactly the folders in skills/ of this export"
echo "  3. Ask it to run: git commit --no-verify  -> the guard must deny it"
echo "  4. Make a commit: git log -1 --format=%B has no Co-authored-by: Copilot line"
echo "To update: the LaunchAgent com.fskroes.agent-config-work-update pulls and runs ./install.sh every hour"
echo "  (log: ~/.local/state/agent-config-work/update.log). By hand: git pull --ff-only && ./install.sh in this folder."
