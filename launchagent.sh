#!/usr/bin/env bash
set -euo pipefail

SCRIPT_ONLY=0
case "${1:-}" in
  "") [[ $# -eq 0 ]] || { echo "usage: ./launchagent.sh [--script-only]" >&2; exit 2; } ;;
  --script-only) [[ $# -eq 1 ]] || { echo "usage: ./launchagent.sh [--script-only]" >&2; exit 2; }; SCRIPT_ONLY=1 ;;
  *) echo "unknown option: $1 (usage: ./launchagent.sh [--script-only])" >&2; exit 2 ;;
esac

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL=com.fskroes.agent-config-work-update
SCRIPT="$HOME/.local/share/agent-config-work-update/update.sh"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
STATE="$HOME/.local/state/agent-config-work"
for p in "$REPO" "$HOME"; do
  case "$p" in
    *[\"\$\`\\\&\<\>]*) echo "  STOP: the path $p holds one of \" \$ \` \\ & < >; move the clone or rename the folder" >&2; exit 1 ;;
  esac
done
mkdir -p "$(dirname "$SCRIPT")" "$(dirname "$PLIST")" "$STATE"
((SCRIPT_ONLY)) || [[ -f "$STATE/name" ]] || echo "  note    no saved name: the files keep \"Fernando\". Run ./install.sh --name <your name> once."

TMP="$SCRIPT.tmp.$$"
cat > "$TMP" <<EOF
#!/usr/bin/env bash
set -euo pipefail
REPO="$REPO"
STATE="$STATE"
STAMP="\$STATE/installed-sha"
mkdir -p "\$STATE"
export GIT_TERMINAL_PROMPT=0

status() { printf '%s %s\n' "\$(date '+%F %T')" "\$1" > "\$STATE/status"; }
trap 'status "FAIL line \$LINENO (see update.log)"' ERR

git -C "\$REPO" fetch -q origin
if git -C "\$REPO" merge-base --is-ancestor HEAD origin/main; then
  git -C "\$REPO" merge -q --ff-only origin/main
elif git -C "\$REPO" diff --quiet HEAD \\
     && git -C "\$REPO" log --format=%T origin/main | grep -xF "\$(git -C "\$REPO" rev-parse 'HEAD^{tree}')" >/dev/null; then
  echo "== \$(date '+%F %T') origin/main has a new history; HEAD \$(git -C "\$REPO" rev-parse --short HEAD) holds nothing of its own, moved to origin/main"
  git -C "\$REPO" reset -q --hard origin/main
else
  status "STOP: origin/main has a new history and this clone holds changes or commits that are not on it. Save them, then clone again: see update.log"
  echo "== \$(date '+%F %T') STOP: HEAD is not on origin/main and holds changes or commits of its own. Nothing changed. Save what you need, then:"
  echo "   rm -rf \"\$REPO\" && git clone https://github.com/fskroes/agent-config-work.git \"\$REPO\" && \"\$REPO/install.sh\""
  exit 1
fi
head=\$(git -C "\$REPO" rev-parse --short HEAD)
if [[ -f "\$STAMP" && "\$(cat "\$STAMP")" == "\$head" ]]; then
  status "ok, no change (\$head)"
  exit 0
fi
echo "== \$(date '+%F %T') install \$head (was \$(cat "\$STAMP" 2>/dev/null || echo none))"
"\$REPO/install.sh"
echo "\$head" > "\$STAMP"
status "ok, installed \$head"
EOF
chmod 755 "$TMP"
mv -f "$TMP" "$SCRIPT"
if ((SCRIPT_ONLY)); then
  echo "  wrote   ${SCRIPT#"$HOME"/}"
  exit 0
fi

TMP="$PLIST.tmp.$$"
cat > "$TMP" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$SCRIPT</string></array>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>3600</integer>
  <key>StandardOutPath</key><string>$STATE/update.log</string>
  <key>StandardErrorPath</key><string>$STATE/update.log</string>
</dict>
</plist>
EOF
mv -f "$TMP" "$PLIST"

if command -v launchctl >/dev/null && [[ -z "${AGENT_CONFIG_NO_LAUNCHCTL:-}" ]]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  echo "  loaded  $LABEL (hourly)"
else
  echo "  wrote   $PLIST (not loaded)"
fi
