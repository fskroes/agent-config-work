#!/usr/bin/env bash
set -u

usage() {
  cat <<'EOF'
Usage: repo-hooks.sh install|uninstall|status [repo ...]

Puts the commit-msg and pre-commit hooks of repo_check.py into git repos, so the rules of AGENTS.md
hold for every agent and person, not only inside Claude Code. With no repo: every git repo under
$AGENT_REPOS (default ~/dev), one level down.

  install    Writes a small shim for commit-msg and pre-commit. A hook that is already there is kept as
             <hook>.pre-agent and runs first; if it fails, the commit fails. A second install changes
             nothing. A hooks path that the repo tracks (.husky/, .githooks/) is never written: it is
             shared with the team and the next install of that tool overwrites it. There the check
             is a config hook instead (git 2.54 or newer): hook.agent-config-repo-check-<hook>.event and
             .command in .git/config of the repo, which git runs next to the tracked hooks. Nothing the
             repo tracks changes. An older git skips the repo and says so; there only the guard (Claude Code, Copilot) checks a commit.
  uninstall  Removes the shims and the config hooks, and puts the kept hooks back.
  status     One line per repo and its mechanism: installed (shim), installed (config hook), not installed,
             or skipped with the reason.

The hooks folder is core.hooksPath when set, else the hooks folder of the repo. A shim whose script
(~/.claude/scripts/repo_check.py) is missing passes and says so on stderr.
The checks, the trailers that exempt a commit and the skip rules are in `repo_check.py --help`.
Both profiles run this: install.sh on the personal machines, and the work install of agent-config-work
(copilot/work-install.sh) for the repos under $AGENT_REPOS or the paths given to it.
EOF
}

MARK="agent-config-repo-hook"
HOOKS=(commit-msg pre-commit)
CONFIG_PREFIX="agent-config-repo-check"

shim() {
  cat <<EOF
#!/bin/sh
: $MARK v1
d=\$(dirname "\$0")
if [ -x "\$d/$1.pre-agent" ]; then "\$d/$1.pre-agent" "\$@" || exit \$?; fi
s="\$HOME/.claude/scripts/repo_check.py"
if [ ! -f "\$s" ]; then echo "repo_check: \$s is missing, check skipped" >&2; exit 0; fi
command -v python3 >/dev/null 2>&1 || { echo "repo_check: python3 is missing, check skipped" >&2; exit 0; }
exec python3 "\$s" $1 "\$@"
EOF
}

git_has_config_hooks() {
  local major minor
  read -r major minor < <(git version | sed -n 's/^git version \([0-9]*\)\.\([0-9]*\).*/\1 \2/p')
  [[ "${major:-0}" -gt 2 || ( "${major:-0}" -eq 2 && "${minor:-0}" -ge 54 ) ]]
}

config_command() {
  cat <<EOF
sh -c 's="\$HOME/.claude/scripts/repo_check.py"; [ -f "\$s" ] || { echo "repo_check: \$s is missing, check skipped" >&2; exit 0; }; command -v python3 >/dev/null 2>&1 || { echo "repo_check: python3 is missing, check skipped" >&2; exit 0; }; exec python3 "\$s" "\$@"' repo-check $1
EOF
}

install_config_hook() {
  local repo="$1" h="$2" key cmd cur
  key="hook.$CONFIG_PREFIX-$h"
  cmd="$(config_command "$h")"
  cur="$(git -C "$repo" config --local --get "$key.command" 2>/dev/null || true)"
  if [[ "$cur" == "$cmd" && "$(git -C "$repo" config --local --get-all "$key.event" 2>/dev/null)" == "$h" ]]; then
    echo same; return
  fi
  git -C "$repo" config --local --replace-all "$key.event" "$h" \
    && git -C "$repo" config --local --replace-all "$key.command" "$cmd" \
    || { echo "error:cannot write the git config of $repo"; return; }
  [[ -n "$cur" ]] && echo updated || echo new
}

uninstall_config_hook() {
  local repo="$1" h="$2" key="hook.$CONFIG_PREFIX-$2"
  if git -C "$repo" config --local --get "$key.command" >/dev/null 2>&1; then
    git -C "$repo" config --local --remove-section "$key" && echo removed || echo none
  else
    echo none
  fi
}

