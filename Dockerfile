FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

# CUPS + Avahi pour l'annonce AirPrint, et la base de drivers OpenPrinting
# packagée par Debian (foomatic, Gutenprint, HPLIP, brlaser, SpliX, foo2zjs…).
RUN apt-get update && apt-get install -y --no-install-recommends \
        cups \
        cups-filters \
        cups-ipp-utils \
        avahi-daemon \
        avahi-utils \
        dbus \
        foomatic-db \
        foomatic-db-engine \
        openprinting-ppds \
        printer-driver-gutenprint \
        printer-driver-hpcups \
        printer-driver-postscript-hp \
        printer-driver-brlaser \
        printer-driver-splix \
        printer-driver-escpr \
        printer-driver-foo2zjs \
        printer-driver-ptouch \
        python3 \
        python3-pip \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/airprint

COPY requirements.txt ./
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

COPY cups/cupsd.conf /etc/cups/cupsd.conf
# Squelette de config : restauré par l'entrypoint quand le volume /etc/cups est vide
RUN cp -a /etc/cups /etc/cups-skel

COPY app ./app
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV UI_PORT=8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
    CMD curl -fs "http://localhost:${UI_PORT}/api/printers" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
