#!/usr/bin/env python3
"""
Wildix WMS backup parser: settings, dialplans and call-flow tracing, offline.

Reads a standard WMS system backup (<host>_<date>_<time>_<version>_<serial>_wms_b_*.tar.gz,
or an extracted folder) and writes:
  <output>/config_summary.txt   human-readable review of park, users, queues, dialplans, trunks
  <output>/config.json          the same model as JSON (redacted)

Optionally traces a number through the dialplans (--trace) or prints one slice (--focus).

Credentials in the backup (SMTP, SIP trunk, LDAP passwords, PINs, keys) are never written:
only whitelisted files are read and every value under a secret-looking key is replaced
with [REDACTED]. Standard library only, Python 3.9+.

Examples:
  python wildix_config_parser.py backup_wms_b_x.tar.gz --output backup_wms_b_x
  python wildix_config_parser.py backup.tar.gz --output out --trace +17805551234 --from trunk:"Carrier Trunk"
  python wildix_config_parser.py backup.tar.gz --output out --trace 8006 --from dialplan:"queues testing"
  python wildix_config_parser.py backup.tar.gz --output out --focus park
"""

import argparse
import base64
import io
import json
import os
import re
import sqlite3
import sys
import tarfile
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lua51  # noqa: E402

REDACTED = '[REDACTED]'
# Key names whose values are credentials. Deliberately not plain "key" (FKEYS, HOTKEY are config).
SECRET_KEY_RE = re.compile(
    r'pass(word|wd)?|secret|token|(^|_)pin$|userpin|totp|twofactor|private|api_?key|auth(_?user|key)?$|cred',
    re.I)
# sip:user:password@host style credentials inside register strings
REGISTER_CRED_RE = re.compile(r'([?/])([^:@"\s]+):([^@"\s]+)(?=[:@"])')

LDIF_KEEP = {'uid', 'cn', 'givenName', 'sn', 'telephoneNumber', 'userKind', 'userDialplan',
             'userFaxDialplan', 'licenseType', 'ou', 'o', 'mail', 'wOfficePhone', 'wcgrp',
             'member', 'gid', 'pbxDomain', 'pbxHostname', 'pbxid', 'preferredLanguage', 'owner'}

BACKUP_NAME_RE = re.compile(
    r'^(?P<host>.+?)_(?P<date>\d{4}\.\d{2}\.\d{2})_(?P<time>\d{2}\.\d{2}\.\d{2})_'
    r'(?P<version>[\d.]+)_(?P<serial>[0-9a-fA-F]+)_wms_b_')

# Positional parameters of WMS dialplan applications (as stored in dialplan_rules.parameters).
# Names are from the WMS dialplan editor; "?" marks positions not yet confirmed.
APP_PARAMS = {
    'Set': ['variable', 'value'],
    'Goto': ['number', 'dialplan', 'priority', '?'],
    'GotoIf': ['condition', 'number', 'dialplan', 'priority', '?'],
    'DialPhone': ['number', 'timeout_s', 'options', '?'],
    'DialTrunk': ['number', 'trunk', '?', '?', '?', 'call_class'],
    'Queue': ['call_group_id', 'options', '?', '?', 'max_wait_s', 'class'],
    'Playback': ['sound', 'no_answer'],
    'Background': ['sound', '?'],
    'WaitExten': ['timeout_s'],
    'VoiceMail': ['mailbox', 'options'],
    'MuxMon': ['?', '?', 'options', '?'],
    'Timetable': ['timetable_id', 'number', 'dialplan', 'priority'],  # WMS 'Verify timetable'; priority not shown in the editor
    'FaxToEmail': ['email', 'format', 'station_id', 'options'],
    # "Jump to if ..." apps. The '?' after each priority has been empty in every backup seen so far.
    'GotoIfSwitchIsOn': ['switch_id', 'number', 'dialplan', 'priority', '?'],
    'GotoIf3SwitchIsOnOrBlinking': ['switch_id', 'number_on', 'dialplan_on', 'priority_on', '?',
                                    'number_blinking', 'dialplan_blinking', 'priority_blinking', '?'],
    'GotoIfCallType': ['call_type', 'number', 'dialplan', 'priority', '?'],
    'GotoIfBusyCongested': ['dial_status', 'number', 'dialplan', 'priority', '?'],
    'Answer': [],
}
# Apps that jump when a condition holds and otherwise continue with the next rule:
# app -> [(branch, number index, dialplan index)]. ELSE_BRANCH names the "continue" branch.
CONDITIONAL_JUMPS = {
    'GotoIf': [('true', 1, 2)],
    'Timetable': [('open', 1, 2)],
    'GotoIfSwitchIsOn': [('on', 1, 2)],
    'GotoIf3SwitchIsOnOrBlinking': [('on', 1, 2), ('blinking', 5, 6)],
    'GotoIfCallType': [('match', 1, 2)],
    'GotoIfBusyCongested': [('match', 1, 2)],
}
ELSE_BRANCH = {'GotoIf': 'false', 'Timetable': 'closed', 'GotoIfSwitchIsOn': 'off', 'GotoIf3SwitchIsOnOrBlinking': 'off',
               'GotoIfCallType': 'otherwise', 'GotoIfBusyCongested': 'otherwise'}
# Switches are dialplan gates, not tied to a timetable. wmsswitches.swtype 3 = 3-state, otherwise 2-state;
# state 0 off, 1 on (WMS shows "enabled"), 2 blinking (WMS shows "extra"). timetable_main.state 1 = "check time"
# (follow the schedule); other timetable states aren't confirmed yet.
SWITCH_STATES = {0: 'off', 1: 'on', 2: 'blinking'}
# Reserved dialplan: the PBX itself sends a call here when a parked call's ring-back to the parker isn't answered
# (or the parker is busy), and when an unanswered blind/semi-attended transfer returns (if Return from transfer is
# on). Nothing points at it. Its rule is the number WMS shows as "default", stored as 's'.
RECEPTIONIST_DP = 'receptionist'
TIMETABLE_STATES = {1: 'check time'}
BUILTIN_DIALPLANS = {'default', 'internalcalls', 'users_internal', 'pbxinternal'}  # pbxinternal: WMS 'Users and call groups'
WILDCARD_CHARS = set('XNZ.![')
# callweaver/sip-<name>.conf files that are not trunk definitions
NOT_TRUNK_CONFS = {'wms', 'extra', 'registration', 'mashup', 'custom', 'general-custom', 'classound-custom'}


# ---------------------------------------------------------------- redaction

def redact(obj, parent_key=''):
    if isinstance(obj, dict):
        return {k: (REDACTED if SECRET_KEY_RE.search(str(k)) and v not in ('', None, {}, [])
                    else redact(v, k)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v, parent_key) for v in obj]
    if isinstance(obj, str):
        return REGISTER_CRED_RE.sub(lambda m: f'{m.group(1)}{REDACTED}:{REDACTED}', obj)
    return obj


# ---------------------------------------------------------------- backup access

class Backup:
    """Read members of the outer tar(.gz) or an extracted folder, plus nested tars, in memory."""

    def __init__(self, path):
        self.path = Path(path)
        self._tar = None
        if self.path.is_file():
            self._tar = tarfile.open(self.path, 'r:*')
            self._names = {m.name.lstrip('./'): m for m in self._tar.getmembers() if m.isfile()}
        elif self.path.is_dir():
            self._names = None
        else:
            raise SystemExit(f'Backup not found: {path}')
        self._nested = {}

    def read(self, name):
        if self._tar is not None:
            m = self._names.get(name)
            return self._tar.extractfile(m).read() if m else None
        p = self.path / name
        return p.read_bytes() if p.is_file() else None

    def nested(self, tar_name):
        if tar_name not in self._nested:
            data = self.read(tar_name)
            if data is None:
                self._nested[tar_name] = None
            else:
                t = tarfile.open(fileobj=io.BytesIO(data), mode='r:*')
                self._nested[tar_name] = {m.name.lstrip('./'): t.extractfile(m).read()
                                          for m in t.getmembers() if m.isfile()}
        return self._nested[tar_name]

    def nested_file(self, tar_name, name):
        n = self.nested(tar_name)
        return n.get(name) if n else None


def text(b):
    return b.decode('utf-8', 'replace') if b is not None else None


# ---------------------------------------------------------------- format parsers

def parse_ldif(data):
    entries, cur, last = [], {}, None
    for raw in data.splitlines() + ['']:
        if raw.startswith(' ') and last:            # folded continuation
            cur[last][-1] += raw[1:]
            continue
        if not raw.strip():
            if cur:
                entries.append(cur)
            cur, last = {}, None
            continue
        if raw.startswith('#') or ':' not in raw:
            continue
        k, v = raw.split(':', 1)
        if v.startswith(':'):                        # base64 value
            try:
                v = base64.b64decode(v[1:].strip()).decode('utf-8', 'replace')
            except Exception:
                v = '<binary>'
        else:
            v = v.strip()
        cur.setdefault(k, []).append(v)
        last = k
    # base64 decoding happens after folding is complete
    return entries


def parse_kv_conf(data):
    out = {}
    for line in data.splitlines():
        line = line.strip()
        if not line or line.startswith(('#', ';', '[')) or '=' not in line:
            continue
        k, v = line.split('=', 1)
        out[k.strip().rstrip('>').strip()] = v.strip().lstrip('>').strip().strip('"')
    return out


def parse_ini_sections(data):
    sections, cur = {}, None
    for line in data.splitlines():
        line = line.strip()
        if not line or line.startswith((';', '#')):
            continue
        m = re.match(r'^\[(.+)\]$', line)
        if m:
            cur = sections.setdefault(m.group(1), {'member': []})
            continue
        if cur is None or '=' not in line:
            continue
        k, v = line.split('=', 1)
        k = k.strip().rstrip('>').strip()
        v = v.strip().lstrip('>').strip()
        if k == 'member':
            cur['member'].append(v)
        elif k == 'allow' and k in cur:
            cur[k] += ',' + v
        else:
            cur[k] = v
    return sections