config_installed() {
  local repo="$1" h have=""
  for h in "${HOOKS[@]}"; do
    git -C "$repo" config --local --get "hook.$CONFIG_PREFIX-$h.command" >/dev/null 2>&1 && have="$have${have:+, }$h"
  done
  echo "$have"
}

is_ours() { [[ -f "$1" ]] && grep -q "$MARK" "$1" 2>/dev/null; }

hooks_dir() {
  local repo="$1" top path common rel par
  top="$(git -C "$repo" rev-parse --show-toplevel 2>/dev/null)" || { echo "SKIP:not a git repo"; return; }
  path="$(git -C "$repo" config --get core.hooksPath 2>/dev/null || true)"
  if [[ -z "$path" ]]; then
    common="$(git -C "$repo" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" || { echo "SKIP:no git dir"; return; }
    echo "$common/hooks"
    return
  fi
  case "$path" in "~"*) path="$HOME${path#\~}" ;; esac
  [[ "$path" == /* ]] || path="$top/$path"
  if [[ "$path" == "$top"/* ]]; then
    rel="${path#"$top"/}"
    par="$(dirname "$rel")"
    if [[ -n "$(git -C "$top" ls-files -- "$rel" | head -1)" ]] \
       || { [[ "$par" != . ]] && [[ -n "$(git -C "$top" ls-files -- "$par" | head -1)" ]]; }; then
      echo "CONFIG:tracked hooks path $rel"
      return
    fi
  fi
  echo "$path"
}

install_hook() {
  local dir="$1" h="$2" f chained new
  f="$dir/$h"; chained="$dir/$h.pre-agent"
  new="$(shim "$h")"
  if is_ours "$f"; then
    [[ "$(cat "$f")" == "$new" ]] && { echo same; return; }
    printf '%s\n' "$new" > "$f"; chmod +x "$f"; echo updated; return
  fi
  if [[ -e "$f" || -L "$f" ]]; then
    [[ -e "$chained" || -L "$chained" ]] && { echo "conflict:$h.pre-agent already exists"; return; }
    mv "$f" "$chained" || { echo "error:cannot move $h"; return; }
    printf '%s\n' "$new" > "$f" && chmod +x "$f" && { echo chained; return; }
    mv "$chained" "$f"; echo "error:cannot write $h"; return
  fi
  printf '%s\n' "$new" > "$f" && chmod +x "$f" && echo new || echo "error:cannot write $h"
}

uninstall_hook() {
  local dir="$1" h="$2" f chained
  f="$dir/$h"; chained="$dir/$h.pre-agent"
  if is_ours "$f"; then
    rm -f "$f"
    if [[ -e "$chained" || -L "$chained" ]]; then mv "$chained" "$f"; echo restored; else echo removed; fi
  else
    echo none
  fi
}

each_repo() {
  local root="${AGENT_REPOS:-$HOME/dev}" g
  if (($# > 0)); then printf '%s\n' "$@"; return; fi
  [[ -d "$root" ]] || return 0
  for g in "$root"/*/.git; do
    [[ -e "$g" ]] && dirname "$g"
  done
}

run_install() {
  local repo dir res h results notes name n_new=0 n_same=0 n_skip=0 n_err=0 chained
  while IFS= read -r repo; do
    name="$(basename "$repo")"
    dir="$(hooks_dir "$repo")"
    if [[ "$dir" == CONFIG:* ]]; then
      if ! git_has_config_hooks; then
        printf '  skipped    %s (%s; git %s has no config hooks, they need 2.54)\n' "$name" "${dir#CONFIG:}" "$(git version | awk '{print $3}')"
        n_skip=$((n_skip + 1)); continue
      fi
      results=""
      for h in "${HOOKS[@]}"; do results="$results $(install_config_hook "$repo" "$h")"; done
      if [[ "$results" == *error* ]]; then
        printf '  error      %s (cannot write the git config)\n' "$name"; n_err=$((n_err + 1))
      elif [[ "$results" == *new* || "$results" == *updated* ]]; then
        printf '  installed  %s (config hook, %s)\n' "$name" "${dir#CONFIG:}"; n_new=$((n_new + 1))
      else
        printf '  ok         %s\n' "$name"; n_same=$((n_same + 1))
      fi
      continue
    fi
    if [[ "$dir" == SKIP:* ]]; then
      printf '  skipped    %s (%s)\n' "$name" "${dir#SKIP:}"; n_skip=$((n_skip + 1)); continue
    fi
    mkdir -p "$dir" 2>/dev/null || { printf '  error      %s (cannot make %s)\n' "$name" "$dir"; n_err=$((n_err + 1)); continue; }
    results=""; notes=""; chained=""
    for h in "${HOOKS[@]}"; do
      res="$(install_hook "$dir" "$h")"
      case "$res" in
        conflict:*|error:*) notes="$notes ${res#*:};" ; results="$results err" ;;
        chained) chained="$chained $h"; results="$results new" ;;
        *) results="$results $res" ;;
      esac
    done
    [[ -n "$chained" ]] && notes="$notes chained:$chained;"
    if [[ "$results" == *err* ]]; then
      printf '  error      %s (%s)\n' "$name" "${notes# }"; n_err=$((n_err + 1))
    elif [[ "$results" == *new* || "$results" == *updated* ]]; then
      printf '  installed  %s%s\n' "$name" "${notes:+ (${notes# })}"; n_new=$((n_new + 1))
    else
      printf '  ok         %s\n' "$name"; n_same=$((n_same + 1))
    fi
  done < <(each_repo "$@")
  echo "repo hooks: $n_new installed, $n_same unchanged, $n_skip skipped, $n_err errors"
  ((n_err == 0))
}

