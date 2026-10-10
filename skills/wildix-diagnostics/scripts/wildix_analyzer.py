#!/usr/bin/env python3
"""
Wildix Autotrace Log Analyzer
==============================
Filters logs and pcap files from a Wildix Diagnostics Hub archive by time range
and call identifiers, then writes a filtered report ready for analysis.

Usage:
  python3 wildix_analyzer.py <archive_or_dir> --start "YYYY-MM-DD HH:MM:SS" \
      --end "YYYY-MM-DD HH:MM:SS" [--system-tz "America/Chicago"] [options]

  --system-tz defaults to 'auto', which detects the PBX timezone from nginx
  log entries in wms.log (they carry an explicit UTC offset like -0500).

  Times are interpreted in --user-tz (default: same as --system-tz).
  The system timezone is what the PBX itself runs in (needed to interpret
  syslog/callweaver timestamps which carry no timezone info).
"""

import argparse
import datetime
import gzip
import io
import ipaddress
import json
import os
import re
import struct
import sys
import tarfile
import tempfile
import shutil
from pathlib import Path
from zoneinfo import ZoneInfo

# ─── TIMEZONE ────────────────────────────────────────────────────────────────

def parse_timezone(tz_str):
    """Accept IANA name ('America/Chicago') or UTC offset ('-0500', '+05:30')."""
    if not tz_str:
        return datetime.timezone.utc
    try:
        return ZoneInfo(tz_str)
    except Exception:
        if '/' in tz_str:
            # IANA name that didn't resolve — usually Windows without the tzdata package
            raise ValueError(
                f"Timezone '{tz_str}' not found. On Windows, install the timezone "
                f"database with:  python -m pip install tzdata\n"
                f"Or pass a UTC offset instead (e.g. '-0400').")
    m = re.match(r'^([+-])(\d{2}):?(\d{2})$', tz_str.strip())
    if m:
        sign = 1 if m.group(1) == '+' else -1
        offset = datetime.timedelta(hours=int(m.group(2)), minutes=int(m.group(3))) * sign
        return datetime.timezone(offset)
    raise ValueError(
        f"Cannot parse timezone: '{tz_str}'\n"
        f"Use an IANA name (e.g. 'America/Chicago', 'Europe/Rome') "
        f"or a UTC offset (e.g. '-0500', '+01:00')."
    )

def to_utc(dt, tz):
    """Attach tz to a naive datetime and convert to UTC."""
    return dt.replace(tzinfo=tz).astimezone(datetime.timezone.utc)

# ─── AUTO TIMEZONE DETECTION ─────────────────────────────────────────────────

# nginx log format: [08/Jun/2026:08:28:35 -0500]
_NGINX_TZ_RE = re.compile(r'\[[\d/\w]+:[\d:]+\s+([+-]\d{4})\]')

def auto_detect_system_tz(base_dir):
    """
    Detect the PBX system timezone by scanning nginx log entries in wms.log files.
    nginx timestamps always carry an explicit UTC offset (e.g. -0500), making them
    the most reliable timezone source in the archive.

    Returns a datetime.timezone offset object, or UTC if nothing is found.
    """
    wms_candidates = sorted(Path(base_dir).rglob('wms.log'))
    # Also check rotated files if current log has no nginx lines
    wms_candidates += sorted(Path(base_dir).rglob('wms.log.*.gz'))

    for wms_path in wms_candidates:
        try:
            opener = gzip.open(str(wms_path), 'rt', errors='replace') \
                     if str(wms_path).endswith('.gz') else \
                     open(str(wms_path), 'r', errors='replace')
            with opener as f:
                for line in f:
                    m = _NGINX_TZ_RE.search(line)
                    if m:
                        offset_str = m.group(1)  # e.g. "-0500"
                        sign = 1 if offset_str[0] == '+' else -1
                        hours   = int(offset_str[1:3])
                        minutes = int(offset_str[3:5])
                        offset  = datetime.timedelta(hours=hours, minutes=minutes) * sign
                        tz = datetime.timezone(offset)
                        print(f"  Auto-detected system timezone: UTC{offset_str} "
                              f"(from nginx entries in {Path(wms_path).name})")
                        return tz, offset_str
        except Exception:
            continue

    print("  Warning: could not auto-detect system timezone — defaulting to UTC")
    return datetime.timezone.utc, "+0000"

# ─── TIMESTAMP PARSERS ───────────────────────────────────────────────────────

MONTHS = {
    'Jan': 1, 'Feb': 2, 'Mar': 3, 'Apr': 4, 'May': 5,  'Jun': 6,
    'Jul': 7, 'Aug': 8, 'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dec': 12,
}

_SYSLOG_RE = re.compile(r'^(\w{3})\s{1,2}(\d{1,2})\s+(\d{2}:\d{2}:\d{2})')
_CW_RE     = re.compile(r'^\[(\w{3})\s{1,2}(\d{1,2})\s+(\d{2}:\d{2}:\d{2})\]')

SERIAL_LINE_RE = re.compile(rb'serial \[([0-9a-f]{12})\]')
WILDIXIN_RE = re.compile(rb'(?<![a-z0-9.-])([a-z0-9][a-z0-9-]*)\.wildixin\.com')
HEX12_RE = re.compile(rb'^[0-9a-f]{12}$')