def split_params(s):
    """'"a","b\\,c",""' -> ['a', 'b,c', '']. CustomApp parameters are returned whole."""
    if s is None:
        return []
    out, buf, inq, i = [], '', False, 0
    while i < len(s):
        ch = s[i]
        if ch == '\\' and i + 1 < len(s):
            buf += s[i + 1]
            i += 2
            continue
        if ch == '"':
            inq = not inq
        elif ch == ',' and not inq:
            out.append(buf)
            buf = ''
        else:
            buf += ch
        i += 1
    out.append(buf)
    return out


# ---------------------------------------------------------------- patterns

def is_pattern(number):
    n = number[1:] if number.startswith('_') else number
    return any(c in WILDCARD_CHARS for c in n)


def pattern_regex(number):
    n = number[1:] if number.startswith('_') else number
    out, i = '', 0
    while i < len(n):
        c = n[i]
        if c == 'X':
            out += '[0-9]'
        elif c == 'Z':
            out += '[1-9]'
        elif c == 'N':
            out += '[2-9]'
        elif c == '.':
            out += '.+'
        elif c == '!':
            out += '.*'
        elif c == '[':
            j = n.find(']', i)
            if j < 0:
                out += re.escape(c)
            else:
                out += '[' + n[i + 1:j].replace('\\', '\\\\') + ']'
                i = j
        else:
            out += re.escape(c)
        i += 1
    return re.compile('^' + out + '$')


def specificity(number):
    """Sort key: exact numbers first, then patterns by per-position choice count (Asterisk-like)."""
    if not is_pattern(number):
        return (0, ())
    n = number[1:] if number.startswith('_') else number
    key, i = [], 0
    while i < len(n):
        c = n[i]
        if c == 'X':
            key.append(10)
        elif c == 'Z':
            key.append(9)
        elif c == 'N':
            key.append(8)
        elif c == '.':
            key.append(1000)
        elif c == '!':
            key.append(1001)
        elif c == '[':
            j = n.find(']', i)
            body = n[i + 1:j] if j > 0 else ''
            cnt = sum((ord(body[k + 2]) - ord(body[k]) + 1) if k + 2 < len(body) and body[k + 1] == '-' else 1
                      for k in range(len(body)) if body[k] != '-' and not (k > 0 and body[k - 1] == '-'))
            key.append(max(cnt, 1))
            i = j if j > 0 else i
        else:
            key.append(1)
        i += 1
    return (1, tuple(key))


def matches(number_rule, dialed):
    if number_rule == dialed:
        return True
    return is_pattern(number_rule) and bool(pattern_regex(number_rule).match(dialed))


# ---------------------------------------------------------------- model

def load_model(backup):
    model = {'source': {}, 'system': {}, 'park': {}, 'users': [], 'groups': [], 'dialplans': {},
             'dialplan_links': [], 'queues': {}, 'trunks': [], 'outgoing_groups': [], 'timetables': [],
             'switches': [], 'ivr': [], 'paging': [], 'warnings': [], 'parse_notes': []}
    notes = model['parse_notes']

    m = BACKUP_NAME_RE.match(backup.path.name)
    model['source'] = {'file': backup.path.name}
    if m:
        model['source'].update({
            'hostname': m.group('host'), 'backup_time': f"{m.group('date').replace('.', '-')} {m.group('time').replace('.', ':')}",
            'version': m.group('version'), 'serial': m.group('serial')})

    # --- configs.tar
    tz = text(backup.nested_file('configs.tar', 'timezone'))
    model['system']['timezone'] = tz.strip() if tz else None
    comp = text(backup.nested_file('configs.tar', 'pbx/company.conf'))
    if comp:
        model['system'].update({k: v for k, v in parse_kv_conf(comp).items() if not SECRET_KEY_RE.search(k)})
    ev = text(backup.nested_file('configs.tar', 'callweaver/extensions-variables.conf'))
    gvars = parse_kv_conf(ev) if ev else {}
    model['system']['globals'] = {k: v for k, v in gvars.items() if not k.startswith('#')}
    if not ev:
        notes.append('callweaver/extensions-variables.conf missing: park timeout unknown')
    feat = text(backup.nested_file('configs.tar', 'callweaver/features.conf'))
    model['system']['features_conf'] = parse_kv_conf(feat) if feat else {}
    qents = text(backup.nested_file('configs.tar', 'callweaver/queues-entities.conf'))
    rendered_queues = parse_ini_sections(qents) if qents else {}

    # --- wmsdb.sql
    sql = text(backup.read('wmsdb.sql'))
    if sql is None:
        raise SystemExit('wmsdb.sql not found in backup: is this a WMS system backup?')
    db = sqlite3.connect(':memory:')
    db.executescript(sql)
    db.row_factory = sqlite3.Row

    def rows(q):
        try:
            return [dict(r) for r in db.execute(q)]
        except sqlite3.OperationalError as e:
            notes.append(f'SQL: {e}')
            return []

    for r in rows('select variable, value from system'):
        v = r['value']
        try:
            v = json.loads(v)
        except (TypeError, ValueError):
            pass
        model['system'][f"db.{r['variable']}"] = v

    names = {r['id']: r for r in rows('select * from dialplan_rulenames')}
    for did, r in names.items():
        model['dialplans'][r['rulename']] = {'id': did, 'description': r['ruledescription'] or '',
                                             'numbers': {}, 'includes': []}
    id2dp = {did: r['rulename'] for did, r in names.items()}
    for r in rows('select * from dialplan_rules order by rule_id, number, priority, id'):
        dp = id2dp.get(r['rule_id'])
        if dp is None:
            notes.append(f"dialplan_rules {r['id']} points to unknown dialplan id {r['rule_id']}")
            continue
        app = r['application']
        params = [r['parameters']] if app == 'CustomApp' else split_params(r['parameters'])
        model['dialplans'][dp]['numbers'].setdefault(r['number'], []).append({
            'rule_id': r['id'], 'priority': r['priority'], 'app': app, 'params': params,
            'comment': r['comment'] or ''})
    for r in rows('select * from dialplan_rules_includes order by rule_id, priority'):
        dp = id2dp.get(r['rule_id'])
        inc = id2dp.get(r['include_rule_id'], f"<built-in {r['include_rule_id']}>")
        if dp:
            model['dialplans'][dp]['includes'].append(inc)
    link_types = {1: 'dialplan', 2: 'trunk', 3: 'call_group'}
    for r in rows('select * from dialplan_links'):
        model['dialplan_links'].append({'from': id2dp.get(r['object_from_id'], r['object_from_id']),
                                        'to_type': link_types.get(r['object_to_type'], r['object_to_type']),
                                        'to': unquote(r['object_to_id'] or '')})

    members = {}
    for r in rows('select * from call_groups_members order by id'):
        members.setdefault(r['call_group_id'], []).append(
            {'member': r['group_member'].split('@')[0], 'penalty': r['penalty']})
    for r in rows('select * from call_groups'):
        try:
            qp = json.loads(r['queue_parameters'] or '{}')
        except ValueError:
            qp = {}
        keep = {k: v for k, v in qp.items() if not k.startswith('queue-') or k in ('queue-cid', 'queue-manager')}
        rq = rendered_queues.get(str(r['id']), {})
        model['queues'][str(r['id'])] = {
            'id': r['id'], 'name': r['call_groupname'], 'params': keep,
            'members': members.get(r['id'], []),
            'rendered_members': rq.get('member', []),
        }

    for r in rows('select * from trunks'):
        try:
            s = json.loads(r['settings_pbx'] or '{}')
        except ValueError:
            s = {}
        for k in ('username', 'fromuser', 'defaultuser', 'authuser'):
            s.pop(k, None)  # SIP auth identity: half of the trunk credential
        try:
            w = json.loads(r['settings_wms'] or '{}')
        except ValueError:
            w = {}
        model['trunks'].append({
            'id': r['id'], 'type': 'sip', 'name': r['title'] or s.get('peername'),
            'host': s.get('host'), 'inbound_dialplan': s.get('context'),
            'transport': s.get('transport_wildix'), 'codecs': s.get('allow'), 'dtmf': s.get('dtmfmode'),
            'registers': w.get('enable_reg'), 'session_timer': s.get('session_timer'),
            'session_expires': s.get('session-expires'), 'qualify': s.get('qualify'),
            'settings': s})
    for r in rows('select * from pstn_trunks'):
        try:
            s = json.loads(r['settings_pbx'] or '{}')
        except ValueError:
            s = {}
        model['trunks'].append({'id': r['id'], 'type': r['technology'], 'name': r['username'],
                                'inbound_dialplan': s.get('context'), 'ports': r['ports'], 'settings': s})
    known = {t['name'] for t in model['trunks']}
    for fname, data in sorted((backup.nested('configs.tar') or {}).items()):
        m = re.match(r'callweaver/sip-([a-z0-9_-]+)\.conf$', fname)
        if not m or m.group(1) in NOT_TRUNK_CONFS or m.group(1).endswith('-custom'):
            continue
        for name, sec in parse_ini_sections(text(data)).items():
            name = unquote(name)
            if sec.get('type') not in ('peer', 'friend') or name in known:
                continue
            model['trunks'].append({
                'id': None, 'type': f'sip (Wildix-managed, {fname})', 'name': name, 'host': sec.get('host'),
                'inbound_dialplan': sec.get('context'), 'codecs': sec.get('allow'), 'dtmf': sec.get('dtmfmode'),
                'transport': sec.get('transport_wildix'), 'session_timer': sec.get('session_timer'),
                'session_expires': sec.get('session-expires'), 'qualify': sec.get('qualify'),
                'settings': {k: v for k, v in sec.items() if k not in ('username', 'defaultuser', 'fromuser')}})
            known.add(name)
    og = {g['id']: dict(g, rules=[]) for g in rows('select * from outgoing_groups_main')}
    for r in rows('select * from outgoing_groups_rules order by group_id, priority'):
        if r['group_id'] in og:
            og[r['group_id']]['rules'].append(r)
    model['outgoing_groups'] = list(og.values())

    tt = {t['id']: dict(t, periods=[]) for t in rows('select * from timetable_main')}
    for r in rows('select * from timetable_additional order by parent_id, id'):
        if r['parent_id'] in tt:
            tt[r['parent_id']]['periods'].append(r)
    model['timetables'] = list(tt.values())
    model['switches'] = rows('select * from wmsswitches')
    model['ivr'] = rows('select * from ivr_tree')
    pg = {p['id']: dict(p, members=[]) for p in rows('select * from paging')}
    for r in rows('select * from paging_members'):
        if r['paging_id'] in pg:
            pg[r['paging_id']]['members'].append(r['group_member'])
    model['paging'] = list(pg.values())

    # --- LDAP users
    ldif = text(backup.read('ldapcat.ldif'))
    uid_users = {}
    if ldif:
        for e in parse_ldif(ldif):
            dn = e.get('dn', [''])[0]
            if dn.startswith('uid=') and 'userKind' in e:
                u = {k: (v[0] if len(v) == 1 else v) for k, v in e.items() if k in LDIF_KEEP}
                u['group'] = (e.get('owner', [''])[0].split(',')[0].split('=')[-1]) or None
                model['users'].append(u)
                uid_users[u.get('uid')] = u
            elif dn.startswith('gid='):
                model['groups'].append({'gid': e.get('gid', [''])[0], 'name': e.get('cn', [''])[0],
                                        'wcgrp': e.get('wcgrp', [''])[0]})
            elif dn.startswith('pbxid='):
                model['system']['pbx_ldap'] = {k: e[k][0] for k in ('pbxid', 'pbxHostname', 'pbxDomain') if k in e}
    else:
        notes.append('ldapcat.ldif missing: no user list')
    gid_names = {g['gid']: g['name'] for g in model['groups']}
    for u in model['users']:
        u['group_name'] = gid_names.get(u.get('group'))

    # --- per-user features (compiled Lua)
    utar = backup.nested('users.tar') or {}
    for fname, data in utar.items():
        uid = Path(fname).stem
        res = lua51.decode(data)
        u = uid_users.get(uid)
        if u is None:
            continue
        if res['ok']:
            u['features'] = summarize_features(res['globals'].get('features') or {})
        else:
            u['features'] = {'decode_error': res['error'],
                             'strings': [s for s in res['strings'] if not SECRET_KEY_RE.search(s)][:200]}
            notes.append(f"user {u.get('telephoneNumber')} features only partially decoded: {res['error']}")

    model['users'].sort(key=lambda u: (u.get('userKind') != 'user', natural(u.get('telephoneNumber', ''))))

    # --- park
    timeout_ms = gvars.get('GPARK_TIMEOUT')
    orbits = [u for u in model['users'] if u.get('userKind') == 'park_orbit']
    orbit_nums = {u.get('telephoneNumber') for u in orbits}
    callback_rules = []
    for dp_name, dp in model['dialplans'].items():
        for num, rules in dp['numbers'].items():
            if any('park' in r['comment'].lower() for r in rules) or num in orbit_nums:
                callback_rules.append({'dialplan': dp_name, 'number': num, 'rules': rules})
    park_keys = []
    for u in model['users']:
        for k in (u.get('features') or {}).get('function_keys', []):
            if k['type'] == 'PORBIT':
                park_keys.append({'user': u.get('telephoneNumber'), 'name': u.get('cn'),
                                  'orbit': k['number'], 'label': k['label']})
    rdp = model['dialplans'].get(RECEPTIONIST_DP)
    rft = gvars.get('RETURNFROMTRANSFER')
    model['park'] = {
        'return_route': ({'dialplan': RECEPTIONIST_DP, 'number': 's', 'rules': rdp['numbers']['s']}
                         if rdp and 's' in rdp['numbers'] else None),
        # Wildix: "Return from transfer" is off by default with the USA country code unless RETURNFROMTRANSFER=yes.
        # Country code 1 is all of North America, so treat it as probably off there. Park returns aren't affected.
        'return_from_transfer': ('on (RETURNFROMTRANSFER=yes)' if str(rft).lower() == 'yes'
                                 else f'off (RETURNFROMTRANSFER={rft})' if rft
                                 else 'probably off (country code 1, RETURNFROMTRANSFER not set)'
                                 if gvars.get('GCOUNTRY_CODE') == '1' else 'on (default)'),
        'timeout_ms': int(timeout_ms) if timeout_ms and timeout_ms.isdigit() else timeout_ms,
        'timeout_source': 'configs.tar:callweaver/extensions-variables.conf GPARK_TIMEOUT',
        'orbits': [{'number': u.get('telephoneNumber'), 'name': u.get('cn'),
                    'dialplan': u.get('userDialplan')} for u in orbits],
        'callback_rules': callback_rules,
        'park_keys': park_keys,
        'legacy_features_conf': {k: model['system']['features_conf'].get(k)
                                 for k in ('parkext', 'parkpos', 'parkingtime') if k in model['system']['features_conf']},
    }

    model['warnings'] = find_warnings(model)
    return redact(model)


