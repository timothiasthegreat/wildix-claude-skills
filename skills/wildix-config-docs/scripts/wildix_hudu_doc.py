#!/usr/bin/env python3
"""
Wildix PBX configuration documentation for Hudu, from a WMS backup.

Reads a standard WMS system backup with wildix_config_parser.py (credentials redacted) and writes:
  <output>/hudu_article.html   a Hudu KB article body (TinyMCE-clean HTML): accordion sections, Mermaid
                               call-flow diagrams (<pre class="mermaid">) and text layouts of every dialplan
  <output>/hudu_meta.json      title, PBX identity, counts and what was carried over from --previous

The same backup and options always give the same HTML. With --previous (the existing article's HTML, saved
from Hudu), the Technician notes and Change log sections are copied over verbatim and a change-log row is
added; everything else is regenerated. Standard library only, Python 3.9+.

Example:
  python wildix_hudu_doc.py backup_wms_b_x.tar.gz --company "Acme Ltd" --output out \\
      --previous out/previous_article.html --summary-file out/summary.txt --changelog "Added user 214"
"""

import argparse
import datetime
import html
import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote

HERE = Path(__file__).resolve().parent
# Packaged skill: the parser is copied next to this script. Repo checkout: use wildix-config-review's copy.
for cand in (HERE, HERE.parent.parent / 'wildix-config-review' / 'scripts'):
    if (cand / 'wildix_config_parser.py').is_file():
        sys.path.insert(0, str(cand))
        break
import wildix_config_parser as P  # noqa: E402

TITLE_FMT = 'Configuration - Wildix - PBX Configuration ({hostname})'
GENERATOR = 'wildix_hudu_doc.py'

# Section ids are the contract with existing articles: --previous finds preserved sections by id
# (falling back to the summary text), and the SKILL.md / reference doc name them. Don't rename.
SECTIONS = [
    ('wx-overview', 'Overview'),
    ('wx-inbound', 'Inbound call flow'),
    ('wx-trunks', 'Trunks and numbers'),
    ('wx-dialplans', 'Dialplans'),
    ('wx-queues', 'Call groups'),
    ('wx-users', 'Users and extensions'),
    ('wx-park', 'Call park'),
    ('wx-outbound', 'Outbound dialling'),
    ('wx-timetables', 'Timetables and switches'),
    ('wx-warnings', 'Configuration warnings'),
    ('wx-notes', 'Technician notes'),
    ('wx-changelog', 'Change log'),
]
PRESERVED = ('wx-notes', 'wx-changelog')
SUMMARY_PLACEHOLDER = 'Summary not written yet.'
# Important notes: a plain block (not an accordion) above everything else, written by people in Hudu and
# carried over verbatim (heading included) on every regenerate.
IMPORTANT_ID = 'wx-important'
IMPORTANT_TITLE = 'Important notes'
IMPORTANT_PLACEHOLDER = ("None recorded. Add anything a technician must know before touching this PBX "
                         "(e.g. don't reboot during business hours, carrier port pending, changes need the "
                         "client's approval). This block is never changed when the documentation is regenerated.")

LEGEND = ('Blue = PBX step, green = a person answers, grey = voicemail/fax/forward, red = the call ends '
          'without a person, dashed = not reached from any trunk or user. ⚠ = depends on Wildix behaviour '
          "the backup can't show. Rule numbers are WMS dialplan rule ids.")


def esc(s):
    return html.escape(unquote(str(s)) if s is not None else '', quote=False)


def secs(v):
    return f'{v} s' if v not in (None, '') else '-'


def table(headers, rows, empty='None configured.'):
    if not rows:
        return f'<p><em>{esc(empty)}</em></p>'
    h = ''.join(f'<th>{esc(x)}</th>' for x in headers)
    body = ''.join('<tr>' + ''.join(f'<td>{c}</td>' for c in r) + '</tr>' for r in rows)
    return f'<table><thead><tr>{h}</tr></thead><tbody>{body}</tbody></table>'


def section(sid, title, body, open_=False):
    attr = ' open="open"' if open_ else ''
    return f'<details class="mce-accordion" id="{sid}"{attr}><summary>{esc(title)}</summary>\n{body}\n</details>'


def mermaid(src):
    # Hudu's editor's own Mermaid code block (/code, language Mermaid). It survives editor saves, where a plain
    # <pre class="mermaid"> is turned into an ordinary code block and stops rendering.
    return f'<pre class="language-mermaid"><code class="language-mermaid">{html.escape(src, quote=False)}</code></pre>'


