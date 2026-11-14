#!/bin/sh
# ---------------------------------------------------------------------------
# install-scan.sh — run the scan receiver as a system daemon on port 21
#
# Usage:
#   sudo ./scan/install-scan.sh                       # default dir ~/Scans, random password
#   sudo ./scan/install-scan.sh --dir /Volumes/Scans --password 'long-password'
#   sudo ./scan/install-scan.sh --uninstall
#
# Why a LaunchDaemon and not a LaunchAgent: the printer insists on port 21,
# and binding a port below 1024 needs root. The daemon is started with --chown
# so received files still belong to the logged-in user and can be edited in Finder.
# ---------------------------------------------------------------------------
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
LABEL="com.sharp.scanreceiver"
PLIST="/Library/LaunchDaemons/$LABEL.plist"
LOG="/tmp/sharp-scan-receiver.log"
DIR="$HOME/Scans"
PASSWORD=""
PORT="${SPLC_SCAN_PORT:-21}"

die() { echo "error: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --dir) DIR="${2:-}"; shift 2 ;;
        --password) PASSWORD="${2:-}"; shift 2 ;;
        --uninstall) UNINSTALL=1; shift ;;
        -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done

[ "$(id -u)" -eq 0 ] || die "run with sudo: sudo $0 $*"

if [ "${UNINSTALL:-0}" = "1" ]; then
    launchctl bootout "system/$LABEL" 2>/dev/null
    rm -f "$PLIST"
    echo "removed $LABEL; port 21 should be free now"
    echo "kept: $LOG and the scan directory"
    exit 0
fi

[ -f "$HERE/scan_receiver.py" ] || die "scan_receiver.py not found next to this script"
[ -f "$HERE/a4_normalize.py" ] || die "a4_normalize.py not found next to this script"

if [ -z "$PASSWORD" ]; then
    PASSWORD=$(/usr/bin/python3 -c 'import secrets; print(secrets.token_urlsafe(9))')
    GENERATED=1
fi

# The printer connects to the LAN address, so advertise the real one rather than 127.0.0.1.
IFACE=$(route -n get default 2>/dev/null | awk '/interface:/{print $2}')
IP=""
[ -n "${IFACE:-}" ] && IP=$(ipconfig getifaddr "$IFACE" 2>/dev/null)
[ -n "$IP" ] || IP=$(/usr/bin/python3 -c 'import socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.connect(("192.0.2.1",9));print(s.getsockname()[0])')

MYUID=$(id -u "${SUDO_UID:-$(stat -f%u "$HOME")}")
MYGID=$(id -g "${SUDO_GID:-$(stat -f%g "$HOME")}")

/bin/mkdir -p "$DIR"
/bin/chown "$MYUID:$MYGID" "$DIR"

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>$HERE/scan_receiver.py</string>
        <string>--port</string><string>$PORT</string>
        <string>--dir</string><string>$DIR</string>
        <string>--user</string><string>scan</string>
        <string>--password</string><string>$PASSWORD</string>
        <string>--chown</string><string>$MYUID:$MYGID</string>
        <string>--advertise-ip</string><string>$IP</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ThrottleInterval</key><integer>10</integer>
    <key>ProcessType</key><string>Background</string>
    <key>StandardOutPath</key><string>$LOG</string>
    <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PLISTEOF

/bin/chown root:wheel "$PLIST"
/bin/chmod 644 "$PLIST"

launchctl bootout "system/$LABEL" 2>/dev/null
launchctl bootstrap system "$PLIST" || die "launchctl bootstrap failed; see $LOG"
sleep 2

echo
echo "=================== scan receiver installed ==================="
echo "  files      $DIR"
echo "  listening  0.0.0.0:$PORT"
echo "  log        $LOG"
echo "  account    scan / $PASSWORD"
[ "${GENERATED:-0}" = "1" ] && echo "             (generated; pass --password to set your own)"
echo
echo "  --- enter this on the printer panel (Address Book -> FTP) ---"
echo "  host       $IP"
echo "  port       21"
echo "  user       scan"
echo "  password   $PASSWORD"
echo "  directory  /"
echo "  SSL        off"
echo "==============================================================="
echo
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "  verified: something is listening on port $PORT"
else
    echo "  WARNING: nothing listening on port $PORT yet; check $LOG"
fi
echo
echo "  restart: sudo launchctl kickstart -k system/$LABEL"
echo "  remove : sudo $0 --uninstall"
echo
echo "  This is plain FTP (no TLS). Keep it on a trusted LAN only."