def natural(s):
    return [int(p) if p.isdigit() else p for p in re.split(r'(\d+)', str(s))]


FEATURE_NAMES = {
    'FCF': 'forward all calls', 'BCF': 'forward if busy', 'UCF': 'forward if no answer/unavailable',
    'TOUT': 'no-answer timeout (s)', 'MEXT': 'mobility extension (delay s)', 'CW': 'call waiting',
    'REJECT': 'reject calls', 'RING': 'ring', 'MOBCONFIRMATION': 'mobility confirmation',
    'EMAILMCN': 'missed-call email', 'SMSMCN': 'missed-call SMS',
}


def summarize_features(f):
    """Reduce the decoded features table to what matters for call flow."""
    out = {'status': (f.get('STATUS') or {}).get('value') or 'available'}
    classes = f.get('_classes') or {}
    out['by_class'] = {}
    for cls in ('external', 'internal', 'whitelist', 'blacklist'):
        per_status = classes.get(cls) or {}
        cls_out = {}
        for st in ('default', 'away', 'dnd'):
            feats = per_status.get(st) or {}
            active = {}
            for code, v in feats.items():
                if code.startswith('_') or not isinstance(v, dict):
                    continue
                if str(v.get('status')) == '1' and code not in ('EMAILMCN', 'SMSMCN', 'RING', 'CW'):
                    active[code] = v.get('value', True) or True
                if code == 'TOUT' and v.get('value'):
                    active['TOUT'] = v.get('value') if str(v.get('status')) == '1' else f"{v.get('value')} (off)"
            if active:
                cls_out[st] = active
        if cls_out:
            out['by_class'][cls] = cls_out
    keys = []
    raw = f.get('FKEYS')
    if isinstance(raw, str):
        for k in raw.split(','):
            parts = k.strip().split(' ')
            if not parts or parts[0] in ('', 'LINE'):
                continue
            keys.append({'type': parts[0], 'number': parts[1] if len(parts) > 1 else '',
                         'label': parts[2].replace('_', ' ') if len(parts) > 2 else '', 'raw': k.strip()})
    out['function_keys'] = keys
    cc = f.get('CALLCENTER') or {}
    if str(cc.get('enable')) == '1':
        out['callcenter'] = cc
    ap = f.get('ALTERNATE_PICKUP_MODE') or {}
    if any(ap.values()):
        out['pickup'] = ap
    hl = f.get('HOTLINE') or {}
    if str(hl.get('status')) == '1':
        out['hotline'] = hl
    return out


def find_warnings(model):
    w = []
    dps = model['dialplans']
    users_by_ext = {u.get('telephoneNumber'): u for u in model['users']}
    used = {u.get('userDialplan') for u in model['users']} | {t.get('inbound_dialplan') for t in model['trunks']}
    for dp_name, dp in dps.items():
        for num, rules in dp['numbers'].items():
            for r in rules:
                p = r['params'] + [''] * 9
                if r['app'] == 'Goto':
                    targets = [(p[1] or dp_name, p[0])]
                else:
                    targets = [(p[di] or dp_name, p[ni]) for _, ni, di in CONDITIONAL_JUMPS.get(r['app'], [])]
                for tgt, tnum in targets:
                    used.add(tgt)
                    if tgt not in dps and tgt not in BUILTIN_DIALPLANS:
                        w.append(f"[{dp_name}] {num} rule {r['rule_id']}: {r['app']} to unknown dialplan '{tgt}'")
                    elif tgt in dps:
                        if tnum and '$' not in tnum and not any(matches(n, tnum) for n in dps[tgt]['numbers']):
                            w.append(f"[{dp_name}] {num} rule {r['rule_id']}: {r['app']} to {tnum}@{tgt}, "
                                     f"but '{tgt}' has no rule matching {tnum}")
                if r['app'] in ('GotoIfSwitchIsOn', 'GotoIf3SwitchIsOnOrBlinking') and \
                        not any(str(x['id']) == p[0] for x in model['switches']):
                    w.append(f"[{dp_name}] {num} rule {r['rule_id']}: {r['app']} uses unknown switch id {p[0]}")
                if r['app'] == 'Queue':
                    q = model['queues'].get((r['params'] or [''])[0])
                    if q is None:
                        w.append(f"[{dp_name}] {num} rule {r['rule_id']}: Queue to unknown call group {r['params'][:1]}")
                    elif not q['members']:
                        w.append(f"[{dp_name}] {num} rule {r['rule_id']}: Queue '{q['name']}' has no members")
                if r['app'] == 'DialTrunk':
                    tname = unquote((r['params'] + ['', ''])[1])
                    if tname and tname not in {t['name'] for t in model['trunks']}:
                        w.append(f"[{dp_name}] {num} rule {r['rule_id']}: DialTrunk to '{tname}', not in the trunks "
                                 f"table (may be a trunk group or Wildix-managed trunk)")
            last = rules[-1]
            if last['app'] == 'Goto' and (last['params'] + ['', ''])[0] == num and \
                    ((last['params'] + ['', ''])[1] or dp_name) == dp_name:
                w.append(f"[{dp_name}] {num}: ends with Goto back to itself: callers loop until answered or they hang up")
    if RECEPTIONIST_DP in dps:
        used.add(RECEPTIONIST_DP)  # reserved: the PBX sends park/transfer returns here
        if 's' not in dps[RECEPTIONIST_DP]['numbers']:
            w.append(f"dialplan '{RECEPTIONIST_DP}' has no 'default' number (stored as 's'): unanswered park "
                     f"ring-backs and transfer returns have no route there")
    for dp_name in dps:
        if dp_name not in used and not any(l['to'] == dp_name for l in model['dialplan_links']):
            w.append(f"dialplan '{dp_name}' is not used by any user, trunk or Goto (unused or reached another way)")
    for q in model['queues'].values():
        if not q['members']:
            w.append(f"call group {q['id']} '{q['name']}' has no members")
    orbit_nums = {o['number'] for o in model['park']['orbits']}
    for k in model['park']['park_keys']:
        if k['orbit'] not in orbit_nums:
            w.append(f"user {k['user']} has a park-orbit key for '{k['orbit']}', but no park-orbit user has that "
                     f"number: check the key")
    for o in model['park']['orbits']:
        if not any(c['number'] == o['number'] for c in model['park']['callback_rules']):
            w.append(f"park orbit {o['number']} has no dialplan rule: timed-out calls use the default return to the parker")
    for u in model['users']:
        if u.get('userDialplan') and u['userDialplan'] not in dps and u['userDialplan'] not in BUILTIN_DIALPLANS:
            w.append(f"user {u.get('telephoneNumber')} uses unknown dialplan '{u['userDialplan']}'")
    for t in model['trunks']:
        if t.get('inbound_dialplan') and t['inbound_dialplan'] not in dps:
            w.append(f"trunk '{t['name']}' inbound dialplan '{t['inbound_dialplan']}' does not exist")
        elif t.get('inbound_dialplan') and not dps[t['inbound_dialplan']]['numbers']                 and not dps[t['inbound_dialplan']]['includes']:
            w.append(f"trunk '{t['name']}' inbound dialplan '{t['inbound_dialplan']}' is empty: inbound calls "
                     f"on it have no route")
    return w