def pre(text):
    return f'<pre>{html.escape(text, quote=False)}</pre>'


# ---------------------------------------------------------------- plain-language routing

def describe_path(model, dp_name, number, depth=0, seen=None):
    """Short plain-language path of a number through the dialplans, e.g. 'Ring 201 (Front Desk) 20 s →
    call group 'Reception' → voicemail 201'."""
    seen = seen if seen is not None else set()
    tracer = P.Tracer(model, 'external')
    hit = tracer.find(dp_name, number) if dp_name not in P.BUILTIN_DIALPLANS else None
    if hit is None:
        u = next((x for x in model['users'] if x.get('telephoneNumber') == number), None)
        return f'Ring {P.user_label(model, number)}' if u else f'No rule for {number} in [{dp_name}]'
    if hit in seen or depth > 6:
        return f'back to {hit[1]} in [{hit[0]}] (loop)'
    seen.add(hit)
    steps = []
    for r in model['dialplans'][hit[0]]['numbers'][hit[1]]:
        app, p = r['app'], r['params'] + [''] * 6
        if app == 'DialPhone':
            ext = p[0].replace('${EXTEN}', number)
            steps.append(f"Ring {P.user_label(model, ext)}" + (f' {p[1]} s' if p[1] else ''))
            u = next((x for x in model['users'] if x.get('telephoneNumber') == ext), None)
            af = P.active_features(u, 'external') if u else {}
            if 'FCF' in af or 'UCF' in af:
                # same rule as the diagrams: a forward takes the call before the later rules run
                steps[-1] += (f" (forward all to {af['FCF']})" if 'FCF' in af
                              else f" (if unanswered, forwards to {af['UCF']}; later steps probably don't run)")
                break
        elif app == 'Queue':
            q = model['queues'].get(p[0])
            steps.append((f"Call group '{q['name']}'" if q else f'Call group {p[0]} (unknown)')
                         + (f' (max wait {p[4]} s)' if p[4] else ''))
            if not p[4]:
                break
        elif app == 'Goto':
            steps.append(describe_path(model, p[1] or hit[0], p[0] or number, depth + 1, seen))
            break
        elif app in P.CONDITIONAL_JUMPS:
            targets = P.jump_targets(r, hit[0], number)
            if app == 'Timetable':
                conds = [f"timetable '{P.timetable_name(model, p[0])}' is open"]
            elif app == 'GotoIf':
                conds = [p[0]]
            elif app in ('GotoIfSwitchIsOn', 'GotoIf3SwitchIsOnOrBlinking'):
                name = P.switch_info(model, p[0])['name']
                conds = [f"switch '{name}' is {branch}" for branch, _, _ in targets]
            elif app == 'GotoIfCallType':
                conds = [f'the call type is {p[0]}']
            else:
                conds = [f"the last dial ended {p[0] or 'busy'}"]
            steps.append('; '.join(f'if {c}: {describe_path(model, tdp, tnum, depth + 1, seen)}'
                                   for c, (_, tdp, tnum) in zip(conds, targets)) + '; otherwise')
        elif app == 'DialTrunk':
            steps.append(f'Out via {unquote(p[1])}')
        elif app == 'VoiceMail':
            steps.append(f'Voicemail {p[0]}')
            break
        elif app == 'FaxToEmail':
            steps.append(f'Fax to email {p[0]}')
            break
        elif app == 'Hangup':
            steps.append('Hang up')
            break
        elif app == 'WaitExten':
            steps.append(f'IVR menu (waits {p[0]} s for a digit)')
        elif app in ('Playback', 'Background'):
            steps.append(f'Play {p[0]}')
    return ' → '.join(steps).replace('; otherwise →', '; otherwise') or 'No routing steps'


# ---------------------------------------------------------------- text layouts

