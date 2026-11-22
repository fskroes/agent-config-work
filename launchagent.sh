#!/usr/bin/env bash
# Install the hourly update of this clone as the LaunchAgent com.fskroes.agent-config-work-update.
# Every hour it runs `git pull --ff-only` here, then ./install.sh when HEAD changed. install.sh
# reuses the name saved by `./install.sh --name`. Idempotent. Run from any folder:
#   ./launchagent.sh
# Writes: ~/.local/share/agent-config-work-update/update.sh,
#         ~/Library/LaunchAgents/com.fskroes.agent-config-work-update.plist
# State and log: ~/.local/state/agent-config-work/{status,update.log,installed-sha}
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL=com.fskroes.agent-config-work-update
SCRIPT="$HOME/.local/share/agent-config-work-update/update.sh"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
STATE="$HOME/.local/state/agent-config-work"
mkdir -p "$(dirname "$SCRIPT")" "$(dirname "$PLIST")" "$STATE"

cat > "$SCRIPT" <<EOF
#!/usr/bin/env bash
set -euo pipefail
REPO="$REPO"
STATE="$STATE"
STAMP="\$STATE/installed-sha"
mkdir -p "\$STATE"
export GIT_TERMINAL_PROMPT=0

status() { printf '%s %s\n' "\$(date '+%F %T')" "\$1" > "\$STATE/status"; }
trap 'status "FAIL line \$LINENO (see update.log)"' ERR

git -C "\$REPO" pull --ff-only -q
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
chmod 755 "$SCRIPT"

cat > "$PLIST" <<EOF
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

# The RunAtLoad run is skipped when launchctl is missing (tests, Linux).
if command -v launchctl >/dev/null && [[ -z "${AGENT_CONFIG_NO_LAUNCHCTL:-}" ]]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  echo "  loaded  $LABEL (hourly)"
else
  echo "  wrote   $PLIST (not loaded)"
fi
