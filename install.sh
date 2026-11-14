#!/bin/sh
# ---------------------------------------------------------------------------
# install.sh — install the splcprint CUPS backend for Sharp SPLC printers
#
# Usage:
#   ./install.sh 192.168.1.50            install (asks for sudo once)
#   ./install.sh 192.168.1.50 --queue    also create a CUPS queue named SHARP_SPLC
#   ./install.sh --uninstall             remove everything this script installed
#
# What it does:
#   1. copies the Python service into ~/Library/Application Support/splcprint
#   2. installs backend/splcprint into /usr/libexec/cups/backend (root:wheel 0755)
#   3. writes ~/Library/LaunchAgents/com.splcprint.agent.plist and loads it
#
# The CUPS backend runs as _lp inside a sandbox and cannot read your home
# directory, so it only forwards the spool file over loopback TCP to the user
# service on 127.0.0.1:9123, which does the rendering and talks to the printer.
# ---------------------------------------------------------------------------
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
SUPPORT="$HOME/Library/Application Support/splcprint"
AGENT="$HOME/Library/LaunchAgents/com.splcprint.agent.plist"
BACKEND="/usr/libexec/cups/backend/splcprint"
PORT="${SPLC_PORT:-9123}"

die() { echo "error: $*" >&2; exit 1; }

if [ "${1:-}" = "--uninstall" ]; then
    launchctl bootout "gui/$(id -u)/com.splcprint.agent" 2>/dev/null
    rm -f "$AGENT"
    sudo rm -f "$BACKEND"
    echo "removed LaunchAgent and CUPS backend."
    echo "left in place: $SUPPORT (delete it manually if you want)"
    exit 0
fi

IP="${1:-}"
[ -n "$IP" ] || die "usage: ./install.sh <printer-ip> [--queue]"
case "$IP" in
    *[!0-9.]*) die "printer address must be an IPv4 address, got: $IP" ;;
esac

[ -f "$HERE/splc.py" ] || die "run this from the repository root"
[ -f "$HERE/band1.bin" ] || echo "warning: band1.bin not found in repo root; see tools/extract-band1.py"

echo "==> installing service files into $SUPPORT"
mkdir -p "$SUPPORT"
cp "$HERE/splc.py" "$HERE/print_job.py" "$HERE/agent.py" "$SUPPORT/"
[ -f "$HERE/band1.bin" ] && cp "$HERE/band1.bin" "$SUPPORT/"

echo "==> installing CUPS backend (sudo required)"
sudo install -d -o root -g wheel -m 0755 /usr/libexec/cups/backend
sudo install -o root -g wheel -m 0755 "$HERE/backend/splcprint" "$BACKEND"

echo "==> writing LaunchAgent"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cat > "$AGENT" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.splcprint.agent</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>$SUPPORT/agent.py</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>SPLC_PRINTER</key><string>$IP</string>
    <key>SPLC_PRINTER_PORT</key><string>9100</string>
    <key>SPLC_PORT</key><string>$PORT</string>
    <key>SPLC_LISTEN</key><string>127.0.0.1</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>5</integer>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/splcprint-agent.out</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/splcprint-agent.err</string>
</dict>
</plist>
PLIST

echo "==> loading service"
launchctl bootout "gui/$(id -u)/com.splcprint.agent" 2>/dev/null
launchctl bootstrap "gui/$(id -u)" "$AGENT" || die "launchctl bootstrap failed"
sleep 1
if nc -z 127.0.0.1 "$PORT" 2>/dev/null; then
    echo "    service is listening on 127.0.0.1:$PORT"
else
    echo "    warning: nothing on 127.0.0.1:$PORT; check ~/Library/Logs/splcprint-agent.err"
fi

if [ "${2:-}" = "--queue" ]; then
    echo "==> creating CUPS queue SHARP_SPLC"
    lpadmin -p SHARP_SPLC -E -v "splcprint://$IP:9100" -m everywhere 2>/dev/null \
        || lpadmin -p SHARP_SPLC -E -v "splcprint://$IP:9100" -P /System/Library/Printers/PPDs/Contents/Resources/Generic.ppd
    lpoptions -d SHARP_SPLC >/dev/null 2>&1 || true
    echo "    queue created; test with: lp -d SHARP_SPLC /path/to/file.pdf"
fi

cat <<DONE

done.

  printer     $IP:9100
  service     $SUPPORT (LaunchAgent com.splcprint.agent)
  log         ~/Library/Logs/splcprint.log
  test        python3 "$SUPPORT/splc.py" --pattern test --send $IP

First print will really put paper out. Keep a page in the tray.
DONE