run_uninstall() {
  local repo dir h res name n=0 n_skip=0
  while IFS= read -r repo; do
    name="$(basename "$repo")"
    dir="$(hooks_dir "$repo")"
    if [[ "$dir" == CONFIG:* ]]; then
      local gone_cfg=""
      for h in "${HOOKS[@]}"; do
        res="$(uninstall_config_hook "$repo" "$h")"
        [[ "$res" != none ]] && gone_cfg="$gone_cfg $h(config hook)"
      done
      if [[ -n "$gone_cfg" ]]; then printf '  removed    %s:%s\n' "$name" "$gone_cfg"; n=$((n + 1)); fi
      continue
    fi
    if [[ "$dir" == SKIP:* ]]; then
      printf '  skipped    %s (%s)\n' "$name" "${dir#SKIP:}"; n_skip=$((n_skip + 1)); continue
    fi
    local gone=""
    for h in "${HOOKS[@]}"; do
      res="$(uninstall_hook "$dir" "$h")"
      [[ "$res" != none ]] && gone="$gone $h($res)"
    done
    if [[ -n "$gone" ]]; then printf '  removed    %s:%s\n' "$name" "$gone"; n=$((n + 1)); fi
  done < <(each_repo "$@")
  echo "repo hooks: $n removed, $n_skip skipped"
}

run_status() {
  local repo dir h f name have chained
  while IFS= read -r repo; do
    name="$(basename "$repo")"
    dir="$(hooks_dir "$repo")"
    if [[ "$dir" == CONFIG:* ]]; then
      have="$(config_installed "$repo")"
      if [[ "$have" == "commit-msg, pre-commit" ]]; then printf '%s: installed (config hook, %s)\n' "$name" "${dir#CONFIG:}"
      elif [[ -n "$have" ]]; then printf '%s: partial (config hook, %s only)\n' "$name" "$have"
      elif git_has_config_hooks; then printf '%s: not installed (%s, needs the config hook)\n' "$name" "${dir#CONFIG:}"
      else printf '%s: guard only (git < 2.54)\n' "$name"
      fi
      continue
    fi
    if [[ "$dir" == SKIP:* ]]; then
      printf '%s: skipped (%s)\n' "$name" "${dir#SKIP:}"; continue
    fi
    have=""; chained=""
    for h in "${HOOKS[@]}"; do
      is_ours "$dir/$h" || continue
      have="$have${have:+, }$h"
      [[ -e "$dir/$h.pre-agent" || -L "$dir/$h.pre-agent" ]] && chained="$chained${chained:+, }$h"
    done
    if [[ -z "$have" ]]; then printf '%s: not installed\n' "$name"
    elif [[ "$have" == "commit-msg, pre-commit" ]]; then printf '%s: installed (shim%s)\n' "$name" "${chained:+, chained: $chained}"
    else printf '%s: partial (%s only)\n' "$name" "$have"
    fi
  done < <(each_repo "$@")
}

cmd="${1:-}"
case "$cmd" in
  install)   shift; run_install "$@" ;;
  uninstall) shift; run_uninstall "$@" ;;
  status)    shift; run_status "$@" ;;
  -h|--help|help) usage ;;
  *) usage >&2; exit 2 ;;
esac