def detect_pbx_identity(base_dir, max_bytes=30_000_000):
    """
    Identify the PBX the archive came from: its serial (12 hex chars, e.g. 2211000012ab, also the
    pbxid and the <serial>.wildixin.com hostname) and its domain (<name>.wildixin.com).
    Used to match the archive with a WMS backup of the same PBX (wildix-config-review).
    """
    serial_counts, domain_counts, explicit = {}, {}, None
    logs = sorted(Path(base_dir).rglob('wms.log')) + sorted(Path(base_dir).rglob('wms.log.*.gz')) +         sorted(Path(base_dir).rglob('cw.log'))
    for lp in logs:
        try:
            opener = gzip.open if lp.suffix == '.gz' else open
            with opener(lp, 'rb') as f:
                data = f.read(max_bytes)
        except OSError:
            continue
        m = SERIAL_LINE_RE.search(data)
        if m and not explicit:
            explicit = m.group(1).decode()
        for name in WILDIXIN_RE.findall(data):
            bucket = serial_counts if HEX12_RE.match(name) else domain_counts
            bucket[name.decode()] = bucket.get(name.decode(), 0) + 1
        if explicit and domain_counts:
            break
    serial = explicit or (max(serial_counts, key=serial_counts.get) if serial_counts else None)
    domain = max(domain_counts, key=domain_counts.get) if domain_counts else None
    return {
        'serial': serial,
        'domain': f'{domain}.wildixin.com' if domain else None,
        'serial_source': 'wms.log "serial [..]" line' if explicit else ('most frequent <serial>.wildixin.com'
                                                                          if serial else 'not found'),
    }


def _parse_ts(match, year, system_tz):
    """Shared timestamp assembly from a regex match of (month, day, HH:MM:SS)."""
    month = MONTHS.get(match.group(1))
    if not month:
        return None
    try:
        h, mi, s = map(int, match.group(3).split(':'))
        dt = datetime.datetime(year, month, int(match.group(2)), h, mi, s)
        return to_utc(dt, system_tz)
    except Exception:
        return None

def parse_syslog_ts(line, year, system_tz):
    """Parse 'Jun  8 08:28:22' syslog timestamps (wms.log, authorization.log)."""
    m = _SYSLOG_RE.match(line)
    return _parse_ts(m, year, system_tz) if m else None

def parse_cw_ts(line, year, system_tz):
    """Parse '[May 19 09:36:16]' callweaver timestamps."""
    m = _CW_RE.match(line)
    return _parse_ts(m, year, system_tz) if m else None

def infer_year_from_log(path, current_year):
    """
    For rotated/archived cw logs: if filename contains a year, use it.
    Otherwise fall back to current year.
    NOTE: Year-boundary edge cases (e.g. Dec log read in Jan) are not handled —
    acceptable for now since archive snapshots are typically collected same-year.
    """
    m = re.search(r'(\d{4})', Path(path).name)
    if m:
        year = int(m.group(1))
        if 2020 <= year <= current_year + 1:
            return year
    return current_year

# ─── CALL FILTER ─────────────────────────────────────────────────────────────

def build_call_patterns(extensions, callerid_names, callerid_nums, usernames):
    """Compile regex patterns for call-related filtering."""
    patterns = []
    for ext in (extensions or []):
        # Match extension as a word boundary to avoid matching e.g. "1170" when looking for "117"
        patterns.append(re.compile(r'\b' + re.escape(str(ext)) + r'\b', re.IGNORECASE))
    for name in (callerid_names or []):
        patterns.append(re.compile(re.escape(name), re.IGNORECASE))
    for num in (callerid_nums or []):
        patterns.append(re.compile(re.escape(str(num)), re.IGNORECASE))
    for user in (usernames or []):
        patterns.append(re.compile(re.escape(user), re.IGNORECASE))
    return patterns

def matches_call_filter(text, patterns):
    """Return True if text matches any pattern, or if no patterns are set."""
    if not patterns:
        return True
    return any(p.search(text) for p in patterns)

# ─── LOG FILE FILTERING ──────────────────────────────────────────────────────

def open_log(path):
    """Open a log file transparently handling .gz compression."""
    path = str(path)
    if path.endswith('.gz'):
        return gzip.open(path, 'rt', encoding='utf-8', errors='replace')
    return open(path, 'r', encoding='utf-8', errors='replace')

def filter_log_entries(line_iter, start_utc, end_utc, ts_parser, year, system_tz, call_patterns):
    """
    Filter log entries by time range and optional call patterns.

    Handles multi-line log entries: lines without a leading timestamp are treated
    as continuations of the previous timestamped entry. The entire accumulated
    entry (all its lines joined) is checked against call_patterns so that
    call-identifying info in any part of the entry is not missed.

    Returns list of (utc_datetime, formatted_text) sorted by time.
    """
    results = []
    current_ts = None
    current_lines = []

    def flush():
        if current_ts is None or not current_lines:
            return
        if current_ts < start_utc or current_ts > end_utc:
            return
        full = '\n'.join(current_lines)
        # Expand #012 escape sequences used by WMS syslog to embed newlines
        full = full.replace('#012', '\n    ')
        if not matches_call_filter(full, call_patterns):
            return
        results.append((current_ts, full))

    for raw_line in line_iter:
        line = raw_line.rstrip('\n')
        ts = ts_parser(line, year, system_tz)
        if ts is not None:
            flush()
            current_ts = ts
            current_lines = [line]
        else:
            current_lines.append(line)

    flush()
    results.sort(key=lambda x: x[0])
    return results

# ─── PCAP FILTERING ──────────────────────────────────────────────────────────

PCAP_GLOBAL_HDR = 24
PCAP_PKT_HDR    = 16

def _pcap_endian(magic):
    if magic == b'\xd4\xc3\xb2\xa1':
        return '<'
    if magic == b'\xa1\xb2\xc3\xd4':
        return '>'
    return None