def dialplan_layout(model, dp_name, dp, used_by):
    L = [f'[{dp_name}]' + (f'  {dp["description"]}' if dp['description'] else '')]
    if used_by:
        L.append('  used by ' + '; '.join(used_by))
    if dp['includes']:
        L.append('  includes: ' + ', '.join(dp['includes']))
    if not dp['numbers']:
        L.append('  (no rules)')
    for num in sorted(dp['numbers'], key=P.specificity):
        rules = dp['numbers'][num]
        comment = sorted({r['comment'] for r in rules if r['comment']})
        L.append('')
        L.append(f'  {P.number_label(num)}' + (f'   # {"; ".join(comment)}' if comment else ''))
        noops = []

        def flush():
            # runs of NoOp-only CustomApps are debug logging: one line instead of one per rule
            if len(noops) == 1:
                L.append(f"    {noops[0]['priority']:>2}. {unquote(P.fmt_rule(noops[0]))}   [rule {noops[0]['rule_id']}]")
            elif noops:
                L.append(f"    {noops[0]['priority']:>2}-{noops[-1]['priority']}. NoOp x{len(noops)} (debug logging only)"
                         f"   [rules {noops[0]['rule_id']}-{noops[-1]['rule_id']}]")
            noops.clear()

        for r in rules:
            if r['app'] == 'CustomApp' and (r['params'] or [''])[0].startswith('NoOp('):
                noops.append(r)
                continue
            flush()
            note = ''
            p = r['params'] + [''] * 6
            if r['app'] == 'Queue':
                q = model['queues'].get(p[0])
                note = f"  -> call group '{q['name']}'" if q else '  -> unknown call group'
            elif r['app'] == 'DialPhone' and p[0] and '$' not in p[0]:
                note = f'  -> {P.user_label(model, p[0])}'
            elif r['app'] == 'Timetable':
                tt = next((t for t in model['timetables'] if str(t['id']) == p[0]), None)
                if tt:
                    note = f"  -> timetable '{tt['timetable']}'"
            elif r['app'] in ('GotoIfSwitchIsOn', 'GotoIf3SwitchIsOnOrBlinking'):
                sw = P.switch_info(model, p[0])
                note = f"  -> switch '{sw['name']}' ({sw['kind']}, {sw['state']} in the backup)"
            L.append(f"    {r['priority']:>2}. {unquote(P.fmt_rule(r))}{note}   [rule {r['rule_id']}]")
        flush()
    return '\n'.join(L)


def forwards(u, cls):
    f = u.get('features') or {}
    if 'decode_error' in f:
        return '(not decoded)'
    af = P.active_features(u, cls)
    return ', '.join(f"{P.SHORT_FEATURE.get(k, k)}={v}" if v is not True else P.SHORT_FEATURE.get(k, k)
                     for k, v in af.items()) or '-'


# ---------------------------------------------------------------- sections

def overview(model, company, summary, generated):
    src, b = model['source'], P.backup_identity(model)
    users = [u for u in model['users'] if u.get('userKind') == 'user']
    info = [
        ('Company', company), ('PBX hostname', src.get('hostname')), ('Domain', b.get('domain')),
        ('Serial', b.get('serial')), ('Version', src.get('version')),
        ('Timezone', model['system'].get('timezone')),
        ('Backup taken', f"{src.get('backup_time', '?')} (PBX local time)"), ('Backup file', src.get('file')),
        ('Documented', generated),
        ('Contents', f"{len(users)} users, {len(model['users']) - len(users)} other extensions, "
                     f"{len(model['dialplans'])} dialplans, {len(model['queues'])} call groups, "
                     f"{len(model['trunks'])} trunks"),
    ]
    rows = [[f'<strong>{esc(k)}</strong>', esc(v if v not in (None, '') else '?')] for k, v in info]
    did_rows = []
    for t in model['trunks']:
        dp = model['dialplans'].get(t.get('inbound_dialplan'))
        if not dp:
            continue
        for num in sorted(dp['numbers'], key=P.specificity):
            did_rows.append([esc(num), esc(t['name']), esc(describe_path(model, t['inbound_dialplan'], num))])
    summ = ''.join(f'<p>{esc(x)}</p>' for x in summary.split('\n\n') if x.strip()) if summary else \
        f'<p><em>{SUMMARY_PLACEHOLDER}</em></p>'
    return (f'<div id="wx-summary">{summ}</div>\n'
            + table(['Field', 'Value'], rows)
            + '\n<h4>Incoming numbers</h4>\n'
            + table(['Number / pattern', 'Trunk', 'What happens'], did_rows, 'No inbound routing found.')
            + f'\n<p><em>Generated by {GENERATOR} from the WMS backup. Edit only Important notes (top of the '
              'page) and Technician notes by hand: the rest is replaced each time the documentation is '
              'regenerated.</em></p>')


