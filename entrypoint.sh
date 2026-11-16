#!/bin/bash
set -euo pipefail

UI_PORT="${UI_PORT:-8080}"

# First start with an empty volume mounted on /etc/cups: repopulate it
if [ ! -f /etc/cups/cupsd.conf ]; then
    cp -a /etc/cups-skel/. /etc/cups/
fi
# cupsd.conf belongs to the image, not to the user: refresh it on every
# start so image upgrades reach existing volumes. Queues (printers.conf,
# ppd/) are left untouched.
cp /etc/cups-skel/cupsd.conf /etc/cups/cupsd.conf

mkdir -p /run/dbus
rm -f /run/dbus/pid /run/avahi-daemon/pid

# Avahi must announce only on the LAN interface: announcing on Docker's
# virtual bridges (docker0, br-*, veth*) publishes unreachable 172.x
# addresses for the host name, which breaks AirPrint clients and feeds
# mDNS name conflicts. The LAN interface is the one holding the default route.
LAN_IF=$(awk '$2 == "00000000" {print $1; exit}' /proc/net/route)
if [ -n "${LAN_IF}" ]; then
    sed -i "s/^#\?allow-interfaces=.*/allow-interfaces=${LAN_IF}/" /etc/avahi/avahi-daemon.conf
fi

dbus-daemon --system
avahi-daemon --daemonize --no-drop-root

# Foreground under this script (not daemonized) so the watchdog below holds
# its PID; with ErrorLog stderr its errors land in `docker logs`.
/usr/sbin/cupsd -f &
CUPSD_PID=$!

# `lpstat -r` exits 0 even when the scheduler is down: match its output.
# (Daemonized cupsd used to return only once ready, which hid this; in the
# foreground it needs a moment before the socket accepts connections.)
cupsd_ready() { LC_ALL=C lpstat -r 2>/dev/null | grep -qx "scheduler is running"; }

for _ in $(seq 1 60); do
    cupsd_ready && break
    sleep 1
done
if ! cupsd_ready; then
    echo "cupsd did not start within 60 s" >&2
    exit 1
fi
cupsctl --share-printers

# Queues created before the retry-job default: a printer switched off for a
# moment would leave them stopped with every AirPrint job stuck.
# Local queues only: `lpstat -e` also lists destinations discovered over
# DNS-SD (including our own queues, re-seen as "<name>_airprint").
for queue in $(lpstat -p 2>/dev/null | awk '$1 == "printer" {print $2}'); do
    lpadmin -p "$queue" -o printer-error-policy=retry-job || true
done

cd /opt/airprint
uvicorn app.main:app --host 0.0.0.0 --port "$UI_PORT" &
UVICORN_PID=$!

# --- Supervision ---------------------------------------------------------------
# Docker only restarts a container whose main process exits: a dead cupsd,
# Avahi or dbus would otherwise leave the bridge "unhealthy" forever, with
# nothing announced or printed. Exit as soon as one of them is gone (or cupsd
# stops answering) and let `restart: unless-stopped` start over cleanly.
set +e

shutdown() {
    # Goodbye packets: Apple devices drop the printers instead of timing out
    avahi-daemon --kill 2>/dev/null
    kill -TERM "$UVICORN_PID" "$CUPSD_PID" 2>/dev/null
    wait "$UVICORN_PID" "$CUPSD_PID"
    exit "${1:-0}"
}
trap 'shutdown 0' TERM INT

alive_pidfile() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }

CUPSD_FAILURES=0
while true; do
    # Backgrounded sleep: a SIGTERM runs the trap at once, not after the sleep
    sleep 10 & wait $!
    for check in "uvicorn:kill -0 $UVICORN_PID" "cupsd:kill -0 $CUPSD_PID" \
                 "avahi-daemon:alive_pidfile /run/avahi-daemon/pid" \
                 "dbus-daemon:alive_pidfile /run/dbus/pid"; do
        if ! eval "${check#*:}" 2>/dev/null; then
            echo "watchdog: ${check%%:*} is gone, exiting so Docker restarts the container" >&2
            shutdown 1
        fi
    done
    # A hung cupsd keeps its PID: three silent checks in a row (30 s) count
    if timeout 10 bash -c "$(declare -f cupsd_ready); cupsd_ready"; then
        CUPSD_FAILURES=0
    elif [ $((CUPSD_FAILURES += 1)) -ge 3 ]; then
        echo "watchdog: cupsd is not answering, exiting so Docker restarts the container" >&2
        shutdown 1
    fi
done