def pcap_time_range(path):
    """Return (first_utc, last_utc) for a pcap file, or (None, None) on failure."""
    try:
        with open(path, 'rb') as f:
            magic = f.read(4)
            endian = _pcap_endian(magic)
            if not endian:
                return None, None
            _, _, _, _, snaplen, _ = struct.unpack(endian + 'HHiIII', f.read(20))
            ts_sec, _, incl_len, _ = struct.unpack(endian + 'IIII', f.read(PCAP_PKT_HDR))
            first = datetime.datetime.fromtimestamp(ts_sec, tz=datetime.timezone.utc)

            # Scan tail of file for last valid packet header
            size = os.path.getsize(path)
            f.seek(max(PCAP_GLOBAL_HDR, size - 200_000))
            tail = f.read()
            last_ts = ts_sec
            i = 0
            while i + PCAP_PKT_HDR <= len(tail):
                s, us, il, ol = struct.unpack(endian + 'IIII', tail[i:i+PCAP_PKT_HDR])
                if 1_000_000_000 < s < 2_000_000_000 and 0 < il <= min(snaplen, 65536):
                    last_ts = s
                    i += PCAP_PKT_HDR + il
                else:
                    i += 1
            last = datetime.datetime.fromtimestamp(last_ts, tz=datetime.timezone.utc)
            return first, last
    except Exception:
        return None, None

def filter_pcap(input_path, output_path, start_utc, end_utc,
                call_patterns=None, is_siprtp=False):
    """
    Copy packets from input_path to output_path that fall within [start_utc, end_utc].

    For siponly pcaps: also applies call_patterns to packet payload text.
    For siprtp pcaps: time filter only (RTP packets won't contain extension/caller info).

    Returns number of packets written.
    """
    start_ts = start_utc.timestamp()
    end_ts   = end_utc.timestamp()
    count    = 0

    try:
        with open(input_path, 'rb') as fin:
            magic = fin.read(4)
            endian = _pcap_endian(magic)
            if not endian:
                return 0
            global_rest = fin.read(20)
            _, _, _, _, snaplen, _ = struct.unpack(endian + 'HHiIII', global_rest)
            max_incl = min(snaplen, 65536)

            with open(output_path, 'wb') as fout:
                fout.write(magic + global_rest)
                while True:
                    hdr = fin.read(PCAP_PKT_HDR)
                    if len(hdr) < PCAP_PKT_HDR:
                        break
                    ts_sec, ts_usec, incl_len, orig_len = struct.unpack(endian + 'IIII', hdr)
                    if incl_len > max_incl:
                        break  # Corrupt record
                    data = fin.read(incl_len)
                    if len(data) < incl_len:
                        break

                    pkt_time = ts_sec + ts_usec / 1_000_000
                    if pkt_time < start_ts or pkt_time > end_ts:
                        continue

                    # Apply call filter to SIP text payload (skip for siprtp mode
                    # because RTP packets have no text and would always be excluded)
                    if call_patterns and not is_siprtp:
                        payload = data.decode('latin-1', errors='replace')
                        if not matches_call_filter(payload, call_patterns):
                            continue

                    fout.write(hdr)
                    fout.write(data)
                    count += 1

        if count == 0 and os.path.exists(output_path):
            os.remove(output_path)

    except Exception as e:
        print(f"    Warning: error reading {os.path.basename(input_path)}: {e}")

    return count

# ─── FILE DISCOVERY ──────────────────────────────────────────────────────────

def discover_files(base_dir):
    """Walk the extracted archive and categorize all relevant files."""
    cats = {
        'wms_logs':          [],   # wms.log, wms.log.N.gz
        'cw_logs':           [],   # cw.log, cw.log.N.gz
        'cw_archives':       [],   # cw_log_archive_YYYYMMDD.tar.gz
        'auth_logs':         [],   # authorization.log
        'subscription_logs': [],   # subscriptions*.log
        'mtr_logs':          [],   # mtr.log, mtr.log.N.gz
        'ping_logs':         [],   # ping_result.txt
        'siponly_pcaps':     [],   # siponly.pcapN
        'siprtp_pcaps':      [],   # siprtp.pcapN
        'dialplan':          [],   # dialplan.txt
        'public_ip':         [],   # public_ip
        'reports':           [],   # reports/report_*.json
    }

    for path in sorted(Path(base_dir).rglob('*')):
        if not path.is_file() or path.name.startswith('.'):
            continue
        name = path.name
        if   re.fullmatch(r'wms\.log(\.\d+\.gz)?',           name): cats['wms_logs'].append(path)
        elif re.fullmatch(r'cw\.log(\.\d+\.gz)?',            name): cats['cw_logs'].append(path)
        elif re.fullmatch(r'cw_log_archive_\d+\.tar\.gz',    name): cats['cw_archives'].append(path)
        elif name == 'authorization.log':                            cats['auth_logs'].append(path)
        elif re.fullmatch(r'subscriptions.*\.log',            name): cats['subscription_logs'].append(path)
        elif re.fullmatch(r'mtr\.log(\.\d+\.gz)?',           name): cats['mtr_logs'].append(path)
        elif name == 'ping_result.txt':                              cats['ping_logs'].append(path)
        elif re.fullmatch(r'siponly\.pcap\d*',                name): cats['siponly_pcaps'].append(path)
        elif re.fullmatch(r'siprtp\.pcap\d*',                 name): cats['siprtp_pcaps'].append(path)
        elif name == 'dialplan.txt':                                 cats['dialplan'].append(path)
        elif name == 'public_ip':                                    cats['public_ip'].append(path)
        elif re.fullmatch(r'report_.*\.json',                 name): cats['reports'].append(path)

    return cats

# ─── CW ARCHIVE HELPERS ──────────────────────────────────────────────────────

def iter_cw_archive_lines(archive_path):
    """Yield text lines from inside a cw_log_archive_YYYYMMDD.tar.gz."""
    with tarfile.open(str(archive_path), 'r:gz') as tar:
        for member in tar.getmembers():
            if member.isfile():
                f = tar.extractfile(member)
                if f:
                    for line in io.TextIOWrapper(f, encoding='utf-8', errors='replace'):
                        yield line

# ─── ARCHIVE EXTRACTION ──────────────────────────────────────────────────────

