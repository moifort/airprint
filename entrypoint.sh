#!/bin/bash
set -euo pipefail

UI_PORT="${UI_PORT:-8080}"

# First start with an empty volume mounted on /etc/cups: repopulate it
if [ ! -f /etc/cups/cupsd.conf ]; then
    cp -a /etc/cups-skel/. /etc/cups/
fi

mkdir -p /run/dbus
rm -f /run/dbus/pid /run/avahi-daemon/pid

dbus-daemon --system
avahi-daemon --daemonize --no-drop-root

/usr/sbin/cupsd

# Wait until cupsd responds before enabling printer sharing
for _ in $(seq 1 30); do
    lpstat -r >/dev/null 2>&1 && break
    sleep 1
done
cupsctl --share-printers

cd /opt/airprint
exec uvicorn app.main:app --host 0.0.0.0 --port "$UI_PORT"