# ---------------------------------------------------------------- formatting

def fmt_rule(r):
    names = APP_PARAMS.get(r['app'])
    p = r['params']
    if r['app'] == 'CustomApp':
        body = p[0] if p else ''
    elif names:
        parts = []
        for i, v in enumerate(p):
            if v == '':
                continue
            n = names[i] if i < len(names) else '?'
            parts.append(f'{v}' if n == '?' or i == 0 else f'{n}={v}')
        body = ', '.join(parts)
    else:
        body = ', '.join(x for x in p if x != '')
    s = f"{r['app']}({unquote(body)})"
    return s


def number_label(n):
    """A dialplan number as WMS shows it: 's' is the editor's "default"."""
    return 'default (s)' if n == 's' else n


def switch_info(model, sid):
    """{'id', 'name', 'kind' ('2-state'/'3-state'), 'state' (off/on/blinking, or the raw value)} for a switch id."""
    sw = next((x for x in model['switches'] if str(x['id']) == str(sid)), None)
    if sw is None:
        return {'id': sid, 'name': f'switch {sid} (unknown)', 'kind': '?', 'state': '?'}
    try:
        st = SWITCH_STATES.get(int(sw.get('state')), f"state {sw.get('state')}")
    except (TypeError, ValueError):
        st = f"state {sw.get('state')}"
    return {'id': sw['id'], 'name': sw['switchname'], 'kind': '3-state' if str(sw.get('swtype')) == '3' else '2-state',
            'state': st}


def timetable_name(model, tid):
    tt = next((t for t in model['timetables'] if str(t['id']) == str(tid)), None)
    return tt['timetable'] if tt else f'timetable {tid} (unknown)'


def condition_text(model, r):
    """Plain-language condition of a conditional jump, e.g. "switch 'Night' (2-state, on in the backup) is on"."""
    app, p = r['app'], r['params'] + [''] * 9
    if app == 'GotoIf':
        return unquote(p[0])
    if app == 'Timetable':
        return f"now is inside timetable '{timetable_name(model, p[0])}'"
    if app in ('GotoIfSwitchIsOn', 'GotoIf3SwitchIsOnOrBlinking'):
        sw = switch_info(model, p[0])
        return f"switch '{sw['name']}' ({sw['kind']}, {sw['state']} in the backup) is " + \
            ('on' if app == 'GotoIfSwitchIsOn' else 'on or blinking')
    if app == 'GotoIfCallType':
        return f'the call type is {p[0]}'
    if app == 'GotoIfBusyCongested':
        return f"the last dial ended {p[0] or 'BUSY/CONGESTED'}"
    return fmt_rule(r)


def jump_targets(r, dp_name, number):
    """[(branch, dialplan, number)] for a conditional jump rule; [] for any other app."""
    p = r['params'] + [''] * 9
    return [(branch, p[di] or dp_name, p[ni].replace('${EXTEN:}', number).replace('${EXTEN}', number) or number)
            for branch, ni, di in CONDITIONAL_JUMPS.get(r['app'], [])]


def describe_queue(model, qid):
    q = model['queues'].get(str(qid))
    if not q:
        return f'call group {qid} (unknown)'
    p = q['params']
    mem = ', '.join(user_label(model, m['member']) for m in q['members']) or 'NO MEMBERS'
    return (f"call group {q['id']} '{q['name']}': strategy {p.get('strategy')}, ring {p.get('timeout')} s, "
            f"retry {p.get('retry')} s, wrapup {p.get('wrapuptime')} s, maxlen {p.get('maxlen')}, "
            f"members: {mem}")


def user_label(model, ext):
    for u in model['users']:
        if u.get('telephoneNumber') == ext:
            kind = '' if u.get('userKind') == 'user' else f", {u.get('userKind')}"
            return f"{ext} ({u.get('cn')}{kind})"
    return ext


SHORT_FEATURE = {'FCF': 'fwd-all', 'BCF': 'fwd-busy', 'UCF': 'fwd-noanswer', 'TOUT': 'noanswer-timeout',
                 'MEXT': 'mobility-ext', 'REJECT': 'reject', 'MOBCONFIRMATION': 'mobility-confirm'}


def active_features(u, cls):
    """Features in force for a call class at the user's current status (falls back to default)."""
    f = u.get('features') or {}
    per = f.get('by_class', {}).get(cls, {})
    st = f.get('status') or 'available'
    return per.get(st) or per.get('default') or {}


def features_line(u, cls=None):
    f = u.get('features') or {}
    if 'decode_error' in f:
        return 'features: (not decoded)'
    parts = [f"status {f.get('status')}"]
    for c in f.get('by_class', {}):
        if cls and c not in ((cls,) if isinstance(cls, str) else cls):
            continue
        feats = active_features(u, c)
        items = ', '.join(f"{SHORT_FEATURE.get(k, k)}={v}" if v is not True else SHORT_FEATURE.get(k, k)
                          for k, v in feats.items())
        parts.append(f"{c}: {items or 'no forwards'}")
    keys = [' '.join(x for x in k['raw'].split(' ') if x) for k in f.get('function_keys', [])]
    if keys:
        parts.append('keys: ' + '; '.join(keys))
    return ' | '.join(parts)


def render_summary(model):
    L = []
    s = model['source']
    L.append('=' * 78)
    L.append('WILDIX CONFIG SUMMARY (from WMS backup, credentials redacted)')
    L.append('=' * 78)
    L.append(f"Backup file : {s.get('file')}")
    L.append(f"PBX         : {s.get('hostname', '?')}  domain {model['system'].get('pbx_domain', '?')}  "
             f"serial {s.get('serial', '?')}")
    L.append(f"Version     : {s.get('version', '?')}   backup taken {s.get('backup_time', '?')} (PBX local)")
    L.append(f"Timezone    : {model['system'].get('timezone')}")
    m = s.get('autotrace_match')
    if m is None:
        L.append('PBX match   : NOT CHECKED against an autotrace (use --autotrace to confirm this backup is '
                 'from the incident PBX)')
    else:
        L.append(f"PBX match   : {m['status'].upper()} with {Path(m['autotrace'].get('source', '')).name}: "
                 + '; '.join(m['details']))
    g = model['system'].get('globals', {})
    L.append(f"Globals     : park timeout GPARK_TIMEOUT={g.get('GPARK_TIMEOUT')} ms, "
             f"GINTDIAL_TIMEOUT={g.get('GINTDIAL_TIMEOUT')} s, country code {g.get('GCOUNTRY_CODE')}, "
             f"national prefix {g.get('GNATIONAL_PREFIX')}, intl prefix {g.get('GINTERNATIONAL_PREFIX')}")
    L.append('')
    L.extend(render_park(model))
    L.append('')
    L.append('-' * 78)
    L.append('USERS  (ext | name | kind | dialplan | group | features)')
    L.append('-' * 78)
    for u in model['users']:
        L.append(f"{u.get('telephoneNumber', '-'):>6} | {u.get('cn')} | {u.get('userKind')} | "
                 f"{u.get('userDialplan')} | {u.get('group_name') or '-'}")
        if u.get('features'):
            L.append(f"         {features_line(u, ('external', 'internal'))}")
    L.append('')
    L.append('-' * 78)
    L.append('CALL GROUPS / QUEUES')
    L.append('-' * 78)
    for q in model['queues'].values():
        L.append(describe_queue(model, q['id']))
        p = q['params']
        L.append(f"    joinempty={p.get('joinempty')} leavewhenempty={p.get('leavewhenempty')} "
                 f"ringinuse={p.get('ringinuse')} autopause={p.get('autopause')} "
                 f"announce-holdtime={p.get('announce-holdtime')} queue-cid={p.get('queue-cid') or '-'}")
    L.append('')
    L.append('-' * 78)
    L.append('TRUNKS')
    L.append('-' * 78)
    for t in model['trunks']:
        L.append(f"{t['name']} ({t['type']}): host {t.get('host')}, inbound dialplan '{t.get('inbound_dialplan')}', "
                 f"codecs {t.get('codecs')}, dtmf {t.get('dtmf')}, transport {t.get('transport')}, "
                 f"session timer {t.get('session_timer')}/{t.get('session_expires')}")
    for o in model['outgoing_groups']:
        L.append(f"trunk group {o['groupname']}: " + ', '.join(r['trunkname'] for r in o['rules']))
    L.append('')
    L.append('-' * 78)
    L.append('DIALPLANS  (number: priority app(params)  # comment)')
    L.append('-' * 78)
    users_by_dp = {}
    for u in model['users']:
        users_by_dp.setdefault(u.get('userDialplan'), []).append(u.get('telephoneNumber'))
    trunks_by_dp = {}
    for t in model['trunks']:
        trunks_by_dp.setdefault(t.get('inbound_dialplan'), []).append(t['name'])
    for dp_name, dp in model['dialplans'].items():
        used_by = []
        if trunks_by_dp.get(dp_name):
            used_by.append('trunks: ' + ', '.join(trunks_by_dp[dp_name]))
        if users_by_dp.get(dp_name):
            used_by.append('users: ' + ', '.join(sorted(users_by_dp[dp_name], key=natural)))
        L.append(f"[{dp_name}] {dp['description']}".rstrip())
        if used_by:
            L.append('  used by ' + '; '.join(used_by))
        if dp['includes']:
            L.append('  includes: ' + ', '.join(dp['includes']))
        if not dp['numbers']:
            L.append('  (no rules)')
        for num in sorted(dp['numbers'], key=specificity):
            rules = dp['numbers'][num]
            comment = {r['comment'] for r in rules if r['comment']}
            L.append(f"  {number_label(num)}" + (f"   # {'; '.join(sorted(comment))}" if comment else ''))
            for r in rules:
                L.append(f"      {r['priority']}. {fmt_rule(r)}   [rule {r['rule_id']}]")
        L.append('')
    if model['timetables'] or model['switches']:
        L.append('-' * 78)
        L.append('TIMETABLES / SWITCHES')
        L.append('-' * 78)
        for t in model['timetables']:
            st = TIMETABLE_STATES.get(t['state'], f"state {t['state']} (meaning not confirmed)")
            L.append(f"timetable {t['id']} '{t['timetable']}' {st}")
            for p in t['periods']:
                L.append(f"    dow {p['day_of_week_from']}-{p['day_of_week_to']} day {p['day_id_from']}-{p['day_id_to']} "
                         f"month {p['month_id']} {p['start_daytime']}-{p['end_daytime']} enabled={p['enabled_id']}")
        for sw in model['switches']:
            i = switch_info(model, sw['id'])
            L.append(f"switch {sw['id']} '{i['name']}' {i['kind']}, {i['state']} in the backup "
                     f"(a dialplan gate: see the 'Jump to if switch' rules that use it)")
        L.append('')
    L.append('-' * 78)
    L.append('WARNINGS')
    L.append('-' * 78)
    L.extend(model['warnings'] or ['(none)'])
    if model['parse_notes']:
        L.append('')
        L.append('Parse notes:')
        L.extend('  ' + n for n in model['parse_notes'])
    return '\n'.join(L) + '\n'