def inbound(model, d):
    return f'<p><em>{esc(LEGEND)}</em></p>\n' + mermaid(d['inbound'])


def trunks(model):
    rows = []
    for t in model['trunks']:
        dp = model['dialplans'].get(t.get('inbound_dialplan')) or {'numbers': {}}
        nums = ', '.join(sorted(dp['numbers'], key=P.specificity)) or '-'
        rows.append([esc(t['name']), esc(t['type']), esc(t.get('host') or '-'),
                     esc(t.get('inbound_dialplan') or '-'), esc(nums),
                     esc('yes' if str(t.get('registers')) == '1' else 'no' if t.get('registers') is not None else '-'),
                     esc(t.get('codecs') or '-'), esc(t.get('dtmf') or '-'), esc(t.get('transport') or '-')])
    body = table(['Trunk', 'Type', 'Host', 'Inbound dialplan', 'Numbers routed', 'Registers', 'Codecs', 'DTMF',
                  'Transport'], rows, 'No trunks configured.')
    groups = [[esc(o['groupname']), esc(', '.join(r['trunkname'] for r in o['rules']))]
              for o in model['outgoing_groups']]
    if groups:
        body += '\n<h4>Trunk groups</h4>\n' + table(['Group', 'Trunks (in order)'], groups)
    return body + '\n<p><em>Trunk credentials are not documented here. Keep them in the client\'s Hudu passwords.</em></p>'


def dialplans(model, d):
    users_by_dp, trunks_by_dp = {}, {}
    for u in model['users']:
        users_by_dp.setdefault(u.get('userDialplan'), []).append(u.get('telephoneNumber'))
    for t in model['trunks']:
        trunks_by_dp.setdefault(t.get('inbound_dialplan'), []).append(t['name'])
    parts = []
    for dp_name, dp in model['dialplans'].items():
        used_by = []
        if trunks_by_dp.get(dp_name):
            used_by.append('trunks: ' + ', '.join(trunks_by_dp[dp_name]))
        if users_by_dp.get(dp_name):
            used_by.append('users: ' + ', '.join(sorted(users_by_dp[dp_name], key=P.natural)))
        note = '' if dp_name in d['reached'] or not dp['numbers'] else \
            '<p><em>Not reached from any trunk or user dialplan (unused, or reached by a feature code or transfer).</em></p>'
        parts.append(f'<h4>{esc(dp_name)}</h4>\n{note}{pre(dialplan_layout(model, dp_name, dp, used_by))}')
    body = '\n'.join(parts) or '<p><em>No dialplans.</em></p>'
    if d['unreached']:
        body += '\n<h4>Diagram: dialplans not reached from any trunk or user</h4>\n' + mermaid(d['unreached'])
    return body


def queues(model):
    rows = []
    for q in model['queues'].values():
        p = q['params']
        mem = ', '.join(P.user_label(model, m['member']) for m in q['members']) or '<strong>no members</strong>'
        used = []
        for dp_name, dp in model['dialplans'].items():
            for num, rules in dp['numbers'].items():
                for r in rules:
                    if r['app'] == 'Queue' and (r['params'] or [''])[0] == str(q['id']):
                        mw = (r['params'] + [''] * 6)[4]
                        used.append(f'{num}@{dp_name}' + (f' (max wait {mw} s)' if mw else ''))
        rows.append([esc(q['id']), esc(q['name']), esc(p.get('strategy', '-')),
                     esc(secs(p.get('timeout'))), esc(secs(p.get('retry'))),
                     esc(secs(p.get('wrapuptime'))), esc(p.get('maxlen', '-')),
                     mem if not q['members'] else esc(mem), esc('; '.join(used) or '-')])
    return table(['ID', 'Name', 'Strategy', 'Ring', 'Retry', 'Wrap-up', 'Max callers', 'Members', 'Used by'],
                 rows, 'No call groups configured.')