def extract_archive(archive_path, dest):
    """Extract a .tar.gz to dest, return the effective root directory."""
    with tarfile.open(str(archive_path), 'r:gz') as tar:
        try:
            tar.extractall(dest, filter='data')   # safe extraction (Py 3.12+)
        except TypeError:
            tar.extractall(dest)
    entries = [e for e in os.listdir(dest) if not e.startswith('.')]
    if len(entries) == 1:
        candidate = os.path.join(dest, entries[0])
        if os.path.isdir(candidate):
            return candidate
    return dest

# ─── PCAP DECODING (pure Python — no tcpdump/tshark needed) ─────────────────

_SIP_START_RE = re.compile(
    rb'^(SIP/2\.0 \d{3}|(INVITE|ACK|BYE|CANCEL|REGISTER|OPTIONS|PRACK|SUBSCRIBE|'
    rb'NOTIFY|PUBLISH|INFO|REFER|MESSAGE|UPDATE) \S+ SIP/2\.0)')
_CALLID_RE = re.compile(r'^(?:Call-ID|i)\s*:\s*(\S+)', re.IGNORECASE | re.MULTILINE)
_CSEQ_RE   = re.compile(r'^CSeq\s*:\s*(.+)$', re.IGNORECASE | re.MULTILINE)

# Digest auth headers: mask the per-challenge/per-response values, keep username, realm, uri,
# algorithm and qop visible because they're what you need to diagnose 401/403/407 loops.
_AUTH_HDR_RE = re.compile(
    r'^(?:Proxy-)?Authorization[ \t]*:.*$|^(?:WWW|Proxy)-Authenticate[ \t]*:.*$'
    r'|^(?:Proxy-)?Authentication-Info[ \t]*:.*$', re.IGNORECASE | re.MULTILINE)
_DIGEST_SECRET_RE = re.compile(
    r'\b(response|nonce|cnonce|opaque|nextnonce|rspauth)(\s*=\s*)("[^"]*"|[^\s,]+)', re.IGNORECASE)

def mask_sip_auth(text):
    """Replace digest response/nonce/cnonce/opaque values in SIP auth headers with [REDACTED]."""
    def mask_param(m):
        value = '"[REDACTED]"' if m.group(3).startswith('"') else '[REDACTED]'
        return m.group(1) + m.group(2) + value
    return _AUTH_HDR_RE.sub(lambda h: _DIGEST_SECRET_RE.sub(mask_param, h.group(0)), text)

def iter_pcap_packets(path):
    """Yield (ts_float, linktype, frame_bytes) for every record in a pcap file."""
    with open(path, 'rb') as f:
        magic = f.read(4)
        endian = _pcap_endian(magic)
        if not endian:
            return
        _, _, _, _, snaplen, linktype = struct.unpack(endian + 'HHiIII', f.read(20))
        max_incl = min(snaplen, 65536) if snaplen else 65536
        while True:
            hdr = f.read(PCAP_PKT_HDR)
            if len(hdr) < PCAP_PKT_HDR:
                return
            ts_sec, ts_usec, incl_len, _ = struct.unpack(endian + 'IIII', hdr)
            if incl_len > max_incl:
                return
            data = f.read(incl_len)
            if len(data) < incl_len:
                return
            yield ts_sec + ts_usec / 1_000_000, linktype, data

def _l2_to_ip(linktype, d):
    """Strip the link-layer header; return (ethertype, ip_bytes) or (None, None)."""
    try:
        if linktype == 1:                       # Ethernet
            et, off = struct.unpack('!H', d[12:14])[0], 14
            while et in (0x8100, 0x88A8):        # VLAN tags
                et, off = struct.unpack('!H', d[off+2:off+4])[0], off + 4
            return et, d[off:]
        if linktype == 113:                     # Linux cooked (SLL)
            return struct.unpack('!H', d[14:16])[0], d[16:]
        if linktype == 276:                     # Linux cooked v2 (SLL2)
            return struct.unpack('!H', d[0:2])[0], d[20:]
        if linktype in (101, 228, 229, 12, 14): # Raw IP
            v = d[0] >> 4
            return (0x0800 if v == 4 else 0x86DD), d
        if linktype == 0:                       # BSD loopback
            v = d[4] >> 4
            return (0x0800 if v == 4 else 0x86DD), d[4:]
    except Exception:
        pass
    return None, None

def _ip_to_transport(et, ip):
    """Return (src, dst, proto, l4_bytes) or None."""
    try:
        if et == 0x0800:
            ihl = (ip[0] & 0x0F) * 4
            proto = ip[9]
            src = str(ipaddress.IPv4Address(ip[12:16]))
            dst = str(ipaddress.IPv4Address(ip[16:20]))
            total = struct.unpack('!H', ip[2:4])[0]
            return src, dst, proto, ip[ihl:total if total else None]
        if et == 0x86DD:
            proto = ip[6]
            src = str(ipaddress.IPv6Address(ip[8:24]))
            dst = str(ipaddress.IPv6Address(ip[24:40]))
            return src, dst, proto, ip[40:]
    except Exception:
        pass
    return None

def decode_packet(linktype, frame):
    """Return dict(src, sport, dst, dport, proto, payload) or None."""
    et, ip = _l2_to_ip(linktype, frame)
    if ip is None:
        return None
    t = _ip_to_transport(et, ip)
    if not t:
        return None
    src, dst, proto, l4 = t
    try:
        if proto == 17 and len(l4) >= 8:
            sport, dport = struct.unpack('!HH', l4[:4])
            return dict(src=src, sport=sport, dst=dst, dport=dport, proto='UDP', payload=l4[8:])
        if proto == 6 and len(l4) >= 20:
            sport, dport = struct.unpack('!HH', l4[:4])
            off = (l4[12] >> 4) * 4
            return dict(src=src, sport=sport, dst=dst, dport=dport, proto='TCP', payload=l4[off:])
    except Exception:
        pass
    return None

