#!/bin/bash
set -euo pipefail

UI_PORT="${UI_PORT:-8080}"

# First start with an empty volume mounted on /etc/cups: repopulate it
if [ ! -f /etc/cups/cupsd.conf ]; then
    cp -a /etc/cups-skel/. /etc/cups/
fi

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

/usr/sbin/cupsd

# Wait until cupsd responds before enabling printer sharing
for _ in $(seq 1 30); do
    lpstat -r >/dev/null 2>&1 && break
    sleep 1
done
cupsctl --share-printers

cd /opt/airprint
exec uvicorn app.main:app --host 0.0.0.0 --port "$UI_PORT"