def users_section(model):
    rows, other = [], []
    for u in model['users']:
        f = u.get('features') or {}
        r = [esc(u.get('telephoneNumber', '-')), esc(u.get('cn')), esc(u.get('userDialplan') or '-'),
             esc(u.get('group_name') or '-')]
        if u.get('userKind') == 'user':
            rows.append(r + [esc(f.get('status', '-')), esc(forwards(u, 'external')), esc(forwards(u, 'internal'))])
        else:
            other.append(r[:2] + [esc(u.get('userKind'))] + r[2:3])
    body = table(['Ext', 'Name', 'Dialplan', 'Group', 'Status', 'Forwards (external calls)',
                  'Forwards (internal calls)'], rows, 'No users.')
    if other:
        body += '\n<h4>Other extensions</h4>\n' + table(['Ext', 'Name', 'Kind', 'Dialplan'], other)
    return body + ('\n<p><em>Forwards are the ones in force at the user\'s status when the backup was taken '
                   '(fwd-all, fwd-busy, fwd-noanswer, noanswer-timeout in seconds). Users change these themselves '
                   'in Collaboration, so treat them as a snapshot.</em></p>')


def park(model, d):
    p = model['park']
    t = p.get('timeout_ms')
    ring = model['system'].get('globals', {}).get('GINTDIAL_TIMEOUT')
    facts = [['<strong>Park timeout</strong>', esc(f'{int(t) / 1000:g} s' if isinstance(t, int) else (t or '?'))],
             ['<strong>Ring-back to the parker</strong>', esc(f'up to {ring} s' if ring else '?')],
             ['<strong>If the parker is busy or doesn\'t answer</strong>',
              esc(f"[{P.RECEPTIONIST_DP}] default: " + '; '.join(P.fmt_rule(r) for r in p['return_route']['rules'])
                  + " (unless the parker's own forward takes the call)" if p.get('return_route')
                  else f"no [{P.RECEPTIONIST_DP}] dialplan: the parker's forwards, if any")],
             ['<strong>Return from transfer</strong>', esc(p.get('return_from_transfer') or '?')]]
    body = table(['Setting', 'Value'], facts)
    body += '\n<h4>Park orbits</h4>\n' + table(['Orbit', 'Name', 'Dialplan'],
                                               [[esc(o['number']), esc(o['name']), esc(o['dialplan'] or '-')]
                                                for o in p['orbits']], 'No park orbits.')
    body += '\n<h4>Park keys on phones</h4>\n' + table(
        ['User', 'Orbit', 'Label'],
        [[esc(f"{k['user']} {k['name']}"), esc(k['orbit']), esc(k['label'])] for k in p['park_keys']],
        'No park keys in user function keys (keys set by phone templates are not in the backup).')
    if d['park']:
        body += '\n<h4>What happens to a parked call</h4>\n' + mermaid(d['park'])
    return body


def outbound(model, d):
    return f'<p><em>{esc(LEGEND)}</em></p>\n' + mermaid(d['outbound'])


def timetables(model):
    rows = []
    for t in model['timetables']:
        periods = '; '.join(f"days {x['day_of_week_from']}-{x['day_of_week_to']} {x['start_daytime']}-{x['end_daytime']}"
                            + (f" (month {x['month_id']})" if x.get('month_id') not in (None, '', 0, '0') else '')
                            for x in t['periods']) or '-'
        st = P.TIMETABLE_STATES.get(t.get('state'), f"{t.get('state')} (not decoded)")
        rows.append([esc(t['id']), esc(t['timetable']), esc(st), esc(periods)])
    body = table(['ID', 'Timetable', 'State', 'Periods'], rows, 'No timetables configured.')
    sw = []
    for s in model['switches']:
        i = P.switch_info(model, s['id'])
        sw.append([esc(i['id']), esc(i['name']), esc(i['kind']), esc(i['state'])])
    if sw:
        body += ('\n<h4>Switches</h4>\n<p>Switches are gates in the dialplan rules ("Jump to if switch is on"), not tied '
                 'to a timetable. The state is the position when the backup was taken.</p>\n'
                 + table(['ID', 'Switch', 'Type', 'State in backup'], sw))
    return body


def warnings(model):
    if not model['warnings']:
        return '<p>No configuration warnings.</p>'
    return ('<p>Found by the config parser when this was generated. Review these with the client or fix them '
            'in WMS.</p>\n<ul>' + ''.join(f'<li>{esc(w)}</li>' for w in model['warnings']) + '</ul>')


# ---------------------------------------------------------------- preserved sections

