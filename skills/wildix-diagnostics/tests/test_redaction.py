"""Tests for digest-auth masking in wildix_analyzer.py's report.

Run from the repo root:  python -m unittest discover -s skills/wildix-diagnostics/tests
"""
import datetime
import struct
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'scripts'))

import wildix_analyzer as A  # noqa: E402

REGISTER = (
    'REGISTER sip:pbx.example.wildixin.com SIP/2.0\r\n'
    'Via: SIP/2.0/UDP 192.0.2.10:5060;branch=z9hG4bK776asdhds\r\n'
    'From: <sip:117@pbx.example.wildixin.com>;tag=1928301774\r\n'
    'To: <sip:117@pbx.example.wildixin.com>\r\n'
    'Call-ID: a84b4c76e66710@192.0.2.10\r\n'
    'CSeq: 2 REGISTER\r\n'
    'Authorization: Digest username="117", realm="pbx.example.wildixin.com", '
    'nonce="dcd98b7102dd2f0e8b11d0f600bfb0c093", uri="sip:pbx.example.wildixin.com", '
    'response="6629fae49393a05397450978507c4ef1", algorithm=MD5, '
    'cnonce="0a4f113b", opaque="5ccc069c403ebaf9f0171e9517f40e41", qop=auth, nc=00000001\r\n'
    'Content-Length: 0\r\n\r\n'
)
CHALLENGE = (
    'SIP/2.0 407 Proxy Authentication Required\r\n'
    'Call-ID: a84b4c76e66710@192.0.2.10\r\n'
    'CSeq: 1 INVITE\r\n'
    'Proxy-Authenticate: Digest realm="pbx.example.wildixin.com", nonce=abc123def, '
    'opaque="xyz", algorithm=MD5, qop="auth"\r\n'
    'Content-Length: 0\r\n\r\n'
)
SECRETS = ('dcd98b7102dd2f0e8b11d0f600bfb0c093', '6629fae49393a05397450978507c4ef1',
           '0a4f113b', '5ccc069c403ebaf9f0171e9517f40e41', 'abc123def', '"xyz"')


class MaskSipAuthTest(unittest.TestCase):
    def test_register_authorization(self):
        out = A.mask_sip_auth(REGISTER)
        for s in SECRETS[:4]:
            self.assertNotIn(s, out)
        for kept in ('username="117"', 'realm="pbx.example.wildixin.com"',
                     'uri="sip:pbx.example.wildixin.com"', 'algorithm=MD5', 'qop=auth'):
            self.assertIn(kept, out)
        self.assertIn('response="[REDACTED]"', out)
        self.assertIn('nonce="[REDACTED]"', out)
        self.assertIn('cnonce="[REDACTED]"', out)
        self.assertIn('opaque="[REDACTED]"', out)

    def test_challenge_unquoted_value(self):
        out = A.mask_sip_auth(CHALLENGE)
        self.assertIn('nonce=[REDACTED],', out)
        self.assertIn('opaque="[REDACTED]"', out)
        self.assertIn('qop="auth"', out)

    def test_other_headers_untouched(self):
        # "nonce=" outside an auth header (e.g. in a body or another header) is left alone.
        text = 'OPTIONS sip:x SIP/2.0\r\nX-Debug: nonce=keepme\r\nCall-ID: response=1\r\n\r\n'
        self.assertEqual(A.mask_sip_auth(text), text)


def _pcap_bytes(payloads):
    """A minimal Ethernet/IPv4/UDP pcap carrying each payload as one packet."""
    out = [struct.pack('<IHHiIII', 0xa1b2c3d4, 2, 4, 0, 0, 65535, 1)]
    for i, pl in enumerate(payloads):
        udp = struct.pack('!HHHH', 5060, 5060, 8 + len(pl), 0) + pl
        ip = struct.pack('!BBHHHBBH4s4s', 0x45, 0, 20 + len(udp), 0, 0, 64, 17, 0,
                         bytes([192, 0, 2, 10]), bytes([192, 0, 2, 1])) + udp
        frame = b'\x00' * 12 + b'\x08\x00' + ip
        out.append(struct.pack('<IIII', 1_700_000_000 + i, 0, len(frame), len(frame)) + frame)
    return b''.join(out)


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pcap = Path(self.tmp.name) / 'filtered_test.pcap'
        self.pcap.write_bytes(_pcap_bytes([REGISTER.encode(), CHALLENGE.encode()]))

    def tearDown(self):
        self.tmp.cleanup()

    def test_report_masks_by_default(self):
        text = A.pcap_to_text(self.pcap, datetime.timezone.utc, 'UTC')
        self.assertIn('## SIP LADDER (2 messages)', text)
        for s in SECRETS:
            self.assertNotIn(s, text)
        self.assertIn('username="117"', text)

    def test_no_redact_keeps_values_and_pcap_unchanged(self):
        before = self.pcap.read_bytes()
        text = A.pcap_to_text(self.pcap, datetime.timezone.utc, 'UTC', redact=False)
        self.assertIn('response="6629fae49393a05397450978507c4ef1"', text)
        self.assertEqual(self.pcap.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
