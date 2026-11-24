#!/usr/bin/env bash
set -u

stop() { echo "STOP: $1"; exit "${2:-1}"; }

TOP=$(git rev-parse --show-toplevel 2>/dev/null) || stop "not in a git work tree" 2
ROOT=$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")
[ "$(cd "$TOP" && pwd -P)" = "$(cd "$ROOT" && pwd -P)" ] && stop "this is the shared checkout ($ROOT); bootstrap runs in a linked worktree" 2
cd "$TOP" || stop "cannot enter $TOP" 2

if [ -e .env ]; then
  echo "BOOTSTRAP .env: kept (the worktree has one)"
elif [ -f "$ROOT/.env" ]; then
  cp "$ROOT/.env" .env || stop "cannot copy $ROOT/.env"
  echo "BOOTSTRAP .env: copied from $ROOT"
else
  echo "BOOTSTRAP .env: none in $ROOT"
fi

shared_problem() {
  [ -d "$ROOT/node_modules" ] || { echo "$ROOT has no node_modules"; return; }
  [ -f "$ROOT/package-lock.json" ] || { echo "$ROOT has no package-lock.json"; return; }
  cmp -s package-lock.json "$ROOT/package-lock.json" || { echo "package-lock.json differs from $ROOT's"; return; }
  [ -f "$ROOT/node_modules/.package-lock.json" ] || { echo "the install in $ROOT is unfinished (no node_modules/.package-lock.json)"; return; }
  [ "$ROOT/package-lock.json" -nt "$ROOT/node_modules/.package-lock.json" ] && { echo "the install in $ROOT is older than its package-lock.json"; return; }
}

if [ ! -f package-lock.json ]; then
  echo "BOOTSTRAP node_modules: skipped (no package-lock.json)"
else
  problem=$(shared_problem)
  if [ -L node_modules ] && [ -z "$problem" ] && [ "$(readlink node_modules)" = "$ROOT/node_modules" ]; then
    echo "BOOTSTRAP node_modules: kept (a link to $ROOT's install, current)"
  elif [ -d node_modules ] && [ ! -L node_modules ]; then
    echo "BOOTSTRAP node_modules: kept (an install of this worktree)"
  elif [ -z "$problem" ]; then
    [ -L node_modules ] && rm node_modules
    ln -s "$ROOT/node_modules" node_modules || stop "cannot link $ROOT/node_modules"
    echo "BOOTSTRAP node_modules: linked to $ROOT's install (same package-lock.json, install current)"
  else
    [ -L node_modules ] && rm node_modules
    command -v npm >/dev/null 2>&1 || stop "npm is not on PATH; $problem"
    echo "BOOTSTRAP node_modules: installing here with npm ci, because $problem"
    npm ci >/dev/null || stop "npm ci failed in $TOP"
    echo "BOOTSTRAP node_modules: installed here"
  fi
fi

if [ -f .gitmodules ]; then
  git submodule update --init --recursive >/dev/null 2>&1 || stop "git submodule update failed"
  echo "BOOTSTRAP submodules: updated"
else
  echo "BOOTSTRAP submodules: none"
fi
exit 0