def find_block(doc, tag, sid, title, title_re):
    """Return (start, end) of the <tag> element with this id, or with no id whose first child (matched by
    title_re) has this text, balancing nested elements of the same tag."""
    tag_re = re.compile(rf'<(/?){tag}\b[^>]*>', re.I)
    for m in tag_re.finditer(doc):
        if m.group(1):
            continue
        idm = re.search(r'\bid\s*=\s*["\']([^"\']+)', m.group(0))
        sm = re.match(title_re, doc[m.end():m.end() + 300], re.S | re.I)
        stext = re.sub(r'<[^>]+>', '', sm.group(1)).strip().lower() if sm else ''
        if (idm and idm.group(1) == sid) or (not idm and stext == title.lower()):
            depth = 0
            for t in tag_re.finditer(doc, m.start()):
                depth += -1 if t.group(1) else 1
                if depth == 0:
                    return m.start(), t.end()
    return None


def find_section(doc, sid, title):
    """Return (start, end) of the accordion section with this id (or summary text)."""
    return find_block(doc, 'details', sid, title, r'\s*<summary[^>]*>(.*?)</summary>')


def find_important(doc):
    """The Important notes block: <div id="wx-important">, or a heading-led div if an editor dropped the id."""
    return find_block(doc, 'div', IMPORTANT_ID, IMPORTANT_TITLE, r'\s*<h\d[^>]*>(.*?)</h\d>')


def section_inner(doc, span):
    """Body of a <details> section (between </summary> and the closing </details>)."""
    s = doc[span[0]:span[1]]
    m = re.search(r'</summary>', s, re.I)
    return s[m.end() if m else s.index('>') + 1:s.rfind('</details>')].strip()


def previous_field(doc, field):
    """A value from the previous Overview facts table. Hudu's editor adds attributes and wraps cell text in
    <p>, so match the label cell loosely and take the next cell."""
    m = re.search(rf'<strong>\s*{re.escape(field)}\s*</strong>(?:\s*</p>)?\s*</t[dh]>\s*<td\b[^>]*>(.*?)</td>',
                  doc, re.S | re.I)
    return html.unescape(re.sub(r'<[^>]+>', '', m.group(1))).strip() if m else None


def changelog_rows(inner):
    """Data rows of the previous change log. The editor moves the header row into <tbody>: skip <th> rows."""
    rows = re.findall(r'<tr\b[^>]*>.*?</tr>', inner, re.S | re.I)
    return [r for r in rows if not re.search(r'<th\b', r, re.I)]


# ---------------------------------------------------------------- article

