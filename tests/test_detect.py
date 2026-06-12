import subprocess

from app import detect

SNMP_LINE = (
    'network socket://192.168.1.50 "HP LaserJet 1320" '
    '"HP LaserJet 1320 series" "MFG:Hewlett-Packard;MDL:hp LaserJet 1320;" ""'
)

IPPTOOL_OUTPUT = """\
    Get-Printer-Attributes:
        attributes-charset (charset) = utf-8
        printer-make-and-model (textWithoutLanguage) = Brother HL-L2350DW series
        printer-state (enum) = idle
"""


def test_parse_snmp_output():
    result = detect.parse_snmp_output(SNMP_LINE)
    assert result == {
        "uri": "socket://192.168.1.50",
        "make_model": "HP LaserJet 1320",
        "device_id": "MFG:Hewlett-Packard;MDL:hp LaserJet 1320;",
    }


def test_parse_snmp_output_ignores_garbage():
    assert detect.parse_snmp_output("") is None
    assert detect.parse_snmp_output("DEBUG: no response from 192.168.1.50") is None


def test_parse_ipptool_output():
    assert (
        detect.parse_ipptool_output(IPPTOOL_OUTPUT)
        == "Brother HL-L2350DW series"
    )
    assert detect.parse_ipptool_output("ipptool: unable to connect") is None


def test_candidate_uris_puts_detected_first():
    uris = detect.candidate_uris("192.168.1.50", "ipps://192.168.1.50/ipp/print")
    assert uris[0] == "ipps://192.168.1.50/ipp/print"
    assert "socket://192.168.1.50:9100" in uris


def test_candidate_uris_puts_dnssd_last():
    """dnssd URIs need live mDNS resolution on every job — direct IP transport
    is far more reliable, so a discovered dnssd URI goes last, not first."""
    uris = detect.candidate_uris("192.168.1.50", "dnssd://printer._pdl-datastream")
    assert uris[0] == "socket://192.168.1.50:9100"
    assert uris[-1] == "dnssd://printer._pdl-datastream"


def test_candidate_uris_no_duplicate():
    uris = detect.candidate_uris("192.168.1.50", "socket://192.168.1.50:9100")
    assert uris.count("socket://192.168.1.50:9100") == 1
    assert uris[0] == "socket://192.168.1.50:9100"


LPINFO_L_OUTPUT = """\
Device: uri = socket://192.168.1.50:9100
        class = network
        info = HP LaserJet 1320
        make-and-model = HP LaserJet 1320
        device-id = MFG:HP;MDL:LaserJet 1320;
        location =
Device: uri = dnssd://HP%20LaserJet%201320._pdl-datastream._tcp.local/
        class = network
        info = HP LaserJet 1320
        make-and-model = HP LaserJet 1320
        device-id =
        location =
Device: uri = ipp
        class = network
        info = Internet Printing Protocol (ipp)
        make-and-model = Unknown
        device-id =
        location =
Device: uri = ipp://192.168.1.60/ipp/print
        class = network
        info = Brother HL-L2350DW
        make-and-model = Brother HL-L2350DW series
        device-id = MFG:Brother;MDL:HL-L2350DW series;
        location =
"""


def test_parse_lpinfo_devices():
    devices = detect.parse_lpinfo_devices(LPINFO_L_OUTPUT)
    assert len(devices) == 4
    assert devices[0]["uri"] == "socket://192.168.1.50:9100"
    assert devices[0]["device-id"] == "MFG:HP;MDL:LaserJet 1320;"
    assert devices[2]["uri"] == "ipp"


def test_scan_filters_and_dedupes(monkeypatch):
    monkeypatch.setattr(detect, "_run", lambda cmd, timeout=0: type(
        "P", (), {"stdout": LPINFO_L_OUTPUT}
    )())
    printers = detect.scan()
    # The bare "ipp" backend template is filtered out; the dnssd entry is a
    # duplicate of the SNMP one (same make-and-model) and must be dropped.
    assert [p["ip"] for p in printers] == ["192.168.1.50", "192.168.1.60"]
    assert printers[0]["device_id"] == "MFG:HP;MDL:LaserJet 1320;"
    assert printers[0]["uris"][0] == "socket://192.168.1.50:9100"
    assert printers[1]["make_model"] == "Brother HL-L2350DW series"


AVAHI_BROWSE_OUTPUT = """\
+;enp3s0;IPv4;Brother\\032HL-1210W\\032series;PDL Printer;local
=;enp3s0;IPv4;Brother\\032HL-1210W\\032series;PDL Printer;local;BRNF0A654D315D4.local;192.168.1.146;9100;"UUID=e3248000" "ty=Brother HL-1210W series"
=;enp3s0;IPv6;Ignored\\032v6;PDL Printer;local;BRNF0A654D315D4.local;fe80::1;9100;""
"""


def test_parse_avahi_browse_maps_instance_to_ipv4():
    addresses = detect.parse_avahi_browse(AVAHI_BROWSE_OUTPUT)
    assert addresses == {"Brother HL-1210W series": "192.168.1.146"}


def test_scan_resolves_ip_less_dnssd_entries(monkeypatch):
    lpinfo_output = (
        "Device: uri = dnssd://Brother%20HL-1210W%20series._pdl-datastream._tcp.local/?uuid=e3248000\n"
        "        class = network\n"
        "        info = Brother HL-1210W series\n"
        "        make-and-model = Brother HL-1210W series\n"
        "        device-id = MFG:Brother;MDL:HL-1210W series;CMD:PJL,HBP;\n"
    )
    monkeypatch.setattr(detect, "_run", lambda cmd, timeout=0: subprocess.CompletedProcess(
        cmd, 0, stdout=lpinfo_output if cmd[0] == "lpinfo" else AVAHI_BROWSE_OUTPUT, stderr=""
    ))
    printers = detect.scan()
    assert printers[0]["ip"] == "192.168.1.146"
    assert printers[0]["uris"][0] == "socket://192.168.1.146:9100"
    assert printers[0]["uris"][-1].startswith("dnssd://")
