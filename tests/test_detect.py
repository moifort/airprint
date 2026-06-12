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
    uris = detect.candidate_uris("192.168.1.50", "dnssd://printer._pdl-datastream")
    assert uris[0] == "dnssd://printer._pdl-datastream"
    assert "socket://192.168.1.50:9100" in uris


def test_candidate_uris_no_duplicate():
    uris = detect.candidate_uris("192.168.1.50", "socket://192.168.1.50:9100")
    assert uris.count("socket://192.168.1.50:9100") == 1
    assert uris[0] == "socket://192.168.1.50:9100"
