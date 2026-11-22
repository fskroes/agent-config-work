#!/usr/bin/env bash
# Install the work slice of agent-config on a laptop that has GitHub Copilot CLI only.
# This file is install.sh of the export (agent-config: copilot/work-install.sh). It runs
# from a clone of agent-config-work, and from the export that `install.sh --profile work`
# of agent-config writes, so the two installs are one code path.
#
#   ./install.sh [--name NAME]
#                   render the skills, hooks, scripts and instructions of this folder,
#                   link them, write the instructions file, merge the settings keys.
#                   --name writes NAME in place of every "Fernando" in what it renders
#                   and remembers it in ~/.local/state/agent-config-work/name; later
#                   runs reuse it. NAME: letters, spaces, . - ' (at most 60).
#
# Render: ~/.local/share/agent-config-work/rendered is a link to a folder with a copy of skills/,
# hooks/, scripts/ and copilot-instructions.md, the name written in. Each run makes a new folder
# and moves the link in one step, so a hook that runs during an install never sees half a copy.
# The links below point into it, never into this folder: a `git pull` changes nothing until
# this script runs again (the LaunchAgent runs it after each pull that changes HEAD).
#
# Writes only: ~/.local/share/agent-config-work/{rendered,rendered.*,install.lock}, ~/.copilot/skills/<each folder in
# skills/>, ~/.copilot/copilot-instructions.md,
# ~/.copilot/hooks/{guard.json,guard.py,agent_config.py}, ~/.claude/scripts/<each file in scripts/>, the keys of
# work-settings.json in ~/.copilot/settings.json, and the marker ~/.copilot/.agent-config-profile
# (`work`: the personal install.sh of agent-config stops on this machine). Idempotent. Anything
# it replaces is moved to ~/agent-config-work-backup-<timestamp>/, except the marker, which is
# never backed up or removed. It never moves anything into this folder.
#
# Updates: ./launchagent.sh of this folder installs the LaunchAgent com.fskroes.agent-config-work-update.
# It runs `git pull --ff-only` in this folder every hour, then this script when HEAD differs from
# the last installed commit (state: ~/.local/state/agent-config-work/{status,update.log,installed-sha}).
# That run passes no --name: the saved name is reused. agent-config-work is public, so the clone
# reads GitHub over HTTPS with no credential; the work network blocks SSH to GitHub.
set -euo pipefail
shopt -s dotglob nullglob

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP="$HOME/agent-config-work-backup-$(date +%Y%m%d-%H%M%S)"
STATE="$HOME/.local/state/agent-config-work"
NAME=""
case "${1:-}" in
  "") [[ $# -eq 0 ]] || { echo "usage: ./install.sh [--name NAME]" >&2; exit 2; } ;;
  -h|--help) sed -n '2,32p' "${BASH_SOURCE[0]}"; exit 0 ;;
  --name) [[ $# -eq 2 && -n "$2" ]] || { echo "usage: ./install.sh --name NAME" >&2; exit 2; }
          NAME="$2" ;;
  *) echo "unknown option: $1 (usage: ./install.sh [--name NAME])" >&2; exit 2 ;;
esac
# No --name: the name of an earlier `--name` run, so the hourly update keeps it.
FROM_STATE=0
[[ -n "$NAME" || ! -f "$STATE/name" ]] || { NAME="$(head -n 1 "$STATE/name")"; FROM_STATE=1; }
command -v python3 >/dev/null || { echo "  STOP: python3 is needed to render the files and edit settings.json" >&2; exit 1; }
# The name goes into Markdown, into strings and comments of guard.py and into a double-quoted
# message of a shell script. Letters, spaces, . - and ' are safe in all of them (a ' is written
# as \' in a .py file); a quote, $, \, { or a newline would break a file or a format string.
if [[ -n "$NAME" ]] && ! python3 -c 'import sys; n = sys.argv[1]
sys.exit(0 if len(n) <= 60 and n[0].isalpha() and all(c.isalpha() or c in " .-\x27" for c in n) else 1)' "$NAME"; then
  if ((FROM_STATE)); then
    echo "  STOP: the saved name in $STATE/name has a character --name does not take. Run ./install.sh --name NAME again." >&2
  else
    echo "  STOP: --name takes letters, spaces, . - and ' only, at most 60 (for example --name \"Anne-Marie O'Brien\")" >&2
  fi
  exit 2
fi

CSET="$HOME/.copilot/settings.json"
WSET="$REPO/work-settings.json"
RENDER="$HOME/.local/share/agent-config-work/rendered"

# Check everything first. A broken export must stop the run before any write.
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

# One install at a time: the hourly update and a run by hand can overlap. Two runs would race on
# the links, and the cleanup of one would delete the copy the other has just linked (the guard
# fails open, so that turns it off). macOS has no flock command: python takes the lock on
# install.lock and runs this script again, which inherits the locked file and holds it to the end.
if [[ -z "${AGENT_CONFIG_WORK_LOCKED:-}" ]]; then
  mkdir -p "$(dirname "$RENDER")"
  exec python3 -c 'import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT, 0o644)
fcntl.flock(fd, fcntl.LOCK_EX)
os.set_inheritable(fd, True)
os.execvpe("bash", ["bash"] + sys.argv[2:], dict(os.environ, AGENT_CONFIG_WORK_LOCKED="1"))' \
    "$(dirname "$RENDER")/install.lock" "${BASH_SOURCE[0]}" "$@"
fi

backup() {  # backup <path>
  local p="$1" rel="${1#"$HOME"/}"
  mkdir -p "$BACKUP/$(dirname "$rel")"
  mv "$p" "$BACKUP/$rel"
  echo "  backed up $rel"
}

link() {  # link <path-in-the-render> <path-in-home>
  local src="$RENDER/$1" dst="$2" t=""
  [[ -e "$src" ]] || { echo "  MISSING in export: $1" >&2; return 1; }
  if [[ -L "$dst" && "$(readlink "$dst")" == "$src" ]]; then
    echo "  ok      ${dst#"$HOME"/}"
    return
  fi
  # A link an install made before the render existed points into this folder: replaced, no backup.
  [[ -L "$dst" ]] && t="$(readlink "$dst")"
  if [[ -n "$t" && "$t" == "$REPO"/* ]]; then
    rm "$dst"
  elif [[ -e "$dst" || -L "$dst" ]]; then
    backup "$dst"
  fi
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

echo "render:"
python3 - "$REPO" "$RENDER" "$NAME" <<'PY' || { echo "  STOP: could not render the export into $RENDER" >&2; exit 1; }
import os, shutil, sys, tempfile
src, link, name = sys.argv[1:4]
base = os.path.dirname(link)   # this run holds install.lock here, so no other run renders now
os.makedirs(base, exist_ok=True)
new = tempfile.mkdtemp(prefix="rendered.", dir=base)
cur = ""
try:
    for top in ("skills", "hooks", "scripts", "copilot-instructions.md"):
        root = os.path.join(src, top)
        files = [root] if os.path.isfile(root) else [os.path.join(d, f) for d, _, fs in os.walk(root) for f in fs]
        for path in files:
            rel = os.path.relpath(path, src)
            if os.path.basename(path) == ".DS_Store" or "__pycache__" in rel.split(os.sep):
                continue
            if os.path.islink(path):
                sys.exit(f"  STOP: {rel} is a symlink")
            data = open(path, "rb").read()
            if name:
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    pass   # not text: copied as it is
                else:
                    data = text.replace("Fernando", name.replace("'", "\\'") if path.endswith(".py") else name).encode("utf-8")
            out = os.path.join(new, rel)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "wb") as f:
                f.write(data)
            shutil.copymode(path, out)
    os.chmod(new, 0o755)

    def tree(top):   # every file: path, mode, bytes; not the __pycache__ that a run of guard.py leaves
        return sorted((os.path.relpath(os.path.join(d, f), top), os.stat(os.path.join(d, f)).st_mode,
                       open(os.path.join(d, f), "rb").read())
                      for d, _, fs in os.walk(top) if "__pycache__" not in d.split(os.sep) for f in fs)

    # The link holds a plain folder name next to it (written below). Joined to base, not resolved:
    # the cleanup at the end compares paths of the same form ($HOME may sit behind a symlink).
    t = os.readlink(link) if os.path.islink(link) else ""
    cur = os.path.join(base, t) if t.startswith("rendered.") and "/" not in t else ""
    if cur and os.path.isdir(cur) and not os.path.islink(cur) and tree(cur) == tree(new):
        shutil.rmtree(new)   # the same copy: a rerun changes nothing
        new = cur
    else:
        tmp = link + ".tmp"
        if os.path.lexists(tmp):
            os.remove(tmp)
        os.symlink(os.path.basename(new), tmp)
        os.replace(tmp, link)   # one rename: the link points at the old copy or the new one, never at nothing
except BaseException:
    if new != cur:
        shutil.rmtree(new, ignore_errors=True)
    raise
# Older copies, and the copy of a run that died halfway. Never the one the link points at.
live = os.readlink(link)
for e in os.listdir(base):
    p = os.path.join(base, e)
    if e.startswith("rendered.") and e != live and p != new and os.path.isdir(p) and not os.path.islink(p):
        shutil.rmtree(p, ignore_errors=True)
PY
echo "  wrote   ${RENDER#"$HOME"/}${NAME:+ (for $NAME)}"
[[ -z "$NAME" ]] || { mkdir -p "$STATE"; printf '%s\n' "$NAME" > "$STATE/name"; }

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
    "$RENDER"/*|"$REPO"/*) return 0 ;;
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
# The render wrote the name in. The exported text uses the name, never he, him or his, so the
# sentences still read right (tests/test_work_export.py checks it).
CI="$HOME/.copilot/copilot-instructions.md"
RENDERED="$RENDER/copilot-instructions.md"
if [[ -f "$CI" && ! -L "$CI" ]] && cmp -s "$RENDERED" "$CI"; then
  echo "  ok      .copilot/copilot-instructions.md${NAME:+ (for $NAME)}"
else
  [[ -e "$CI" || -L "$CI" ]] && backup "$CI"
  mkdir -p "$(dirname "$CI")"
  cp "$RENDERED" "$CI"
  echo "  wrote   .copilot/copilot-instructions.md${NAME:+ (for $NAME)}"
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

echo "hourly update:"
# The update.sh of the LaunchAgent is a copy that launchagent.sh wrote. A newer launchagent.sh in
# this export rewrites it, so a fix of the updater reaches every Mac with the next export. Only
# the update.sh of this folder: an install from another copy of the export leaves it alone.
UPD="$HOME/.local/share/agent-config-work-update/update.sh"
if [[ -f "$UPD" && -f "$REPO/launchagent.sh" ]] && grep -qxF "REPO=\"$REPO\"" "$UPD"; then
  bash "$REPO/launchagent.sh" --script-only
else
  echo "  none    (./launchagent.sh of this folder installs it)"
fi

[[ -d "$BACKUP" ]] || echo "(nothing needed backing up)"
echo
echo "Done. Check these by hand on the work laptop (Copilot needs a login):"
echo "  1. copilot -> /login"
echo "  2. /skills list shows exactly the folders in skills/ of this export"
echo "  3. Ask it to run: git commit --no-verify  -> the guard must deny it"
echo "  4. Make a commit: git log -1 --format=%B has no Co-authored-by: Copilot line"
echo "To update: ./launchagent.sh installs the LaunchAgent com.fskroes.agent-config-work-update, which pulls and"
echo "  runs ./install.sh every hour (log: ~/.local/state/agent-config-work/update.log)."
echo "  By hand: git pull --ff-only && ./install.sh in this folder."