def _looks_like_rtp(p):
    if len(p) < 12 or (p[0] >> 6) != 2:
        return False
    pt = p[1] & 0x7F
    return not (72 <= pt <= 76)    # 72-76 overlap RTCP types 200-204

def _looks_like_rtcp(p):
    return len(p) >= 8 and (p[0] >> 6) == 2 and 200 <= p[1] <= 204

RTP_PT_NAMES = {0: 'PCMU/G.711u', 3: 'GSM', 4: 'G.723', 8: 'PCMA/G.711a', 9: 'G.722',
                18: 'G.729', 101: 'telephone-event (DTMF)'}

def pcap_to_text(pcap_path, user_tz, user_tz_display, redact=True):
    """
    Decode a filtered pcap into readable text:
      1. A one-line-per-message SIP ladder (time, direction, request/response, Call-ID)
      2. An RTP stream summary (packets, duration, codec, sequence gaps) per 5-tuple
      3. Full SIP message bodies (headers + SDP), with digest auth values masked unless redact=False
    All timestamps are shown in the user's timezone.
    """
    sip_msgs, rtp, rtcp_count, undecoded, tls_count = [], {}, 0, 0, 0
    try:
        for ts, linktype, frame in iter_pcap_packets(str(pcap_path)):
            pkt = decode_packet(linktype, frame)
            if not pkt:
                undecoded += 1
                continue
            pl = pkt['payload']
            if not pl:
                continue
            if _SIP_START_RE.match(pl):
                sip_msgs.append((ts, pkt, pl.decode('utf-8', errors='replace')))
            elif 5061 in (pkt['sport'], pkt['dport']):
                tls_count += 1
            elif pkt['proto'] == 'UDP' and _looks_like_rtcp(pl):
                rtcp_count += 1
            elif pkt['proto'] == 'UDP' and _looks_like_rtp(pl):
                key = (pkt['src'], pkt['sport'], pkt['dst'], pkt['dport'])
                seq = struct.unpack('!H', pl[2:4])[0]
                ssrc = struct.unpack('!I', pl[8:12])[0]
                s = rtp.setdefault(key, dict(count=0, first=ts, last=ts, pts=set(),
                                             ssrcs=set(), first_seq=seq, last_seq=seq))
                s['count'] += 1
                s['last'] = ts
                s['last_seq'] = seq
                s['pts'].add(pl[1] & 0x7F)
                s['ssrcs'].add(ssrc)
    except Exception as e:
        return f"[Could not decode {pcap_path.name}: {e}]"

    def fmt(ts):
        dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).astimezone(user_tz)
        return dt.strftime('%Y-%m-%d %H:%M:%S.') + f"{dt.microsecond // 1000:03d}"

    out = [f"# Decoded by wildix_analyzer.py — timestamps in {user_tz_display}",
           f"# Open {pcap_path.name} in Wireshark for full packet detail.", ""]

    # ── SIP ladder ──
    out.append(f"## SIP LADDER ({len(sip_msgs)} messages)")
    callid_short = {}
    for ts, p, text in sip_msgs:
        first = text.split('\n', 1)[0].rstrip('\r')
        m = _CALLID_RE.search(text)
        cid = m.group(1) if m else '?'
        tag = callid_short.setdefault(cid, f"C{len(callid_short) + 1}")
        cseq = _CSEQ_RE.search(text)
        cseq = cseq.group(1).strip() if cseq else ''
        out.append(f"  {fmt(ts)}  [{tag}] {p['src']}:{p['sport']} -> {p['dst']}:{p['dport']} "
                   f"{p['proto']}  {first}   (CSeq {cseq})")
    if callid_short:
        out.append("")
        out.append("  Call-ID legend:")
        for cid, tag in callid_short.items():
            out.append(f"    {tag} = {cid}")
    out.append("")

    # ── RTP summary ──
    if rtp or rtcp_count:
        out.append(f"## RTP STREAMS ({len(rtp)} streams, {rtcp_count} RTCP packets)")
        for (src, sport, dst, dport), s in sorted(rtp.items(), key=lambda kv: kv[1]['first']):
            # Expected count from sequence span (assumes at most one 16-bit wrap)
            span = (s['last_seq'] - s['first_seq']) % 65536 + 1
            lost = max(0, span - s['count'])
            dur = s['last'] - s['first']
            codecs = ', '.join(RTP_PT_NAMES.get(pt, f"PT{pt}") for pt in sorted(s['pts']))
            out.append(f"  {fmt(s['first'])} -> {fmt(s['last'])}  ({dur:6.1f}s)  "
                       f"{src}:{sport} -> {dst}:{dport}  pkts={s['count']}  "
                       f"est_lost={lost} ({lost / span * 100:.1f}%)  "
                       f"codec={codecs}  ssrc_changes={len(s['ssrcs']) - 1}")
        out.append("  Tip: a stream in one direction with no matching reverse stream = one-way audio candidate.")
        out.append("")

    if tls_count:
        out.append(f"## NOTE: {tls_count} packets on port 5061 (SIP-TLS) are encrypted and were not decoded.")
        out.append("")
    if undecoded:
        out.append(f"## NOTE: {undecoded} packets had an unsupported link/network layer and were skipped.")
        out.append("")

    # ── Full SIP messages ──
    out.append("## FULL SIP MESSAGES" + ("" if redact else " (digest auth values NOT masked: --no-redact)"))
    for ts, p, text in sip_msgs:
        out.append(f"--- {fmt(ts)}  {p['src']}:{p['sport']} -> {p['dst']}:{p['dport']} ({p['proto']}) ---")
        text = text.replace('\r\n', '\n').rstrip()
        out.append(mask_sip_auth(text) if redact else text)
        out.append("")
    return '\n'.join(out)

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def run(args):
    current_year = datetime.datetime.now().year

    # ── Resolve input path first (needed for auto-tz before printing summary) ─
    input_path = Path(args.archive)
    if not input_path.exists():
        print(f"ERROR: '{input_path}' does not exist")
        sys.exit(1)

    temp_dir = None
    if input_path.is_file() and str(input_path).endswith('.tar.gz'):
        print(f"Extracting {input_path.name} ...")
        temp_dir = tempfile.mkdtemp(prefix='wildix_')
        import atexit
        atexit.register(shutil.rmtree, temp_dir, ignore_errors=True)
        base_dir = extract_archive(str(input_path), temp_dir)
        print(f"  → {base_dir}")
    else:
        base_dir = str(input_path)

    # ── Timezone setup ────────────────────────────────────────────────────────
    system_tz_str = args.system_tz
    try:
        if args.system_tz.lower() == 'auto':
            system_tz, system_tz_str = auto_detect_system_tz(base_dir)
        else:
            system_tz = parse_timezone(args.system_tz)
        user_tz = parse_timezone(args.user_tz) if args.user_tz else system_tz
    except ValueError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    pbx_identity = detect_pbx_identity(base_dir)

    # ── Parse time range (input is in user_tz) ────────────────────────────────
    def parse_dt(s):
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
            try:
                return to_utc(datetime.datetime.strptime(s, fmt), user_tz)
            except ValueError:
                pass
        raise ValueError(f"Cannot parse datetime: '{s}' — use YYYY-MM-DD HH:MM:SS")

    try:
        start_utc = parse_dt(args.start)
        end_utc   = parse_dt(args.end)
    except ValueError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    if start_utc >= end_utc:
        print("ERROR: --start must be before --end")
        sys.exit(1)

    # ── Call filter patterns ───────────────────────────────────────────────────
    call_patterns = build_call_patterns(
        args.extension, args.callerid_name, args.callerid_num, args.username
    )

    # ── Print summary of what we're doing ─────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  Wildix Autotrace Analyzer")
    print(f"{'='*60}")
    print(f"  Archive:      {args.archive}")
    print(f"  PBX:          {pbx_identity['domain'] or '?'} (serial {pbx_identity['serial'] or '?'})")
    print(f"  System TZ:    {system_tz_str} (auto-detected)" if args.system_tz.lower() == 'auto' else f"  System TZ:    {args.system_tz}")
    print(f"  User TZ:      {args.user_tz or system_tz_str}")
    print(f"  Time range:   {start_utc.astimezone(user_tz).strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"            →   {end_utc.astimezone(user_tz).strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"  (UTC)         {start_utc.strftime('%Y-%m-%d %H:%M:%S')} → {end_utc.strftime('%Y-%m-%d %H:%M:%S')}")
    if call_patterns:
        parts = []
        if args.extension:     parts.append(f"extensions={args.extension}")
        if args.callerid_name: parts.append(f"callerid_name={args.callerid_name}")
        if args.callerid_num:  parts.append(f"callerid_num={args.callerid_num}")
        if args.username:      parts.append(f"username={args.username}")
        print(f"  Call filter:  {', '.join(parts)}")
    print(f"{'='*60}\n")

    # ── Discover files ─────────────────────────────────────────────────────────
    files = discover_files(base_dir)
    total_files = sum(len(v) for v in files.values())
    print(f"Files discovered ({total_files} total):")
    for cat, paths in files.items():
        if paths:
            print(f"  {cat:<22} {len(paths)} file(s)")

    # ── Output directory ───────────────────────────────────────────────────────
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / 'pbx_identity.json').write_text(
        json.dumps(dict(pbx_identity, archive=Path(args.archive).name), indent=2), encoding='utf-8')

    # ── Anomaly check: flag very old pcap files ────────────────────────────────
    anomalous_pcaps = []
    age_threshold = datetime.datetime.now(tz=datetime.timezone.utc) - datetime.timedelta(days=60)

    # ── Filter WMS logs ────────────────────────────────────────────────────────
    print("\n--- WMS logs ---")
    wms_results = []
    for lp in sorted(files['wms_logs']):
        with open_log(lp) as f:
            r = filter_log_entries(f, start_utc, end_utc, parse_syslog_ts,
                                   current_year, system_tz, call_patterns)
        # callweaver also logs to syslog — drop those duplicates when cw.log is present
        if files['cw_logs']:
            r = [e for e in r if ' callweaver[' not in e[1]]
        wms_results.extend(r)
        print(f"  {lp.name:<30}  {len(r)} entries")

    # ── Filter authorization log ───────────────────────────────────────────────
    print("\n--- Authorization log ---")
    auth_results = []
    for lp in files['auth_logs']:
        with open_log(lp) as f:
            r = filter_log_entries(f, start_utc, end_utc, parse_syslog_ts,
                                   current_year, system_tz, call_patterns)
        auth_results.extend(r)
        print(f"  {lp.name:<30}  {len(r)} entries")

    # ── Filter callweaver logs ─────────────────────────────────────────────────
    print("\n--- Callweaver logs ---")
    cw_results = []

    # Current + rotated (.gz)
    for lp in sorted(files['cw_logs']):
        year = infer_year_from_log(lp, current_year)
        with open_log(lp) as f:
            r = filter_log_entries(f, start_utc, end_utc, parse_cw_ts,
                                   year, system_tz, call_patterns)
        cw_results.extend(r)
        print(f"  {lp.name:<30}  {len(r)} entries")

    # Monthly cw_log_archive_YYYYMMDD.tar.gz files
    for ap in sorted(files['cw_archives']):
        year = infer_year_from_log(ap, current_year)
        lines = iter_cw_archive_lines(ap)
        r = filter_log_entries(lines, start_utc, end_utc, parse_cw_ts,
                               year, system_tz, call_patterns)
        cw_results.extend(r)
        if r:
            print(f"  {ap.name:<30}  {len(r)} entries")

    cw_results.sort(key=lambda x: x[0])

    # ── Filter PCAPs ───────────────────────────────────────────────────────────
    print("\n--- PCAPs ---")
    pcap_outputs = []   # list of (out_path, original_name, count)

    def process_pcaps(pcap_list, label, is_siprtp):
        for pp in sorted(pcap_list):
            size = pp.stat().st_size
            if size == 0:
                print(f"  {pp.name:<28}  EMPTY — skipping")
                continue

            first_ts, last_ts = pcap_time_range(str(pp))
            if first_ts is None:
                print(f"  {pp.name:<28}  Cannot read header — skipping")
                continue

            # Flag anomalously old files
            if last_ts < age_threshold:
                anomalous_pcaps.append((pp.name, first_ts, last_ts))

            range_str = (f"{first_ts.strftime('%Y-%m-%d %H:%M')} → "
                         f"{last_ts.strftime('%Y-%m-%d %H:%M')} UTC")

            # Skip if no overlap with requested range
            if last_ts < start_utc or first_ts > end_utc:
                print(f"  {pp.name:<28}  [{range_str}]  no overlap — skipping")
                continue

            out_path = output_dir / f"filtered_{pp.stem}.pcap"
            count = filter_pcap(str(pp), str(out_path), start_utc, end_utc,
                                call_patterns if not is_siprtp else None,
                                is_siprtp=is_siprtp)
            note = " (time-filter only — RTP packets excluded from text search)" if is_siprtp and call_patterns else ""
            print(f"  {pp.name:<28}  [{range_str}]  {count} packets → {out_path.name}{note}")
            if count > 0:
                pcap_outputs.append((out_path, pp.name, count))

    process_pcaps(files['siponly_pcaps'], 'siponly', is_siprtp=False)
    process_pcaps(files['siprtp_pcaps'],  'siprtp',  is_siprtp=True)

    # ── Dump SIP content from filtered pcaps ──────────────────────────────────
    pcap_text_sections = []
    for out_path, orig_name, count in pcap_outputs:
        text = pcap_to_text(out_path, user_tz, args.user_tz or system_tz_str,
                            redact=not args.no_redact)
        pcap_text_sections.append((orig_name, out_path.name, count, text))

    # ── Write filtered report ─────────────────────────────────────────────────
    report_path = output_dir / 'filtered_report.txt'
    print(f"\nWriting report → {report_path}")

    user_tz_display = args.user_tz or system_tz_str

    with open(report_path, 'w', encoding='utf-8') as out:
        def w(s=''):
            out.write(s + '\n')

        w("=" * 70)
        w("  WILDIX AUTOTRACE FILTERED REPORT")
        w("=" * 70)
        w(f"  Generated:    {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        w(f"  Archive:      {args.archive}")
        w(f"  PBX:          {pbx_identity['domain'] or '?'} (serial {pbx_identity['serial'] or '?'}, "
          f"{pbx_identity['serial_source']})")
        w(f"  System TZ:    {system_tz_str}" + (" (auto-detected)" if args.system_tz.lower() == 'auto' else ""))
        w(f"  Time range:   {start_utc.astimezone(user_tz).strftime('%Y-%m-%d %H:%M:%S')} "
          f"→ {end_utc.astimezone(user_tz).strftime('%Y-%m-%d %H:%M:%S')} ({user_tz_display})")
        w(f"  Time range:   {start_utc.strftime('%Y-%m-%d %H:%M:%S')} "
          f"→ {end_utc.strftime('%Y-%m-%d %H:%M:%S')} (UTC)")
        if call_patterns:
            w(f"  Call filter:  {', '.join(parts)}")
        w()

        # Anomalous pcap warning
        if anomalous_pcaps:
            w("⚠️  ANOMALOUS PCAP FILES DETECTED (much older than other files)")
            w("   This can happen when the pcap capture mode was changed (e.g. siponly ↔ siprtp)")
            w("   and old rotation files were not cleared. These files are still searched.")
            for name, first, last in anomalous_pcaps:
                w(f"   {name}: {first.date()} → {last.date()}")
            w()

        # Static context: public IP, ping, MTR
        for pip in files['public_ip']:
            w("─" * 70)
            w("  PBX PUBLIC IP")
            w("─" * 70)
            w(pip.read_text().strip())
            w()

        for pp in files['ping_logs']:
            w("─" * 70)
            w("  PING RESULTS (to 8.8.8.8)")
            w("─" * 70)
            w(pp.read_text())

        for mp in files['mtr_logs']:
            w("─" * 70)
            w(f"  MTR RESULTS ({mp.name})")
            w("─" * 70)
            with open_log(mp) as f:
                w(f.read())

        for rp in sorted(files['reports']):
            w("─" * 70)
            w(f"  USER REPORT: {rp.name}")
            w("─" * 70)
            w(rp.read_text())
            w()

        # Dialplan context
        for dp in files['dialplan']:
            w("─" * 70)
            w("  DIALPLAN (DID rules)")
            w("─" * 70)
            w(dp.read_text())
            w()

        # Subscriptions — archives keep one file per day for weeks, so only include
        # files dated within a day of the requested window (undated files always)
        win_dates = {(start_utc.astimezone(system_tz) + datetime.timedelta(days=d)).date()
                     for d in range(-1, (end_utc - start_utc).days + 2)}
        for sp in files['subscription_logs']:
            m = re.search(r'(\d{4}-\d{2}-\d{2})', sp.name)
            if m and datetime.date.fromisoformat(m.group(1)) not in win_dates:
                continue
            # Files are 10-minute snapshots headed "Time: YYYY-MM-DD HH:MM:SS";
            # keep only snapshots within 10 min of the window
            with open_log(sp) as f:
                content = f.read()
            blocks = re.split(r'(?m)^(?=Time: \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', content)
            kept = []
            for b in blocks:
                bm = re.match(r'Time: (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', b)
                if not bm:
                    continue
                bts = to_utc(datetime.datetime.strptime(bm.group(1), '%Y-%m-%d %H:%M:%S'), system_tz)
                if start_utc - datetime.timedelta(minutes=10) <= bts <= end_utc:
                    kept.append(b)
            if not kept and any(re.match(r'Time: ', b) for b in blocks):
                continue
            w("─" * 70)
            w(f"  SUBSCRIPTIONS: {sp.name}")
            w("─" * 70)
            w(''.join(kept) if kept else content)

        # ── WMS log ────────────────────────────────────────────────────────────
        w("=" * 70)
        w(f"  WMS LOG  ({len(wms_results)} entries in time range)")
        w("=" * 70)
        if wms_results:
            for ts, text in wms_results:
                ts_local = ts.astimezone(user_tz).strftime('%Y-%m-%d %H:%M:%S %Z')
                out.write(f"[{ts_local}] {text}\n\n")
        else:
            w("  (no entries in time range)")
        w()

        # ── Authorization log ──────────────────────────────────────────────────
        w("=" * 70)
        w(f"  AUTHORIZATION LOG  ({len(auth_results)} entries in time range)")
        w("=" * 70)
        if auth_results:
            for ts, text in auth_results:
                ts_local = ts.astimezone(user_tz).strftime('%Y-%m-%d %H:%M:%S %Z')
                out.write(f"[{ts_local}] {text}\n\n")
        else:
            w("  (no entries in time range)")
        w()

        # ── Callweaver log ─────────────────────────────────────────────────────
        w("=" * 70)
        w(f"  CALLWEAVER LOG  ({len(cw_results)} entries in time range)")
        w("=" * 70)
        if cw_results:
            for ts, text in cw_results:
                ts_local = ts.astimezone(user_tz).strftime('%Y-%m-%d %H:%M:%S %Z')
                out.write(f"[{ts_local}] {text}\n\n")
        else:
            w("  (no entries in time range)")
        w()

        # ── PCAP sections ──────────────────────────────────────────────────────
        if pcap_text_sections:
            for orig_name, out_name, count, text in pcap_text_sections:
                w("=" * 70)
                w(f"  SIP CAPTURE: {orig_name}  ({count} packets)")
                w(f"  Filtered file: {out_name}")
                w("=" * 70)
                w(text)
                w()
        else:
            w("=" * 70)
            w("  SIP CAPTURES")
            w("=" * 70)
            w("  (no pcap files matched the time range)")
            w()

        w("=" * 70)
        w("  END OF FILTERED REPORT")
        w("=" * 70)

    # ── Summary ────────────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  DONE")
    print(f"{'='*60}")
    print(f"  WMS log entries:     {len(wms_results)}")
    print(f"  Auth log entries:    {len(auth_results)}")
    print(f"  CW log entries:      {len(cw_results)}")
    print(f"  PCAP files written:  {len(pcap_outputs)}")
    print(f"  Report:              {report_path}")
    print(f"  Output dir:          {output_dir}")

    if not wms_results and not auth_results and not cw_results and not pcap_outputs:
        print(f"\n  ⚠️  No data found in the requested time range.")
        print(f"     Check that --system-tz is correct for this PBX.")

    if temp_dir:
        shutil.rmtree(temp_dir, ignore_errors=True)

    return str(report_path)


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    p = argparse.ArgumentParser(
        description='Filter Wildix Autotrace logs and pcaps by time range and call identity.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Timezone formats accepted:
  IANA name:    America/Chicago, America/New_York, Europe/Rome, Asia/Tokyo
  UTC offset:   -0500, +0100, -05:00, +01:00

Examples:
  # 10-minute window, CST system, viewed in Eastern time
  python3 wildix_analyzer.py archive.tar.gz \\
    --start "2026-06-08 08:30:00" --end "2026-06-08 08:40:00" \\
    --system-tz "America/Chicago" --user-tz "America/New_York"

  # Filter by extension 117, already-extracted directory
  python3 wildix_analyzer.py ./autotrace_logs-1700000000 \\
    --start "2026-06-08 08:30:00" --end "2026-06-08 08:35:00" \\
    --system-tz "America/Chicago" --extension 117

  # Multiple call filters
  python3 wildix_analyzer.py archive.tar.gz \\
    --start "2026-06-08 08:00:00" --end "2026-06-08 09:00:00" \\
    --system-tz "-0500" \\
    --extension 117 --extension 111 --callerid-num "+15555550100"
""")
    p.add_argument('archive',
        help='Path to .tar.gz archive or extracted directory')
    p.add_argument('--start', required=True,
        help='Start datetime in user timezone, e.g. "2026-06-08 08:30:00"')
    p.add_argument('--end', required=True,
        help='End datetime in user timezone, e.g. "2026-06-08 08:40:00"')
    p.add_argument('--system-tz', default='auto',
        help='PBX system timezone (IANA name, UTC offset, or "auto" to detect from logs) [default: auto]')
    p.add_argument('--user-tz', default=None,
        help='Your display timezone (default: same as --system-tz)')
    p.add_argument('--extension', action='append', metavar='EXT',
        help='Filter by extension number (repeatable)')
    p.add_argument('--callerid-name', action='append', metavar='NAME',
        help='Filter by caller ID name (repeatable)')
    p.add_argument('--callerid-num', action='append', metavar='NUM',
        help='Filter by caller ID number (repeatable)')
    p.add_argument('--username', action='append', metavar='USER',
        help='Filter by PBX username (repeatable)')
    p.add_argument('--output', default='./wildix_filtered',
        help='Output directory for filtered files (default: ./wildix_filtered)')
    p.add_argument('--no-redact', action='store_true',
        help="Show digest response/nonce/cnonce/opaque values in the report's full SIP messages "
             '(masked by default; the filtered .pcap files are never altered)')

    args = p.parse_args()
    run(args)


if __name__ == '__main__':
    main()