def render_park(model):
    p = model['park']
    L = ['-' * 78, 'CALL PARK', '-' * 78]
    t = p.get('timeout_ms')
    L.append(f"Park timeout: {t} ms ({int(t) / 1000:g} s)  [{p['timeout_source']}]" if isinstance(t, int)
             else f"Park timeout: {t}  [{p['timeout_source']}]")
    L.append('  cw.log shows this as "Nobody picked up in <timeout> ms" when a parked call times out.')
    if p.get('legacy_features_conf'):
        L.append(f"  (features.conf {p['legacy_features_conf']}: Asterisk parking lot, not used by Wildix park orbits)")
    L.append('Park orbits:')
    for o in p['orbits'] or [{'number': '(none)', 'name': '', 'dialplan': ''}]:
        L.append(f"  {o['number']}  {o['name']}  dialplan '{o['dialplan']}'")
    L.append('Dialplan rules for orbit numbers / commented "park":')
    if not p['callback_rules']:
        L.append('  (none): timed-out parked calls ring back the user who parked them')
    for c in p['callback_rules']:
        L.append(f"  [{c['dialplan']}] {c['number']}")
        for r in c['rules']:
            extra = ''
            if r['app'] == 'Queue':
                extra = '  -> ' + describe_queue(model, (r['params'] or [''])[0])
            L.append(f"      {r['priority']}. {fmt_rule(r)}{extra}   [rule {r['rule_id']}]"
                     + (f"  # {r['comment']}" if r['comment'] else ''))
    rr = p.get('return_route')
    L.append('After the park timeout the call rings back the parker (GINTDIAL_TIMEOUT). If the parker is busy or '
             "doesn't answer, and no forward of theirs takes the call:")
    L.append(f"  -> [{RECEPTIONIST_DP}] default (s): " + ('; '.join(fmt_rule(r) for r in rr['rules']) if rr
             else 'no such rule, so nothing else is tried (unconfirmed what the caller gets)'))
    L.append(f"Return from transfer (unanswered blind/semi-attended transfers): {p.get('return_from_transfer')}")
    L.append('Park keys on phones (FKEYS PORBIT):')
    for k in p['park_keys'] or [{'user': '(none found)', 'name': '', 'orbit': '', 'label': ''}]:
        L.append(f"  {k['user']} {k['name']}: orbit {k['orbit']} '{k['label']}'")
    return L


# ---------------------------------------------------------------- tracing

class Tracer:
    MAX_DEPTH = 40

    def __init__(self, model, call_class, origin_user=None):
        self.m = model
        self.cls = call_class
        self.origin_user = origin_user
        self.lines = []
        self.seen = set()

    def out(self, depth, s):
        self.lines.append('  ' * depth + s)

    def find(self, dp_name, number, visited_includes=None):
        """Return (dialplan_name, matched_number) honouring includes, or None."""
        dp = self.m['dialplans'].get(dp_name)
        if dp is None:
            return None
        cands = [n for n in dp['numbers'] if matches(n, number)]
        if cands:
            return dp_name, sorted(cands, key=specificity)[0]
        visited_includes = visited_includes or set()
        visited_includes.add(dp_name)
        for inc in dp['includes']:
            if inc in visited_includes:
                continue
            hit = self.find(inc, number, visited_includes)
            if hit:
                return hit
        return None

    def run(self, dp_name, number, depth=0, start_prio=None, via=''):
        if depth > self.MAX_DEPTH:
            self.out(depth, '... stopping: too deep')
            return
        if dp_name in BUILTIN_DIALPLANS:
            self.builtin(number, depth, f"Goto built-in '{dp_name}'")
            return
        hit = self.find(dp_name, number)
        if hit is None:
            if dp_name not in self.m['dialplans']:
                self.out(depth, f"dialplan '{dp_name}' does not exist")
                return
            self.builtin(number, depth, f"no rule for {number} in [{dp_name}]")
            return
        hdp, hnum = hit
        key = (hdp, hnum, start_prio)
        if key in self.seen:
            self.out(depth, f"LOOP: back to {hnum}@[{hdp}]. The call cycles until answered or the caller hangs up.")
            return
        self.seen.add(key)
        inc = f" (via include from [{dp_name}])" if hdp != dp_name else ''
        self.out(depth, f"[{hdp}] matches {number} with rule '{hnum}'{inc}{via}")
        rules = self.m['dialplans'][hdp]['numbers'][hnum]
        for r in rules:
            if start_prio is not None and r['priority'] < start_prio:
                continue
            if self.step(r, hdp, number, depth + 1) == 'stop':
                return
        self.out(depth + 1, 'end of rules: call is hung up')

    def step(self, r, dp_name, number, depth):
        app, p = r['app'], r['params'] + [''] * 6
        tag = f"[rule {r['rule_id']}]" + (f" # {r['comment']}" if r['comment'] else '')
        line = f"{r['priority']}. {fmt_rule(r)}  {tag}"
        if app == 'Goto':
            tnum = p[0].replace('${EXTEN}', number) or number
            self.out(depth, line)
            self.run(p[1] or dp_name, tnum, depth + 1, int(p[2]) if p[2].isdigit() and p[2] != '1' else None)
            return 'stop'
        if app in CONDITIONAL_JUMPS:
            self.out(depth, line)
            cond = condition_text(self.m, r)
            for branch, tdp, tnum in jump_targets(r, dp_name, number):
                if app == 'GotoIf3SwitchIsOnOrBlinking':
                    cond = f"{condition_text(self.m, r).rsplit(' is ', 1)[0]} is {branch}"
                self.out(depth + 1, f"IF {cond}{' is true' if app == 'GotoIf' else ''}:")
                self.run(tdp, tnum, depth + 2)
            self.out(depth + 1, 'ELSE ' + ('(outside hours) ' if app == 'Timetable' else '') + 'continue with the next rule')
            return None
        if app == 'FaxToEmail':
            self.out(depth, line + '  -> fax received and emailed; call ends here')
            return 'stop'
        if app == 'Queue':
            self.out(depth, line)
            self.out(depth + 1, describe_queue(self.m, p[0]))
            wait = p[4]
            self.out(depth + 1, f"caller waits in queue {'up to ' + wait + ' s' if wait else 'with no max wait set'}"
                     f"{', then continues to the next rule' if wait else ''}")
            return None
        if app == 'DialPhone':
            ext = p[0].replace('${EXTEN}', number)
            self.out(depth, line)
            self.describe_user(ext, depth + 1, p[1])
            return None
        if app == 'DialTrunk':
            self.out(depth, line + '  -> outbound; continues to the next rule only if the trunk call fails')
            return None
        if app in ('VoiceMail', 'Hangup'):
            self.out(depth, line + '  -> call ends here')
            return 'stop'
        if app in ('Background', 'WaitExten'):
            self.out(depth, line)
            if app == 'WaitExten':
                opts = [n for n in self.m['dialplans'][dp_name]['numbers'] if not is_pattern(n) and n != number]
                self.out(depth + 1, f"IVR: digits dialled now are looked up in [{dp_name}] "
                                    f"({', '.join(sorted(opts, key=natural)[:15])}); on timeout the next rule runs")
            return None
        self.out(depth, line)
        return None

    def describe_user(self, ext, depth, timeout):
        u = next((x for x in self.m['users'] if x.get('telephoneNumber') == ext), None)
        if u is None:
            self.out(depth, f"rings {ext} (not a user; may be a device, short number or pattern)")
            return
        kind = u.get('userKind')
        to = f" for {timeout} s" if timeout else " (user's own no-answer timeout)"
        self.out(depth, f"rings {user_label(self.m, ext)}{to}")
        if kind == 'park_orbit':
            pt = self.m['park'].get('timeout_ms')
            ring = self.m['system'].get('globals', {}).get('GINTDIAL_TIMEOUT')
            self.out(depth + 1, f"park orbit: caller hears music on hold for the park timeout ({pt} ms), then the "
                                f"call rings back the user who parked it for up to GINTDIAL_TIMEOUT={ring} s, using "
                                f"that user's INTERNAL call features (fwd-busy / fwd-noanswer apply)")
            parker = self.origin_user
            if parker:
                af = active_features(parker, 'internal')
                self.out(depth + 1, f"parker {user_label(self.m, parker.get('telephoneNumber'))} internal features: "
                                    + (', '.join(f"{SHORT_FEATURE.get(k, k)}={v}" for k, v in af.items())
                                       or 'no forwards'))
                if 'UCF' in af:
                    self.out(depth + 1, f"-> ring-back unanswered after {ring} s: probably forwarded to {af['UCF']}")
                if 'BCF' in af:
                    self.out(depth + 1, f"-> parker busy on another call when the park times out: probably "
                                        f"forwarded straight to {af['BCF']}")
            rr = self.m['park'].get('return_route')
            if rr:
                self.out(depth, f"if the parker is busy or doesn't answer and no forward takes the call, the PBX sends "
                                f"it to [{RECEPTIONIST_DP}] default (s) (confirmed on a test PBX with no forwards):")
                self.run(RECEPTIONIST_DP, 's', depth + 1)
            else:
                self.out(depth, f"no [{RECEPTIONIST_DP}] default rule: nothing is tried after the ring-back")
            self.out(depth, 'rules after this DialPhone run only if the ring-back ends without a forward (unconfirmed)')
            return
        elif u.get('features'):
            self.out(depth + 1, features_line(u, self.cls))
            af = active_features(u, self.cls)
            if 'FCF' in af:
                self.out(depth, f"forward-all is ON ({af['FCF']}): the call is forwarded before ringing; "
                                f"later rules probably do not run")
                return
            if 'UCF' in af:
                tout = af.get('TOUT')
                self.out(depth, f"forward-if-no-answer is ON ({af['UCF']}"
                                f"{', after ' + str(tout) + ' s' if tout and '(off)' not in str(tout) else ''}): "
                                f"if unanswered the call probably goes there and later rules do NOT run "
                                f"(confirm with a test call)")
                return
        self.out(depth, 'if unanswered (or busy/unavailable without a forward), the next rule runs')

    def builtin(self, number, depth, why):
        u = next((x for x in self.m['users'] if x.get('telephoneNumber') == number), None)
        if u:
            self.out(depth, f"{why}: built-in internal routing rings user")
            self.describe_user(number, depth + 1, '')
        else:
            self.out(depth, f"{why}: no matching rule and not a user extension: likely 'number not found'")


