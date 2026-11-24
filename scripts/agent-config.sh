_agent_cfg_get() {
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
