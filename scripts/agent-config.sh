# Sourced, not run. Reads the git config keys of the agent setup (mode, branchPrefix,
# selfMerge, home, ...). Claude Code, GitHub Copilot CLI and Codex all read them, so their
# name is agent.<key>. The old name claude.<key> still works: it is read when agent.<key> is
# not set. Write only agent.<key>.
#
#   . "$(dirname "$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || printf %s "${BASH_SOURCE[0]}")")/agent-config.sh"
#   agent_cfg [-C <dir>] [git config options] <key>     agent.<key>, else claude.<key>
#   agent_cfg_union [-C <dir>] [git config options] <key>   both values, space separated
#
# The options go to `git config` as they are, for example --local --no-includes or --bool.
# agent_cfg prints the value and returns 0, or prints nothing and returns 1 when neither
# name is set. It falls back to claude.<key> only when agent.<key> is not set (git exit 1),
# so an agent.<key> that git refuses (a bad --bool value) never lets the old name through.
# agent_cfg_union is for list keys such as selfMergeExclude, where more values is stricter:
# a glob under the old name keeps working after the new name is set.
# Works with the bash 3.2 of macOS: no empty "${a[@]}" under set -u.

_agent_cfg_get() {   # _agent_cfg_get <prefix> <args...>: one git config --get, exit code kept
  local prefix=$1; shift
  local c=() o=() k
  if [ "${1:-}" = -C ]; then c=(-C "$2"); shift 2; fi
  [ $# -ge 1 ] || return 2
  k=${!#}
  [ $# -gt 1 ] && o=("${@:1:$(($# - 1))}")
  git ${c[@]+"${c[@]}"} config ${o[@]+"${o[@]}"} --get "$prefix.$k" 2>/dev/null
}

agent_cfg() {
  local v rc
  v=$(_agent_cfg_get agent "$@"); rc=$?
  if [ $rc -eq 1 ]; then
    v=$(_agent_cfg_get claude "$@"); rc=$?
  fi
  [ $rc -eq 0 ] || return 1
  printf '%s\n' "$v"
}

agent_cfg_union() {
  local a c
  a=$(_agent_cfg_get agent "$@")
  c=$(_agent_cfg_get claude "$@")
  a="$a $c"; a=${a# }; a=${a% }
  [ -n "$a" ] || return 1
  printf '%s\n' "$a"
}