def trace(model, number, origin):
    kind, _, name = origin.partition(':')
    if kind == 'trunk':
        t = next((t for t in model['trunks'] if (t['name'] or '').lower() == name.lower()), None)
        if not t:
            raise SystemExit(f"trunk '{name}' not found. Trunks: {', '.join(x['name'] for x in model['trunks'])}")
        dp, cls, head = t['inbound_dialplan'], 'external', f"inbound on trunk '{t['name']}'"
    elif kind == 'user':
        u = next((u for u in model['users'] if u.get('telephoneNumber') == name), None)
        if not u:
            raise SystemExit(f"user {name} not found")
        dp, cls, head = u.get('userDialplan'), 'internal', f"dialled by {user_label(model, name)}"
    elif kind == 'dialplan':
        dp, cls, head = name, 'external', f"entering dialplan [{name}]"
    else:
        raise SystemExit("--from must be trunk:NAME, user:EXT or dialplan:NAME")
    tr = Tracer(model, cls, u if kind == 'user' else None)
    tr.lines.append(f"TRACE {number}: {head} -> dialplan [{dp}]  (call class: {cls})")
    tr.run(dp, number)
    return '\n'.join(tr.lines) + '\n'


def focus(model, what):
    kind, _, arg = what.partition(':')
    if kind == 'park':
        return '\n'.join(render_park(model)) + '\n'
    if kind == 'queue':
        q = model['queues'].get(arg) or next((q for q in model['queues'].values() if q['name'] == arg), None)
        if not q:
            raise SystemExit(f"call group {arg} not found")
        out = [describe_queue(model, q['id']), json.dumps(q['params'], indent=2), 'Used by:']
        for dp_name, dp in model['dialplans'].items():
            for num, rules in dp['numbers'].items():
                for r in rules:
                    if r['app'] == 'Queue' and r['params'] and r['params'][0] == str(q['id']):
                        out.append(f"  [{dp_name}] {num} {r['priority']}. {fmt_rule(r)}  [rule {r['rule_id']}]")
        return '\n'.join(out) + '\n'
    if kind == 'user':
        u = next((u for u in model['users'] if u.get('telephoneNumber') == arg), None)
        if not u:
            raise SystemExit(f"user {arg} not found")
        out = [user_label(model, arg), f"dialplan '{u.get('userDialplan')}', group {u.get('group_name')}",
               json.dumps(u.get('features'), indent=2), 'In call groups:']
        out += [f"  {q['id']} '{q['name']}'" for q in model['queues'].values()
                if any(m['member'] == arg for m in q['members'])]
        out.append('Referenced in dialplans:')
        for dp_name, dp in model['dialplans'].items():
            for num, rules in dp['numbers'].items():
                for r in rules:
                    if arg in r['params'] or num == arg:
                        out.append(f"  [{dp_name}] {num} {r['priority']}. {fmt_rule(r)}  [rule {r['rule_id']}]")
        return '\n'.join(out) + '\n'
    raise SystemExit('--focus must be park, queue:ID|NAME or user:EXT')


# ---------------------------------------------------------------- matching backup <-> autotrace

SERIAL_LINE_RE = re.compile(rb'serial \[([0-9a-f]{12})\]')
WILDIXIN_RE = re.compile(rb'(?<![a-z0-9.-])([a-z0-9][a-z0-9-]*)\.wildixin\.com')
HEX12_RE = re.compile(r'^[0-9a-f]{12}$')


def backup_identity(model):
    """Serial and domain of the PBX the backup came from."""
    src, sysm = model['source'], model['system']
    serial = src.get('serial') or (sysm.get('pbx_ldap') or {}).get('pbxid')
    domain = sysm.get('pbx_domain') or (sysm.get('pbx_ldap') or {}).get('pbxDomain')
    return {'serial': serial.lower() if serial else None, 'domain': domain.lower() if domain else None,
            'hostname': src.get('hostname')}


def _scan_identity(chunks):
    serial, serials, domains = None, {}, {}
    for data in chunks:
        m = SERIAL_LINE_RE.search(data)
        if m and not serial:
            serial = m.group(1).decode()
        for name in WILDIXIN_RE.findall(data):
            name = name.decode()
            bucket = serials if HEX12_RE.match(name) else domains
            bucket[name] = bucket.get(name, 0) + 1
        if serial and domains:
            break
    serial = serial or (max(serials, key=serials.get) if serials else None)
    domain = max(domains, key=domains.get) if domains else None
    return {'serial': serial, 'domain': f'{domain}.wildixin.com' if domain else None}


def autotrace_identity(path, max_bytes=30_000_000):
    """
    PBX identity of an autotrace: a wildix-diagnostics output folder (pbx_identity.json, or the PBX: line
    of filtered_report.txt), an extracted autotrace folder, or an autotrace_logs-*.tar.gz.
    """
    import gzip
    p = Path(path)
    if p.is_dir():
        j = p / 'pbx_identity.json'
        if j.is_file():
            d = json.loads(j.read_text(encoding='utf-8'))
            return {'serial': d.get('serial'), 'domain': d.get('domain'), 'source': str(j)}
        r = p / 'filtered_report.txt'
        if r.is_file():
            with open(r, encoding='utf-8', errors='replace') as f:
                for _, line in zip(range(40), f):
                    m = re.match(r'\s*PBX:\s+(\S+) \(serial (\w+)', line)
                    if m:
                        return {'serial': None if m.group(2) == '?' else m.group(2),
                                'domain': None if m.group(1) == '?' else m.group(1), 'source': str(r)}
            # older reports have no PBX: line; the filtered logs and SIP still carry the serial/domain
            with open(r, 'rb') as f:
                found = _scan_identity([f.read(max_bytes)])
            if found['serial'] or found['domain']:
                return dict(found, source=str(r))
        files = sorted(p.rglob('wms.log')) + sorted(p.rglob('wms.log.*.gz')) + sorted(p.rglob('cw.log'))

        def dir_chunks():
            for fp in files:
                with (gzip.open if fp.suffix == '.gz' else open)(fp, 'rb') as f:
                    yield f.read(max_bytes)
        return dict(_scan_identity(dir_chunks()), source=str(p))
    if p.is_file():
        t = tarfile.open(p, 'r:*')

        def tar_chunks():
            for m in t:
                base = m.name.rsplit('/', 1)[-1]
                if m.isfile() and (base == 'wms.log' or base.startswith('wms.log.') or base == 'cw.log'):
                    data = t.extractfile(m).read()
                    data = gzip.decompress(data) if base.endswith('.gz') else data
                    yield data[:max_bytes]
        return dict(_scan_identity(tar_chunks()), source=str(p))
    raise SystemExit(f'--autotrace path not found: {path}')


def check_match(model, trace_id):
    b = backup_identity(model)
    result = {'backup': b, 'autotrace': trace_id, 'status': 'unknown', 'details': []}
    if b['serial'] and trace_id.get('serial'):
        ok = b['serial'] == trace_id['serial'].lower()
        result['details'].append(f"serial: backup {b['serial']} vs autotrace {trace_id['serial']} "
                                 f"-> {'MATCH' if ok else 'DIFFERENT'}")
        result['status'] = 'match' if ok else 'mismatch'
    if b['domain'] and trace_id.get('domain'):
        ok = b['domain'] == trace_id['domain'].lower()
        result['details'].append(f"domain: backup {b['domain']} vs autotrace {trace_id['domain']} "
                                 f"-> {'MATCH' if ok else 'DIFFERENT'}")
        if not ok:
            result['status'] = 'mismatch'
        elif result['status'] == 'unknown':
            result['status'] = 'match'
    return result