def build_article(model, company, generated, summary=None, previous=None, change=None):
    d = P.mermaid_diagrams(model)
    meta = {'title': TITLE_FMT.format(hostname=model['source'].get('hostname') or 'PBX'),
            'company': company, **P.backup_identity(model),
            'backup_time': model['source'].get('backup_time'), 'version': model['source'].get('version'),
            'counts': {'users': len(model['users']), 'dialplans': len(model['dialplans']),
                       'call_groups': len(model['queues']), 'trunks': len(model['trunks']),
                       'warnings': len(model['warnings'])},
            'summary_written': bool(summary), 'previous': None}

    notes = ('<p><em>Add anything the backup can\'t show: who to call at the client, carrier account numbers, '
             'why something is set up the way it is, pending changes. This section is kept when the '
             'documentation is regenerated.</em></p>')
    important = (f'<div id="{IMPORTANT_ID}"><h3>{IMPORTANT_TITLE}</h3>\n'
                 f'<p><em>{esc(IMPORTANT_PLACEHOLDER)}</em></p>\n</div>')
    old_rows = []
    if previous is not None:
        prev = {'important_kept': False, 'notes_kept': False, 'changelog_rows_kept': 0,
                'serial': previous_field(previous, 'Serial'),
                'hostname': previous_field(previous, 'PBX hostname'),
                'backup_time': (previous_field(previous, 'Backup taken') or '').replace(' (PBX local time)', '') or None}
        span = find_important(previous)
        if span:
            important = previous[span[0]:span[1]]
            if IMPORTANT_ID not in important[:important.index('>')]:
                important = f'<div id="{IMPORTANT_ID}"' + important[len('<div'):]  # restore the id
            prev['important_kept'] = True
        span = find_section(previous, 'wx-notes', 'Technician notes')
        if span:
            notes = section_inner(previous, span)
            prev['notes_kept'] = True
        span = find_section(previous, 'wx-changelog', 'Change log')
        if span:
            old_rows = changelog_rows(section_inner(previous, span))
            prev['changelog_rows_kept'] = len(old_rows)
        # None = couldn't read the serial from the previous article: unknown, not a match
        prev['serial_matches'] = None if not prev['serial'] else prev['serial'].lower() == (meta['serial'] or '')
        prev['older_backup'] = bool(prev['backup_time'] and meta['backup_time']
                                    and meta['backup_time'] < prev['backup_time'])
        meta['previous'] = prev

    if change is None:
        change = 'Regenerated from a new backup.' if previous is not None else 'Initial documentation.'
    new_row = (f'<tr><td>{esc(generated)}</td><td>{esc(model["source"].get("backup_time", "?"))}</td>'
               f'<td>{esc(change)}</td></tr>')
    changelog = ('<table><thead><tr><th>Documented</th><th>Backup taken</th><th>Changes</th></tr></thead>'
                 f'<tbody>{new_row}{"".join(old_rows)}</tbody></table>')

    bodies = {
        'wx-overview': overview(model, company, summary, generated),
        'wx-inbound': inbound(model, d),
        'wx-trunks': trunks(model),
        'wx-dialplans': dialplans(model, d),
        'wx-queues': queues(model),
        'wx-users': users_section(model),
        'wx-park': park(model, d),
        'wx-outbound': outbound(model, d),
        'wx-timetables': timetables(model),
        'wx-warnings': warnings(model),
        'wx-notes': notes,
        'wx-changelog': changelog,
    }
    out = [important, '<hr>']
    out += [f'<p>Configuration of the Wildix PBX <strong>{esc(model["source"].get("hostname") or "?")}</strong> '
           f'({esc(meta["domain"] or "?")}) for {esc(company)}, from the WMS backup taken '
           f'{esc(model["source"].get("backup_time", "?"))}. Expand a section to read it.</p>']
    out += [section(sid, title, bodies[sid], open_=(sid == 'wx-overview')) for sid, title in SECTIONS]
    return '\n'.join(out) + '\n', meta


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('backup', help='WMS backup .tar.gz (or extracted folder)')
    ap.add_argument('--company', required=True, help='client company name as it appears in Hudu')
    ap.add_argument('--output', help='output folder (default: backup name without .tar.gz, plus -hudu)')
    ap.add_argument('--previous', metavar='HTML', help="the existing Hudu article's HTML, to keep its notes and "
                                                        'change log')
    ap.add_argument('--summary-file', metavar='TXT', help='plain-language overview summary (paragraphs separated '
                                                          'by a blank line)')
    ap.add_argument('--changelog', metavar='TEXT', help='what changed, for the new change-log row')
    ap.add_argument('--date', help='documented date (default: today, YYYY-MM-DD)')
    args = ap.parse_args()

    model = P.load_model(P.Backup(args.backup))
    previous = Path(args.previous).read_text(encoding='utf-8') if args.previous else None
    summary = Path(args.summary_file).read_text(encoding='utf-8').strip() if args.summary_file else None
    generated = args.date or datetime.date.today().isoformat()
    doc, meta = build_article(model, args.company, generated, summary, previous, args.changelog)

    out = Path(args.output) if args.output else Path(re.sub(r'\.tar(\.gz)?$|\.tgz$', '', str(args.backup)) + '-hudu')
    out.mkdir(parents=True, exist_ok=True)
    (out / 'hudu_article.html').write_text(doc, encoding='utf-8')
    (out / 'hudu_meta.json').write_text(json.dumps(meta, indent=2), encoding='utf-8')
    print(f"Wrote {out / 'hudu_article.html'} ({len(doc)} chars) and {out / 'hudu_meta.json'}", file=sys.stderr)
    print(f"Title: {meta['title']}", file=sys.stderr)
    prev = meta['previous']
    if prev:
        print(f"Previous article: important notes kept={prev['important_kept']}, notes kept={prev['notes_kept']}, "
              f"change-log rows kept={prev['changelog_rows_kept']}", file=sys.stderr)
        if prev['serial_matches'] is None:
            print('WARNING: could not read the serial from the previous article: confirm it documents this PBX '
                  f"({meta['serial']}) before replacing it.", file=sys.stderr)
        elif not prev['serial_matches']:
            print(f"WARNING: the previous article documents serial {prev['serial']}, this backup is "
                  f"{meta['serial']}: a different PBX.", file=sys.stderr)
        if prev['older_backup']:
            print(f"WARNING: this backup ({meta['backup_time']}) is older than the one the article was built from "
                  f"({prev['backup_time']}).", file=sys.stderr)


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except AttributeError:
        pass
    main()