# ---------------------------------------------------------------- mermaid diagrams

MERMAID_CLASSES = [
    'classDef step fill:#e8f0fe,stroke:#4a6fa5,color:#1a1a1a',
    'classDef person fill:#e6f4ea,stroke:#3c8d5a,color:#1a1a1a',
    'classDef vm fill:#eeeeee,stroke:#777,color:#1a1a1a',
    'classDef bad fill:#fde8e8,stroke:#c0392b,color:#1a1a1a',
    'classDef unused fill:#fff,stroke:#999,stroke-dasharray:5 5,color:#555',
]
MINOR_APPS = {'Set', 'CustomApp', 'MuxMon', 'Playback', 'Background', 'Answer'}


def mtext(t):
    """Escape one line of text for a quoted Mermaid label."""
    t = unquote(str(t))
    for a, b in (('#', '#35;'), ('"', '#quot;'), ('<', '#lt;'), ('>', '#gt;'), ('|', '#124;')):
        t = t.replace(a, b)
    return t


def mlabel(*lines):
    return '<br/>'.join(mtext(x) for x in lines if x not in (None, ''))


def _short(app_rule):
    """One-line description of a minor app for a node label."""
    app, p = app_rule['app'], app_rule['params'] + [''] * 4
    if app == 'Set':
        return f"Set {p[0]} = {p[1]}"
    if app in ('Playback', 'Background'):
        return f"{app} {p[0]}"
    if app == 'CustomApp':
        body = p[0]
        return None if body.startswith('NoOp(') else body[:60]
    if app == 'MuxMon':
        return 'Call recording on'
    if app == 'Answer':
        return None
    return fmt_rule(app_rule)


class MermaidBuilder:
    """Builds one flowchart. emit_rule() draws the path of a number through a dialplan, like Tracer."""
    MAX_NODES = 220

    def __init__(self, model, prefix, call_class='external', direction='TD'):
        self.m = model
        self.prefix = prefix
        self.cls = call_class
        self.lines = [f'flowchart {direction}'] + ['    ' + c for c in MERMAID_CLASSES]
        self.n = 0
        self.memo = {}
        self.tracer = Tracer(model, call_class)
        self.reached = set()
        self.truncated = False

    def node(self, label, shape='rect', cls='step'):
        self.n += 1
        nid = f'{self.prefix}{self.n}'
        o, c = {'rect': ('["', '"]'), 'round': ('(["', '"])'), 'diamond': ('{"', '"}'),
                'sub': ('[["', '"]]')}[shape]
        self.lines.append(f'    {nid}{o}{label}{c}:::{cls}')
        return nid

    def edge(self, a, b, label=None, dotted=False):
        arrow = '-.->' if dotted else '-->'
        self.lines.append(f'    {a} {arrow}|"{mlabel(label)}"| {b}' if label else f'    {a} {arrow} {b}')

    def terminal(self, text, cls):
        return self.node(mlabel(*text) if isinstance(text, (list, tuple)) else mlabel(text), 'rect', cls)

    def answered(self):
        return self.node(mlabel('Answered'), 'round', 'person')

    # -- a number in a dialplan
    def emit_rule(self, dp_name, number):
        if '$' in number:
            return self.terminal(f'Goto {number}@{dp_name} (dynamic number)', 'step')
        if self.n > self.MAX_NODES:
            self.truncated = True
            return self.terminal(f'… {number}@{dp_name} (diagram truncated)', 'step')
        if dp_name in BUILTIN_DIALPLANS:
            return self.builtin(number, f"built-in '{dp_name}'")
        hit = self.tracer.find(dp_name, number)
        if hit is None:
            if dp_name not in self.m['dialplans']:
                return self.terminal(f"Dialplan '{dp_name}' does not exist", 'bad')
            return self.builtin(number, f'no rule in [{dp_name}]')
        key = hit
        if key in self.memo:
            return self.memo[key]
        hdp, hnum = hit
        self.reached.add(hdp)
        rules = self.m['dialplans'][hdp]['numbers'][hnum]
        comments = sorted({r['comment'] for r in rules if r['comment']})
        ids = [r['rule_id'] for r in rules]
        rng = f"rule {ids[0]}" if len(ids) == 1 else f"rules {min(ids)}-{max(ids)}"
        head = self.node(mlabel(f'[{hdp}] {number_label(hnum)}', '; '.join(comments), rng), 'rect', 'step')
        self.memo[key] = head
        self.chain(head, rules, hdp, number)
        return head

    def chain(self, cur, rules, dp_name, number):
        label, pending, last_trunk = None, [], False

        def node_with_pending(main_lines, shape='rect', cls='step'):
            nonlocal pending
            n = self.node(mlabel(*(pending + list(main_lines))), shape, cls)
            pending = []
            return n

        for r in rules:
            app, p = r['app'], r['params'] + [''] * 6
            last_trunk = False
            if app in MINOR_APPS:
                t = _short(r)
                if t:
                    pending.append(t)
                continue
            if app == 'Goto':
                if pending:
                    n = node_with_pending([])
                    self.edge(cur, n, label)
                    cur, label = n, None
                self.edge(cur, self.emit_rule(p[1] or dp_name, p[0] or number), label)
                return
            if app in CONDITIONAL_JUMPS:
                if app == 'GotoIf':
                    head = [f'IF {p[0]}']
                elif app == 'Timetable':
                    head = [f"Timetable '{timetable_name(self.m, p[0])}'"]
                elif app in ('GotoIfSwitchIsOn', 'GotoIf3SwitchIsOnOrBlinking'):
                    sw = switch_info(self.m, p[0])
                    head = [f"Switch '{sw['name']}'", f"{sw['kind']}, {sw['state']} in the backup"]
                elif app == 'GotoIfCallType':
                    head = [f'Call type {p[0]}?']
                else:
                    head = [f"Last dial {p[0] or 'busy'}?"]
                d = node_with_pending(head, 'diamond')
                self.edge(cur, d, label)
                for branch, tdp, tnum in jump_targets(r, dp_name, number):
                    self.edge(d, self.emit_rule(tdp, tnum), 'yes' if branch == 'match' else branch)
                cur, label = d, ELSE_BRANCH[app]
                continue
            if app == 'Queue':
                q = self.m['queues'].get(p[0])
                if q:
                    pr = q['params']
                    mem = ', '.join(m['member'] for m in q['members']) or 'NO MEMBERS'
                    qn = node_with_pending([f"Call group {q['id']} '{q['name']}': {pr.get('strategy')}", mem,
                                            f"ring {pr.get('timeout')} s, max wait {p[4] + ' s' if p[4] else 'none'}"])
                else:
                    qn = node_with_pending([f'Call group {p[0]} (unknown)'], cls='bad')
                self.edge(cur, qn, label)
                self.edge(qn, self.answered(), 'answered')
                if not p[4]:
                    return  # no max wait: the caller stays in the queue
                cur, label = qn, f'no answer after {p[4]} s'
                continue
            if app == 'DialPhone':
                ext = p[0]
                u = next((x for x in self.m['users'] if x.get('telephoneNumber') == ext), None)
                if u and u.get('userKind') == 'park_orbit':
                    self.edge(cur, self.park_chain(u, pending), label)
                    return
                who = user_label(self.m, ext) if u else ('the dialled number' if '$' in ext else ext)
                d = node_with_pending([f'Ring {who}' + (f' for {p[1]} s' if p[1] else '')])
                self.edge(cur, d, label)
                self.edge(d, self.answered(), 'answered')
                af = active_features(u, self.cls) if u else {}
                if 'FCF' in af:
                    self.edge(d, self.terminal(f"⚠ forward all: {af['FCF']}", 'vm'), 'forwarded')
                    return
                if 'UCF' in af:
                    self.edge(d, self.terminal(f"⚠ fwd-noanswer: {af['UCF']}", 'vm'), 'no answer')
                    return
                if u is None and '$' in ext:
                    self.edge(d, self.terminal("callee's own forwards apply", 'vm'), 'no answer / busy')
                    return
                cur, label = d, 'no answer'
                continue
            if app == 'DialTrunk':
                d = node_with_pending([f'Outbound via {unquote(p[1])}', p[0]], 'round')
                self.edge(cur, d, label)
                cur, label, last_trunk = d, 'call fails', True
                continue
            if app in ('VoiceMail', 'FaxToEmail', 'Hangup'):
                txt = {'VoiceMail': f'VoiceMail {p[0]}', 'FaxToEmail': f'Fax to email {p[0]}',
                       'Hangup': 'Hang up'}[app]
                self.edge(cur, node_with_pending([txt], cls='bad' if app == 'Hangup' else 'vm'), label)
                return
            if app == 'WaitExten':
                d = node_with_pending([f'Wait {p[0]} s for a digit'], 'diamond')
                self.edge(cur, d, label)
                opts = sorted((n for n in self.m['dialplans'][dp_name]['numbers']
                               if not is_pattern(n) and n != number and len(n) <= 4), key=natural)
                for digit in opts:
                    self.edge(d, self.emit_rule(dp_name, digit), f'press {digit}' if len(digit) <= 2 else f'dials {digit}')
                cur, label = d, f'no key within {p[0]} s'
                continue
            pending.append(fmt_rule(r))
        if pending:
            n = node_with_pending([])
            self.edge(cur, n, label)
            cur, label = n, None
        if not last_trunk:
            self.edge(cur, self.terminal('End of rules: PBX hangs up', 'bad'), label)

    def park_chain(self, orbit, pending):
        pt = self.m['park'].get('timeout_ms')
        ring = self.m['system'].get('globals', {}).get('GINTDIAL_TIMEOUT')
        hold = self.node(mlabel(*(pending + [f"Park orbit {orbit.get('telephoneNumber')}: hold music",
                                              f"GPARK_TIMEOUT = {pt / 1000:g} s" if isinstance(pt, int)
                                              else f'GPARK_TIMEOUT = {pt}'])), 'rect', 'step')
        self.edge(hold, self.node(mlabel('Picked up from the park key'), 'round', 'person'), 'picked up in time')
        rb = self.node(mlabel('Ring back the PARKER only', f'up to GINTDIAL_TIMEOUT = {ring} s',
                              "parker's INTERNAL forwards apply"), 'diamond', 'step')
        self.edge(hold, rb, 'nobody picked up')
        self.edge(rb, self.answered(), 'answered')
        self.edge(rb, self.terminal(["⚠ parker's fwd-noanswer / fwd-busy", '(often their voicemail)', '(probably first)'],
                                    'vm'), 'no answer / busy, parker has a forward')
        if self.m['park'].get('return_route'):
            self.edge(rb, self.emit_rule(RECEPTIONIST_DP, 's'), 'no answer / busy, no forward')
        else:
            self.edge(rb, self.terminal([f'No [{RECEPTIONIST_DP}] default rule', 'nothing else is tried'], 'bad'),
                      'no answer / busy, no forward')
        self.edge(rb, self.terminal('Caller hangs up', 'bad'), 'caller gives up')
        return hold

    def builtin(self, number, why):
        u = next((x for x in self.m['users'] if x.get('telephoneNumber') == number), None)
        if u and u.get('userKind') == 'park_orbit':
            return self.park_chain(u, [f'{why}: built-in routing'])
        if u:
            n = self.node(mlabel(f'{why}: built-in routing', f'Ring {user_label(self.m, number)}'), 'rect', 'step')
            self.edge(n, self.answered(), 'answered')
            return n
        return self.terminal([why, 'number not found'], 'bad')

    def text(self):
        if self.truncated:
            self.lines.append(f'    %% truncated after {self.MAX_NODES} nodes')
        return '\n'.join(self.lines) + '\n'


def mermaid_diagrams(model):
    """Mermaid flowchart sources keyed inbound / park / outbound / unreached (None when there is nothing to draw).

    Also returns 'reached': the dialplans drawn from a trunk or user."""
    reached = set()
    d = {'inbound': None, 'park': None, 'outbound': None, 'unreached': None}

    # inbound
    mb = MermaidBuilder(model, 'i')
    for t in model['trunks']:
        tn = mb.node(mlabel(f"Trunk: {t['name']}", t.get('host')), 'round', 'step')
        dp_name = t.get('inbound_dialplan')
        dp = model['dialplans'].get(dp_name)
        if not dp:
            mb.edge(tn, mb.terminal(f"Inbound dialplan '{dp_name}' does not exist", 'bad'))
            continue
        if not dp['numbers'] and not dp['includes']:
            mb.edge(tn, mb.terminal([f'[{dp_name}] has no rules', 'inbound calls have no route'], 'bad'))
            continue
        nums = sorted(dp['numbers'], key=specificity)
        for num in nums:
            kind = 'pattern' if is_pattern(num) else ('DID' if re.fullmatch(r'\+?\d+', num) else 'number')
            mb.edge(tn, mb.emit_rule(dp_name, num), f'{kind} {num}')
        if dp['includes']:
            mb.edge(tn, mb.terminal(['other numbers: included dialplans', ', '.join(dp['includes'])], 'step'),
                    'other numbers')
        elif not any(n in ('X.', '!', '.', '_X.', '_.', '_!') for n in nums):
            mb.edge(tn, mb.terminal([f'No rule in [{dp_name}]', 'number not found'], 'bad'), 'any other number')
    reached |= mb.reached
    d['inbound'] = mb.text()

    # call park
    users = [u for u in model['users'] if u.get('userKind') == 'user']
    dps = [u.get('userDialplan') for u in users if u.get('userDialplan')]
    parker_dp = max(set(dps), key=dps.count) if dps else None
    orbits = model['park']['orbits']
    if orbits and parker_dp:
        pb = MermaidBuilder(model, 'p', call_class='internal')
        start = pb.node(mlabel('User on a call presses a park key', f"(parker's dialplan: {parker_dp})"),
                        'round', 'person')
        for o in orbits:
            pb.edge(start, pb.emit_rule(parker_dp, o['number']), f"transfer to {o['number']} ({o['name']})")
        reached |= pb.reached
        d['park'] = pb.text()

    # outbound
    ob = MermaidBuilder(model, 'o', call_class='internal', direction='LR')
    orbit_nums = {o['number'] for o in orbits}
    for dp_name in sorted({x for x in dps if x in model['dialplans']}):
        un = ob.node(mlabel(f'User dials ({dp_name} dialplan)'), 'round', 'person')
        for num in sorted(model['dialplans'][dp_name]['numbers'], key=specificity):
            if num in orbit_nums:
                ob.edge(un, ob.terminal('Park (see Call park)', 'step'), num)
                continue
            ob.edge(un, ob.emit_rule(dp_name, num), num)
        ob.edge(un, ob.terminal('Built-in routing: internal extensions', 'step'), 'other numbers')
    reached |= ob.reached
    d['outbound'] = ob.text()

    # not reached
    rest = [x for x, v in model['dialplans'].items() if x not in reached and v['numbers']]
    if rest:
        ub = MermaidBuilder(model, 'u')
        for x in rest:
            for num in sorted(model['dialplans'][x]['numbers'], key=specificity):
                ub.emit_rule(x, num)
        ub.lines = ub.lines[:1 + len(MERMAID_CLASSES)] + [
            l.replace(':::step', ':::unused').replace(':::person', ':::unused')
            for l in ub.lines[1 + len(MERMAID_CLASSES):]]
        d['unreached'] = ub.text()
    d['reached'] = reached
    return d


def render_mermaid(model):
    """Markdown with Mermaid flowcharts: inbound, call park, outbound, unused dialplans."""
    src = model['source']
    b = backup_identity(model)
    match = src.get('autotrace_match')
    out = [f"# {src.get('hostname') or 'PBX'} call flow", '',
           f"**PBX:** {b.get('domain') or '?'} (serial {b.get('serial') or '?'}) · **Backup:** {src.get('file')} "
           f"(taken {src.get('backup_time', '?')}) · **Autotrace match:** "
           + (match['status'].upper() if match else 'not checked'), '',
           'Generated by wildix_config_parser.py --mermaid. Blue = PBX step, green = a person answers, '
           'grey = voicemail/fax/forward, red = the call ends without a person, dashed = not reached from any '
           "trunk or user. ⚠ = depends on Wildix behaviour the backup can't show. Labels give `dialplan_rules` ids.",
           '']
    d = mermaid_diagrams(model)
    out += ['## Inbound', '', '```mermaid', d['inbound'] + '```', '']
    if d['park']:
        out += ['## Call park', '', '```mermaid', d['park'] + '```', '']
        keys = model['park']['park_keys']
        out += ['Park keys in user function keys (keys from phone templates are not in the backup):', '',
                '| User | Orbit | Label |', '|---|---|---|']
        out += [f"| {k['user']} {k['name']} | {k['orbit']} | {k['label']} |" for k in keys] or ['| (none) | | |']
        out.append('')
    out += ['## Outbound', '', '```mermaid', d['outbound'] + '```', '']
    if d['unreached']:
        out += ['## Not reached from any trunk or user', '',
                'Leftovers or flows reached some other way (feature codes, transfers to these numbers, '
                'Autotrace reporting).', '', '```mermaid', d['unreached'] + '```', '']
    return '\n'.join(out)


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('backup', help='WMS backup .tar.gz (or extracted folder)')
    ap.add_argument('--output', help='output folder (default: backup name without .tar.gz)')
    ap.add_argument('--trace', metavar='NUMBER', help='number to trace through the dialplans')
    ap.add_argument('--from', dest='origin', metavar='SPEC',
                    help='trace origin: trunk:NAME, user:EXT or dialplan:NAME')
    ap.add_argument('--focus', metavar='WHAT', help='print one slice: park, queue:ID|NAME, user:EXT')
    ap.add_argument('--autotrace', metavar='PATH',
                    help='the incident\'s autotrace archive, extracted folder or wildix-diagnostics output '
                         'folder; the backup must come from the same PBX (serial/domain) or the run stops')
    ap.add_argument('--mermaid', nargs='?', const='', metavar='FILE',
                    help='write Mermaid call-flow diagrams (markdown) to FILE (default <output>/call_flow.md)')
    ap.add_argument('--allow-mismatch', action='store_true',
                    help='continue even if the backup and the autotrace come from different PBXes')
    args = ap.parse_args()

    if args.trace and not args.origin:
        ap.error('--trace needs --from')
    model = load_model(Backup(args.backup))
    if args.autotrace:
        match = check_match(model, autotrace_identity(args.autotrace))
        model['source']['autotrace_match'] = match
        for d in match['details']:
            print(d, file=sys.stderr)
        if match['status'] == 'mismatch' and not args.allow_mismatch:
            print('ERROR: this backup is from a different PBX than the autotrace. Get a backup from the PBX in '
                  'the diagnostic report (or pass --allow-mismatch to review it anyway).', file=sys.stderr)
            sys.exit(3)
        if match['status'] == 'unknown':
            print('WARNING: could not identify the PBX in the autotrace; confirm the match by hand.',
                  file=sys.stderr)

    out = Path(args.output) if args.output else Path(re.sub(r'\.tar(\.gz)?$|\.tgz$', '', str(args.backup)))
    if out.resolve() == Path(args.backup).resolve():
        out = Path(str(out) + '-config')
    out.mkdir(parents=True, exist_ok=True)
    (out / 'config.json').write_text(json.dumps(model, indent=2, default=str), encoding='utf-8')
    summary = render_summary(model)
    (out / 'config_summary.txt').write_text(summary, encoding='utf-8')
    print(f"Wrote {out / 'config_summary.txt'} ({summary.count(chr(10))} lines) and {out / 'config.json'}",
          file=sys.stderr)
    print(f"{len(model['users'])} users, {len(model['dialplans'])} dialplans, {len(model['queues'])} call groups, "
          f"{len(model['trunks'])} trunks, {len(model['warnings'])} warnings", file=sys.stderr)

    if args.mermaid is not None:
        mpath = Path(args.mermaid) if args.mermaid else out / 'call_flow.md'
        mpath.write_text(render_mermaid(model), encoding='utf-8')
        print(f'Wrote {mpath}', file=sys.stderr)
    if args.trace:
        sys.stdout.write(trace(model, args.trace, args.origin))
    if args.focus:
        sys.stdout.write(focus(model, args.focus))


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except AttributeError:
        pass
    main()
