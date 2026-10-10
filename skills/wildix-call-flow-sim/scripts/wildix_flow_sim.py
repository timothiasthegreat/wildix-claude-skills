#!/usr/bin/env python3
"""
Interactive Wildix call-flow simulator pages, from a WMS backup and/or a build plan.

Writes one self-contained HTML page (assets/template.html + a JSON flow spec) with:
  - a call simulator: controls (day/time, switches, who is free, what the caller presses) drive a highlighted
    path through an SVG diagram, an outcome banner and a step-by-step trace
  - from a plan: Build plan, extra info tabs, a UAT checklist and Open items (ticks saved in the browser)

Inputs:
  --backup  a WMS system backup (*_wms_b_*.tar.gz), read with wildix_config_parser.py (credentials redacted).
            Alone: one flow per inbound number, built from the real dialplans.
            With --plan: the plan's objects are checked against it (real names, "To create" for missing ones).
  --plan    plan JSON written from a build plan (schema: references/flow-spec.md)

Outputs in --output: <slug>-call-flow.html and flow_spec.json (the resolved spec, for review).
Standard library only, Python 3.9+.

Example:
  python wildix_flow_sim.py --plan plan.json --backup acme_..._wms_b_x.tar.gz --output acme-callflow
"""

import argparse
import copy
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

TEMPLATE = HERE.parent / 'assets' / 'template.html'
GENERATOR = 'wildix_flow_sim.py'

DAY_KEYS = {'sun': 0, 'mon': 1, 'tue': 2, 'wed': 3, 'thu': 4, 'fri': 5, 'sat': 6}
DAY_NAMES = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']

# Node types and their outgoing branches (besides 'next'). Terminals have none.
NODE_TYPES = {'start', 'route', 'play', 'switch', 'timetable', 'condition', 'decide', 'ring', 'queue', 'ivr',
              'dial_external', 'park', 'voicemail', 'fax', 'forward', 'answered', 'hangup', 'end'}
SINGLE_NEXT = {'start', 'route', 'play'}
TERMINALS = {'park', 'voicemail', 'fax', 'forward', 'answered', 'hangup', 'end'}
DEFAULT_LABELS = {'open': 'Open', 'closed': 'Closed', 'true': 'Yes', 'false': 'No', 'no_answer': 'No answer',
                  'skipped': 'Nobody free', 'answered': 'Answered', 'full': 'Queue full', 'timeout': 'Timeout'}
# The branch that continues down the main column when subtrees tie in size.
PRIMARY = {'switch': None, 'timetable': 'open', 'condition': 'false', 'ring': 'no_answer', 'queue': 'timeout',
           'ivr': 'timeout', 'dial_external': 'no_answer'}
STATUS_CHIP = {'existing': ('d', 'In PBX'), 'to_create': ('c', 'To create'), 'not_found': ('x', 'Not found in PBX'),
               'ambiguous': ('o', 'Ambiguous'), 'planned': ('o', 'Planned')}


class SpecError(Exception):
    pass


# ---------------------------------------------------------------- helpers

def slugify(s):
    return re.sub(r'[^a-z0-9]+', '-', str(s).lower()).strip('-') or 'pbx'


def norm(s):
    return re.sub(r'[\s_\-]+', ' ', str(s or '')).strip().casefold()


def mask_number(s, full=False):
    """Hide all but the last 4 digits of external numbers (urgent contacts' cells and the like)."""
    s = unquote(str(s or ''))
    if full or '$' in s:
        return s
    digits = re.sub(r'\D', '', s)
    if len(digits) < 7:
        return s
    return '…' + digits[-4:]


def parse_time(t):
    t = str(t).strip()
    m = re.fullmatch(r'(\d{1,2}):(\d{2})(?::\d{2})?', t)
    if not m:
        raise SpecError(f'bad time {t!r} (use HH:MM)')
    return int(m.group(1)) * 60 + int(m.group(2))


def parse_ranges(v):
    """'08:00-12:00, 13:00-17:00' or ['08:00-12:00', ...] -> [[480, 720], [780, 1020]]."""
    items = v if isinstance(v, list) else str(v).split(',')
    out = []
    for it in items:
        it = str(it).strip()
        if not it or it.lower() == 'closed':
            continue
        a, _, b = it.partition('-')
        start, end = parse_time(a), parse_time(b)
        if end == 0 or end <= start:
            end = 1440 if end == 0 else end
        out.append([start, end])
    return out


def parse_hours(h):
    """{'mon': '08:00-19:00', 'tue-thu': '08:00-16:00'} -> {'1': [[480, 1140]], '2': ...} (0 = Sunday)."""
    out = {}
    for k, v in (h or {}).items():
        a, _, b = str(k).lower().partition('-')
        if a not in DAY_KEYS or (b and b not in DAY_KEYS):
            raise SpecError(f'bad day {k!r} in hours (use mon..sun or a range like tue-thu)')
        d0, d1 = DAY_KEYS[a], DAY_KEYS[b or a]
        days = [d0]
        while days[-1] != d1:
            days.append((days[-1] + 1) % 7)
        for d in days:
            out.setdefault(str(d), []).extend(parse_ranges(v))
    return out


def hours_text(hours):
    if not hours:
        return 'no weekly hours'
    parts = []
    for d in (1, 2, 3, 4, 5, 6, 0):
        r = hours.get(str(d))
        if r:
            parts.append(f"{DAY_NAMES[d]} " + ', '.join(f'{a // 60:02d}:{a % 60:02d}-{b // 60:02d}:{b % 60:02d}'
                                                        for a, b in r))
    return '; '.join(parts)


def find_moment(hours, inside=True):
    """A (dow, 'HH:MM') inside (or outside) the weekly hours, weekdays first."""
    if inside:
        for d in (2, 3, 1, 4, 5, 6, 0):
            r = hours.get(str(d))
            if r:
                a, b = r[0]
                m = min(a + 120, (a + b) // 2)
                return d, f'{m // 60:02d}:{m % 60:02d}'
        return 2, '10:00'
    for d, t in ((6, '19:00'), (0, '23:00'), (2, '23:30'), (2, '03:00')):
        m = parse_time(t)
        if not any(a <= m < b for a, b in hours.get(str(d), [])):
            return d, t
    return 6, '19:00'


# ---------------------------------------------------------------- backup -> spec

def backup_timetables(model):
    """Timetable definitions from the backup, keyed by name, in the spec's format."""
    out = {}
    for t in model['timetables']:
        hours, dates = {}, []
        for p in t.get('periods', []):
            if str(p.get('enabled_id', 1)) == '0':
                continue
            dated = any(int(p.get(k) or 0) for k in ('day_id_from', 'day_id_to', 'month_id', 'year_id'))
            a = parse_time(str(p.get('start_daytime') or '00:00')[:5])
            b = parse_time(str(p.get('end_daytime') or '00:00')[:5])
            if b == 0 or str(p.get('end_daytime', '')).startswith('23:59'):
                b = 1440
            if dated:
                dates.append(f"day {p.get('day_id_from')}-{p.get('day_id_to')} month {p.get('month_id')} "
                             f"year {p.get('year_id')} {str(p.get('start_daytime'))[:5]}-{str(p.get('end_daytime'))[:5]}")
                continue
            f, to = int(p.get('day_of_week_from') or 0), int(p.get('day_of_week_to') or 0)
            days = list(range(7)) if f == 0 and to == 0 else []
            if not days:
                d = f % 7
                days = [d]
                while days[-1] != to % 7 and len(days) < 7:
                    days.append((days[-1] + 1) % 7)
            for d in days:
                hours.setdefault(str(d), []).append([a, b if b > a else 1440])
        tt = {'kind': 'weekly' if hours else 'dates', 'hours': hours, 'source': 'backup',
              'summary': hours_text(hours) + (f'; {len(dates)} date entries' if dates else '')}
        if hours and dates:
            tt['dates'] = dates
        elif dates:
            tt['dates'] = dates
        out[t['timetable']] = tt
    return out


def backup_switches(model):
    """Switches are gates in the dialplan rules (Jump to if switch is on / 3-state switch is on or blinking).
    Each starts in the position it had when the backup was taken."""
    out = {}
    for s in model['switches']:
        i = P.switch_info(model, s['id'])
        states = [['off', 'Off'], ['on', 'On']] + ([['blinking', 'Blinking']] if i['kind'] == '3-state' else [])
        known = i['state'] in [x[0] for x in states]
        out[i['name']] = {
            'states': states, 'default': i['state'] if known else 'off', 'source': 'backup',
            'note': f"{i['kind']}. Position in the backup: {i['state']}."
                    + ('' if known else ' (not decoded; check it in WMS)')}
    return out


class FlowBuilder:
    """Turns one inbound number's dialplan path into spec nodes. Mirrors MermaidBuilder.chain() in the parser."""
    MAX_NODES = 90

    def __init__(self, model, full_numbers=False, call_class='external'):
        self.m = model
        self.full = full_numbers
        self.cls = call_class
        self.nodes = {}
        self.n = 0
        self.memo = {}
        self.pre = []
        self.tracer = P.Tracer(model, call_class)
        self.people = {}
        self.truncated = False

    def add(self, type_, title, sub=None, **kw):
        self.n += 1
        nid = f'n{self.n}'
        lines = [x for x in self.pre + list(sub or []) if x]
        self.pre = []
        node = {'type': type_, 'title': title}
        if lines:
            node['sub'] = lines
        node.update({k: v for k, v in kw.items() if v not in (None, '', [], {})})
        self.nodes[nid] = node
        return nid

    def connect(self, pend, target):
        for nid, key in pend:
            if key == 'next':
                self.nodes[nid]['next'] = target
            else:
                self.nodes[nid].setdefault('go', {})[key] = target

    def person(self, ext):
        label = P.user_label(self.m, ext)
        self.people[label] = {'label': label, 'ext': ext}
        return label

    def emit(self, dp_name, number):
        if '$' in number:
            return self.add('end', f'Goto {number}', [f'in [{dp_name}]: dynamic number, not followed'])
        if self.n > self.MAX_NODES:
            self.truncated = True
            return self.add('end', 'Diagram truncated', [f'{number} in [{dp_name}]'])
        if dp_name in P.BUILTIN_DIALPLANS:
            return self.builtin(number, f"built-in '{dp_name}'")
        hit = self.tracer.find(dp_name, number)
        if hit is None:
            if dp_name not in self.m['dialplans']:
                return self.add('hangup', f"Dialplan '{dp_name}' does not exist")
            return self.builtin(number, f'no rule in [{dp_name}]')
        if hit in self.memo:
            return self.memo[hit]
        hdp, hnum = hit
        rules = self.m['dialplans'][hdp]['numbers'][hnum]
        comments = sorted({r['comment'] for r in rules if r['comment']})
        ids = [r['rule_id'] for r in rules]
        rng = f"rule {ids[0]}" if len(ids) == 1 else f"rules {min(ids)}-{max(ids)}"
        head = self.add('route', f'[{hdp}] {P.number_label(hnum)}', ['; '.join(comments)] if comments else None, step=f'[{hdp}] {rng}')
        self.memo[hit] = head
        self.chain([(head, 'next')], rules, hdp, number)
        return head

    def chain(self, pend, rules, dp_name, number):
        for r in rules:
            app, p = r['app'], r['params'] + [''] * 6
            step = f"[{dp_name}] rule {r['rule_id']}"
            if app in ('Playback', 'Background'):
                self.pre.append(f'Plays {unquote(p[0])}')
                continue
            if app == 'MuxMon':
                self.pre.append('Call recording on')
                continue
            if app == 'Answer':
                continue
            if app in ('Set', 'CustomApp'):
                s = P._short(r) if app == 'CustomApp' else None
                if s:
                    self.pre.append(s[:50])
                continue
            if app == 'Goto':
                tnum = (p[0] or number).replace('${EXTEN}', number)
                if self.pre:
                    plays = all(x.startswith('Plays ') for x in self.pre)
                    nid = self.add('play', 'Plays a message' if plays else 'Before the jump', step=step)
                    self.connect(pend, nid)
                    pend = [(nid, 'next')]
                self.connect(pend, self.emit(p[1] or dp_name, tnum))
                return
            if app == 'GotoIf':
                cond = unquote(p[0])
                # Switch state lives in the AstDB as TIMETABLE/<switch id> (e.g. a *80 code doing Set(DB(TIMETABLE/1)=1)).
                sw = next((x for x in self.m['switches']
                           if re.search(rf"DB\(TIMETABLE/{x['id']}\)", cond)), None)
                title = f"Switch '{sw['switchname']}'" if sw else 'Condition'
                nid = self.add('condition', title, [cond[:60]], label=f'IF {cond}', step=step)
                self.connect(pend, nid)
                tnum = p[1].replace('${EXTEN:}', number).replace('${EXTEN}', number) or number
                self.nodes[nid]['go'] = {'true': self.emit(p[2] or dp_name, tnum)}
                pend = [(nid, 'false')]
                continue
            if app in P.CONDITIONAL_JUMPS:      # Timetable and the "Jump to if ..." apps (GotoIf is above)
                targets = P.jump_targets(r, dp_name, number)
                known_tt = any(str(t['id']) == p[0] for t in self.m['timetables'])
                if app == 'Timetable' and known_tt:
                    name = P.timetable_name(self.m, p[0])
                    nid = self.add('timetable', f"Timetable '{name}'", timetable=name, step=step)
                    keys, other = ['open'], 'closed'
                elif app in ('GotoIfSwitchIsOn', 'GotoIf3SwitchIsOnOrBlinking') and \
                        any(str(x['id']) == p[0] for x in self.m['switches']):
                    sw = P.switch_info(self.m, p[0])
                    nid = self.add('switch', f"Switch '{sw['name']}'", [f"{sw['kind']}, {sw['state']} in the backup"],
                                   switch=sw['name'], step=step)
                    keys, other = [b for b, _, _ in targets], 'off'
                else:
                    cond = P.condition_text(self.m, r)
                    nid = self.add('condition', {'GotoIfCallType': f'Call type {p[0]}?',
                                                 'GotoIfBusyCongested': f"Last dial {p[0] or 'busy'}?"}.get(app, 'Condition'),
                                   [cond[:60]], label=cond[:1].upper() + cond[1:], step=step)
                    keys, other = ['true'], 'false'
                self.connect(pend, nid)
                self.nodes[nid]['go'] = {k: self.emit(tdp, tnum) for k, (_, tdp, tnum) in zip(keys, targets)}
                pend = [(nid, other)]
                continue
            if app == 'Queue':
                q = self.m['queues'].get(p[0])
                if not q:
                    nid = self.add('hangup', f'Call group {p[0]} (unknown)', step=step)
                    self.connect(pend, nid)
                    return
                pr = q['params']
                members = [self.person(m['member']) for m in q['members']]
                max_wait = int(p[4]) if str(p[4]).isdigit() else None
                max_len = int(pr.get('maxlen') or 0)
                sub = [f"{pr.get('strategy') or 'strategy ?'}, ring {pr.get('timeout')} s",
                       f"max wait {max_wait} s" if max_wait else 'no max wait']
                if not members:
                    sub.append('NO MEMBERS')
                nid = self.add('queue', f"Call group '{q['name']}'", sub, members=members, group_id=q['id'],
                               strategy=pr.get('strategy'), max_len=max_len, max_wait=max_wait, step=step)
                self.connect(pend, nid)
                # A full queue (maxlen reached) and a max-wait timeout both continue with the next rule.
                pend = ([(nid, 'timeout')] if max_wait else []) + ([(nid, 'full')] if max_len else [])
                if not pend:
                    return
                continue
            if app == 'DialPhone':
                ext = p[0].replace('${EXTEN}', number)
                u = next((x for x in self.m['users'] if x.get('telephoneNumber') == ext), None)
                if u and u.get('userKind') == 'park_orbit':
                    pt = self.m['park'].get('timeout_ms')
                    nid = self.add('park', f'Park orbit {ext}',
                                   [f'hold music for {pt / 1000:g} s' if isinstance(pt, int) else 'park timeout unknown',
                                    'then rings back the parker'], step=step)
                    self.connect(pend, nid)
                    return
                af = P.active_features(u, self.cls) if u else {}
                who = self.person(ext) if u else ('the dialled number' if '$' in p[0] else ext)
                if 'FCF' in af:
                    nid = self.add('forward', f'{who}: forward all', [f"to {mask_number(af['FCF'], self.full)}"], step=step)
                    self.connect(pend, nid)
                    return
                tout = p[1] or (af.get('TOUT') if str(af.get('TOUT', '')).isdigit() else None)
                if not u:
                    self.people.setdefault(who, {'label': who})
                nid = self.add('ring', f'Ring {who}', [f'{tout} s' if tout else "user's own timeout"],
                               members=[who], timeout=int(tout) if str(tout or '').isdigit() else None, step=step)
                self.connect(pend, nid)
                if 'UCF' in af:
                    self.nodes[nid]['go'] = {'no_answer': self.add(
                        'forward', 'Forward if no answer', [f"{who} to {mask_number(af['UCF'], self.full)}"])}
                    return
                if u is None and '$' in p[0]:
                    self.nodes[nid]['go'] = {'no_answer': self.add('end', "Callee's own forwards apply")}
                    return
                pend = [(nid, 'no_answer')]
                continue
            if app == 'DialTrunk':
                tgt = mask_number(p[0].replace('${EXTEN}', number), self.full)
                nid = self.add('dial_external', f'Dial {tgt}', [f'via {unquote(p[1])}'], target=tgt,
                               trunk=unquote(p[1]), step=step)
                self.connect(pend, nid)
                pend = [(nid, 'no_answer')]
                continue
            if app == 'WaitExten':
                nid = self.add('ivr', 'Menu: wait for a key', [f'waits {p[0]} s'] if p[0] else None,
                               wait=int(p[0]) if str(p[0]).isdigit() else None, step=step)
                self.connect(pend, nid)
                opts = sorted((n for n in self.m['dialplans'][dp_name]['numbers']
                               if not P.is_pattern(n) and n != number and len(n) <= 4), key=P.natural)
                self.nodes[nid]['go'] = {d: self.emit(dp_name, d) for d in opts}
                pend = [(nid, 'timeout')]
                continue
            if app in ('VoiceMail', 'FaxToEmail', 'Hangup'):
                t, title = {'VoiceMail': ('voicemail', f'Voicemail {unquote(p[0])}'),
                            'FaxToEmail': ('fax', f'Fax to email {unquote(p[0])}'),
                            'Hangup': ('hangup', 'Hang up')}[app]
                nid = self.add(t, title, mailbox=unquote(p[0]) if app == 'VoiceMail' else None, step=step)
                self.connect(pend, nid)
                return
            self.pre.append(P.fmt_rule(r)[:50])
        nid = self.add('hangup', 'End of rules', ['PBX hangs up'])
        self.connect(pend, nid)

    def builtin(self, number, why):
        u = next((x for x in self.m['users'] if x.get('telephoneNumber') == number), None)
        if u and u.get('userKind') == 'park_orbit':
            return self.add('park', f'Park orbit {number}', [why])
        if u:
            who = self.person(number)
            nid = self.add('ring', f'Ring {who}', [f'{why}: built-in routing'], members=[who])
            self.nodes[nid]['go'] = {'no_answer': self.add('end', "User's own forwards apply")}
            return nid
        return self.add('hangup', 'Number not found', [why])


def flows_from_backup(model, full_numbers=False, prefix=''):
    """One flow per (inbound dialplan, number), keyed by the trunks that reach it."""
    entries = {}
    for t in model['trunks']:
        dp = t.get('inbound_dialplan')
        if dp not in model['dialplans']:
            continue
        for num in sorted(model['dialplans'][dp]['numbers'], key=P.specificity):
            entries.setdefault((dp, num), []).append(t['name'])
    flows, people, notes = [], {}, []
    for i, ((dp, num), trunks) in enumerate(entries.items(), 1):
        fb = FlowBuilder(model, full_numbers)
        kind = 'pattern' if P.is_pattern(num) else 'number'
        start = fb.add('start', f'Call to {num}', [f"from {', '.join(trunks)}"], say_text=None)
        fb.nodes[start]['next'] = fb.emit(dp, num)
        fl = {'id': f'{prefix}b{i}', 'label': f"{prefix and 'Current: '}{num} ({kind}, {', '.join(trunks)})",
              'start': start, 'nodes': fb.nodes, 'source': 'backup'}
        if fb.truncated:
            fl['note'] = f'Diagram truncated after {FlowBuilder.MAX_NODES} steps.'
            notes.append(f'{num}: diagram truncated')
        flows.append(fl)
        people.update(fb.people)
    park = park_flow(model, full_numbers, prefix)
    if park:
        flows.append(park[0])
        people.update(park[1])
    return flows, people, notes


PARKER = 'The parker'


def park_flow(model, full_numbers=False, prefix=''):
    """What happens to a parked call: pick-up from the park key, ring-back to the parker, then the parker's forward
    or the reserved [receptionist] dialplan (its 'default' number, stored as 's'). The ring-back order was confirmed
    on a test PBX with no forwards; whether a parker's forward wins over [receptionist] is not confirmed."""
    p = model['park']
    if not p['orbits']:
        return None
    t = p.get('timeout_ms')
    secs = f'{t / 1000:g}' if isinstance(t, int) else '?'
    ring = model['system'].get('globals', {}).get('GINTDIAL_TIMEOUT')
    fb = FlowBuilder(model, full_numbers, call_class='internal')
    fb.people[PARKER] = {'label': PARKER}
    start = fb.add('start', 'Call parked', [f"on {', '.join(o['number'] for o in p['orbits'])}", f'hold music {secs} s'])
    pick = fb.add('condition', 'Picked up from park key?', [f'within {secs} s'],
                  label=f'Someone picks up the parked call from the park key within {secs} s')
    got = fb.add('answered', 'Picked up from the park key', banner='Picked up from the park key')
    rb = fb.add('ring', 'Ring back the parker', [f'up to {ring} s (GINTDIAL_TIMEOUT)' if ring else ''], members=[PARKER],
                timeout=int(ring) if str(ring or '').isdigit() else None, skip_if_none_free=True,
                pick_label='The parker answers the ring-back')
    fwd = fb.add('condition', "Parker's forward on?", ['internal fwd-busy / fwd-noanswer'],
                 label="The parker has an internal forward-if-busy / no-answer (it probably takes the call first: unconfirmed)")
    fwd_t = fb.add('forward', "Parker's forward", ['often their voicemail'])
    rest = (fb.emit(P.RECEPTIONIST_DP, 's') if p.get('return_route')
            else fb.add('hangup', f'No [{P.RECEPTIONIST_DP}] default', ['nothing else is tried (unconfirmed)']))
    fb.nodes[start]['next'] = pick
    fb.nodes[pick]['go'] = {'true': got, 'false': rb}
    fb.nodes[pick]['labels'] = {'true': 'Yes', 'false': 'Timeout'}
    fb.nodes[rb]['go'] = {'no_answer': fwd}
    fb.nodes[rb]['labels'] = {'no_answer': 'No answer / busy'}
    fb.nodes[fwd]['go'] = {'true': fwd_t, 'false': rest}
    flow = {'id': f'{prefix}park', 'label': f"{prefix and 'Current: '}Parked call returns", 'start': start,
            'nodes': fb.nodes, 'source': 'backup',
            'note': f"If nobody picks it up within {secs} s, the PBX rings back whoever parked it. If they're busy or don't "
                    f"answer, the PBX sends the call to the reserved [{P.RECEPTIONIST_DP}] dialplan's 'default' number. "
                    "A forward set on the parker probably takes the call first (not confirmed)."}
    return flow, fb.people


# ---------------------------------------------------------------- plan

def load_plan(path):
    try:
        plan = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError) as e:
        raise SpecError(f'cannot read plan {path}: {e}')
    if not isinstance(plan, dict) or not plan.get('flows'):
        raise SpecError('plan needs a "flows" list (see references/flow-spec.md)')
    return plan


def plan_timetables(plan):
    out = {}
    for name, t in (plan.get('timetables') or {}).items():
        t = dict(t)
        if 'hours' in t:
            t['hours'] = parse_hours(t['hours'])
            t.setdefault('kind', 'weekly')
            t['summary'] = hours_text(t['hours'])
        else:
            t.setdefault('kind', 'dates')
        out[name] = t
    return out


def plan_switches(plan):
    out = {}
    for name, s in (plan.get('switches') or {}).items():
        s = dict(s)
        states = s.get('states') or [['off', 'Off'], ['on', 'On']]
        s['states'] = [list(x) if isinstance(x, (list, tuple)) else [x, x] for x in states]
        s.setdefault('default', s['states'][0][0])
        out[name] = s
    return out


# ---------------------------------------------------------------- resolve plan objects against a backup

def resolve(spec, model, warnings, open_items):
    """Mark plan objects existing / to_create / not_found and take real details from the backup."""
    objects = []
    have = model is not None

    def status(found):
        return ('existing' if found else 'to_create') if have else 'planned'

    tts = {norm(t['timetable']): t for t in (model['timetables'] if have else [])}
    btt = backup_timetables(model) if have else {}
    for name, t in spec['timetables'].items():
        if t.get('source') == 'backup':
            continue
        hit = tts.get(norm(name))
        st = status(hit)
        detail = t.get('summary') or t.get('label') or ''
        if hit:
            b = btt[hit['timetable']]
            if t.get('kind') == 'weekly' and not t.get('hours'):
                t['hours'] = b['hours']
            elif t.get('kind') == 'weekly' and b['hours'] and t['hours'] != b['hours']:
                warnings.append(f"timetable '{name}': plan hours ({hours_text(t['hours'])}) differ from the PBX "
                                f"({b['summary']})")
                detail += f" (PBX now: {b['summary']})"
        objects.append({'type': 'Timetable', 'name': name, 'status': st, 'detail': detail})

    sws = {norm(s['switchname']): s for s in (model['switches'] if have else [])}
    for name, s in spec['switches'].items():
        if s.get('source') == 'backup':
            continue
        hit = sws.get(norm(name))
        objects.append({'type': 'Switch', 'name': name, 'status': status(hit),
                        'detail': f"{len(s['states'])}-state: " + ', '.join(x[1] for x in s['states'])})

    users = [u for u in (model['users'] if have else []) if u.get('userKind') == 'user']
    for name, p in spec['people'].items():
        if p.get('source') == 'backup':
            continue
        if not have:
            objects.append({'type': 'Person', 'name': name, 'status': 'planned', 'detail': p.get('ext') or ''})
            continue
        if p.get('external'):
            continue
        ext = str(p.get('ext') or '')
        cands = [u for u in users if ext and u.get('telephoneNumber') == ext]
        if not cands:
            n = norm(p.get('match') or name)
            cands = [u for u in users if norm(u.get('cn')) == n] or \
                    [u for u in users if norm(u.get('cn')).split(' ')[0] == n.split(' ')[0] and n]
        if len(cands) == 1:
            u = cands[0]
            p['label'] = p.get('label') or f"{name} ({u.get('telephoneNumber')})"
            p['ext'] = u.get('telephoneNumber')
            objects.append({'type': 'User', 'name': name, 'status': 'existing',
                            'detail': f"{u.get('telephoneNumber')} {u.get('cn')}"})
        elif len(cands) > 1:
            opts = ', '.join(f"{u.get('telephoneNumber')} {u.get('cn')}" for u in cands[:5])
            warnings.append(f"person '{name}' matches several users: {opts}")
            open_items.append(['o', f"Which user is '{name}'? Candidates in the PBX: {opts}"])
            objects.append({'type': 'User', 'name': name, 'status': 'ambiguous', 'detail': opts})
        else:
            warnings.append(f"person '{name}' not found among the PBX users")
            open_items.append(['o', f"'{name}' is not a user in the PBX yet: create the user or confirm the name/extension"])
            objects.append({'type': 'User', 'name': name, 'status': 'not_found', 'detail': ext})

    qs = {norm(q['name']): q for q in (model['queues'].values() if have else [])}
    for name, g in spec['groups'].items():
        hit = qs.get(norm(name))
        detail = ', '.join(m if isinstance(m, str) else m['name'] for m in g.get('members', []))
        if hit and not g.get('members'):
            g['members'] = [P.user_label(model, m['member']) for m in hit['members']]
            for lab, m in zip(g['members'], hit['members']):
                spec['people'].setdefault(lab, {'label': lab, 'ext': m['member'], 'source': 'backup'})
            detail = ', '.join(g['members'])
        if hit:
            pr = hit['params']
            detail += f" (PBX: {pr.get('strategy')}, ring {pr.get('timeout')} s, maxlen {pr.get('maxlen')})"
        objects.append({'type': 'Call group', 'name': name, 'status': status(hit), 'detail': detail})

    trunks = {norm(t['name']) for t in (model['trunks'] if have else [])}
    seen = set()
    for f in spec['flows']:
        for n in f['nodes'].values():
            tr = n.get('trunk')
            if n.get('type') == 'dial_external' and tr and tr not in seen and f.get('source') != 'backup':
                seen.add(tr)
                hit = norm(tr) in trunks
                if have and not hit:
                    warnings.append(f"trunk '{tr}' not found in the PBX")
                objects.append({'type': 'Trunk', 'name': tr, 'status': status(hit) if hit or not have else 'not_found',
                                'detail': ''})
    return objects


# ---------------------------------------------------------------- validation

def branches(node, spec):
    """[(branch_key, target)] for every outgoing edge, including implicit fallbacks."""
    t = node['type']
    if t in SINGLE_NEXT:
        return [('next', node['next'])] if node.get('next') else []
    go = dict(node.get('go') or {})
    out = [(k, v) for k, v in go.items() if k != '*' and v]
    if t == 'switch':
        sw = spec['switches'].get(node.get('switch'), {})
        for v, _ in sw.get('states', []):
            if v not in go and go.get('*'):
                out.append((v, go['*']))
    if t == 'ring' and node.get('skip_if_none_free') and 'skipped' not in go and go.get('no_answer'):
        out.append(('skipped', go['no_answer']))
    if t == 'queue' and node.get('max_len') and 'full' not in go and go.get('timeout'):
        out.append(('full', go['timeout']))
    return out


def validate(spec):
    errs = []
    for f in spec['flows']:
        nodes = f['nodes']
        if f.get('start') not in nodes:
            errs.append(f"flow {f['id']}: start node {f.get('start')!r} not found")
        for nid, n in nodes.items():
            where = f"flow {f['id']} node {nid}"
            if n.get('type') not in NODE_TYPES:
                errs.append(f"{where}: unknown type {n.get('type')!r}")
                continue
            if not n.get('title'):
                errs.append(f'{where}: missing title')
            for b, tgt in branches(n, spec):
                if tgt not in nodes:
                    errs.append(f'{where}: branch {b!r} points to missing node {tgt!r}')
            t = n['type']
            if t == 'switch' and n.get('switch') not in spec['switches']:
                errs.append(f"{where}: switch {n.get('switch')!r} is not defined in \"switches\"")
            if t == 'switch':
                states = [s[0] for s in spec['switches'].get(n.get('switch'), {}).get('states', [])]
                for k in (n.get('go') or {}):
                    if k != '*' and k not in states:
                        errs.append(f'{where}: go key {k!r} is not a state of switch {n.get("switch")!r}')
            if t == 'timetable' and n.get('timetable') not in spec['timetables']:
                errs.append(f"{where}: timetable {n.get('timetable')!r} is not defined in \"timetables\"")
            if t == 'decide':
                for c in n.get('cases', []):
                    if c.get('go') not in (n.get('go') or {}):
                        errs.append(f"{where}: case go {c.get('go')!r} has no entry in \"go\"")
                    if c.get('timetable') and c['timetable'] not in spec['timetables']:
                        errs.append(f"{where}: timetable {c['timetable']!r} is not defined")
                if n.get('else') not in (n.get('go') or {}):
                    errs.append(f'{where}: "else" must name a key of "go"')
            if t in ('ring', 'queue'):
                g = n.get('group')
                if g and g not in spec['groups']:
                    errs.append(f'{where}: group {g!r} is not defined in "groups"')
                for m in (n.get('members') or spec['groups'].get(g, {}).get('members', [])):
                    name = m if isinstance(m, str) else m.get('name')
                    if name not in spec['people']:
                        errs.append(f'{where}: member {name!r} is not defined in "people"')
            for expr in _exprs(n):
                errs.extend(f'{where}: {e}' for e in _check_expr(expr, spec))
    for p in spec['presets']:
        if 'flow' in p and p['flow'] not in {f['id'] for f in spec['flows']}:
            errs.append(f"preset {p.get('label')!r}: unknown flow {p['flow']!r}")
    if errs:
        raise SpecError('\n'.join(errs))


def _exprs(n):
    out = []
    if n.get('if'):
        out.append(n['if'])
    for c in n.get('cases', []):
        if c.get('when'):
            out.append(c['when'])
    for m in n.get('members', []) or []:
        if isinstance(m, dict) and m.get('when'):
            out.append(m['when'])
    return out


def _check_expr(x, spec):
    if not isinstance(x, dict):
        return [f'bad condition {x!r}']
    if 'any' in x or 'all' in x:
        return [e for y in x.get('any', x.get('all')) for e in _check_expr(y, spec)]
    if 'not' in x:
        return _check_expr(x['not'], spec)
    if 'switch' in x:
        sw = spec['switches'].get(x['switch'])
        if not sw:
            return [f"switch {x['switch']!r} is not defined"]
        states = [s[0] for s in sw['states']]
        bad = [v for v in ([x.get('is')] if not isinstance(x.get('is'), list) else x['is']) if v not in states]
        return [f"switch {x['switch']!r} has no state {v!r}" for v in bad]
    if 'timetable' in x:
        return [] if x['timetable'] in spec['timetables'] else [f"timetable {x['timetable']!r} is not defined"]
    if 'flag' in x:
        return [] if x['flag'] in spec.get('flags', {}) else [f"flag {x['flag']!r} is not defined in \"flags\""]
    return [f'bad condition {x!r}']


# ---------------------------------------------------------------- layout

NODE_W, GAP_X, GAP_Y, MARGIN = 196, 64, 34, 20
CHARS = 30


def wrap(text, width=CHARS):
    words, lines, cur = str(text).split(), [], ''
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f'{cur} {w}'.strip()
    if cur:
        lines.append(cur)
    return lines or ['']


def node_lines(n):
    t = wrap(n['title'], 26)[:2]
    s = [x for line in (n.get('sub') or []) for x in wrap(line)][:3]
    return [['t', x] for x in t] + [['s', x] for x in s]


def layout(flow, spec):
    """Grid layout: the main path runs down column 0, branches step right, shared nodes go below their callers."""
    nodes = flow['nodes']
    out = {nid: [b for b in branches(n, spec)] for nid, n in nodes.items()}
    # Back edges (loops to an earlier step) don't make a node "shared": find them with a DFS from the start.
    back, state = set(), {}

    def dfs(u):
        state[u] = 1
        for _, v in out.get(u, []):
            if state.get(v) == 1:
                back.add((u, v))
            elif v not in state and v in nodes:
                dfs(v)
        state[u] = 2

    sys.setrecursionlimit(max(sys.getrecursionlimit(), 4 * len(nodes) + 100))
    for root in [flow['start']] + list(nodes):
        if root not in state:
            dfs(root)
    indeg = {}
    for nid, bs in out.items():
        for t in {t for _, t in bs}:
            if (nid, t) not in back:
                indeg[t] = indeg.get(t, 0) + 1

    size_memo = {}

    def size(nid, stack=()):
        if nid in stack:
            return 0
        if nid not in size_memo:
            size_memo[nid] = 1 + sum(size(t, stack + (nid,)) for _, t in out[nid] if indeg.get(t, 0) <= 1)
        return size_memo[nid]

    pos, occ = {}, {}
    fixed = {k: tuple(v) for k, v in (flow.get('layout') or {}).items()}
    for k, v in fixed.items():
        occ[v] = k
    deferred = []

    def free(c, r):
        return (c, r) not in occ

    def reserve(nid, c, r):
        if nid in fixed:
            pos[nid] = fixed[nid]
        else:
            while not free(c, r):
                r += 1
            pos[nid] = (c, r)
            occ[(c, r)] = nid

    def place(nid, c, r):
        reserve(nid, c, r)
        expand(nid)

    def expand(nid):
        c, r = pos[nid]
        n = nodes[nid]
        kids = []
        for _, t in out[nid]:
            if t not in kids:
                kids.append(t)
        if not kids:
            return
        prim = n.get('primary')
        if prim is None:
            pref = PRIMARY.get(n['type'])
            if n['type'] == 'switch':
                pref = spec['switches'].get(n.get('switch'), {}).get('default')
            if n['type'] == 'decide':
                pref = n.get('else')
            ordered = sorted(out[nid], key=lambda kt: (-(size(kt[1]) if indeg.get(kt[1], 0) <= 1 else 0),
                                                      kt[0] != pref))
            prim = ordered[0][1]
        else:
            prim = (n.get('go') or {}).get(prim, n.get('next'))
        todo = []
        for t in [prim] + [t for t in kids if t != prim]:
            if t in pos or t in deferred:
                continue
            if indeg.get(t, 0) > 1:
                deferred.append(t)
            else:
                todo.append(t)
        side = [t for t in todo if t != prim]
        # One side branch sits beside the node when that cell is free; otherwise (or with several) they fan out
        # on the row below, reached through the gap between the rows so no line crosses a box.
        side_row = r if len(side) == 1 and free(c + 1, r) else r + 1
        if prim in todo:
            reserve(prim, c, r + 1)
        cc = c + 1
        for t in side:
            while not free(cc, side_row):
                cc += 1
            reserve(t, cc, side_row)
            cc += 1
        for t in todo:
            expand(t)

    place(flow['start'], 0, 0)
    stalled = 0
    while deferred:
        t = deferred.pop(0)
        if t in pos:
            continue
        preds = [nid for nid, bs in out.items() if any(x == t for _, x in bs) and nid in pos]
        if not preds and stalled <= len(deferred):
            deferred.append(t)      # its callers aren't placed yet
            stalled += 1
            continue
        stalled = 0
        if not preds:
            place(t, 0, max(r for _, r in pos.values()) + 1)
            continue
        cols = sorted(pos[p][0] for p in preds)
        c = cols[len(cols) // 2] if len(cols) > 1 else cols[0]
        r = max(pos[p][1] for p in preds) + (1 if len(preds) == 1 else 0)
        if len(preds) > 1 and c == 0:
            r = max(pos[p][1] for p in preds) + 1
        place(t, c, r)
    for nid in nodes:                       # unreachable nodes (still drawn, below everything)
        if nid not in pos:
            place(nid, 0, max((r for _, r in pos.values()), default=0) + 1)

    # pixel geometry
    lines = {nid: node_lines(nodes[nid]) for nid in nodes}
    hgt = {nid: 22 + 18 * len(lines[nid]) for nid in nodes}
    rows = max(r for _, r in pos.values()) + 1
    cols = max(c for c, _ in pos.values()) + 1
    row_h = [max([hgt[n] for n, p in pos.items() if p[1] == r] or [40]) for r in range(rows)]
    row_y = [MARGIN]
    for r in range(rows - 1):
        row_y.append(row_y[-1] + row_h[r] + GAP_Y)
    geo_nodes = {}
    for nid, (c, r) in pos.items():
        x = MARGIN + c * (NODE_W + GAP_X)
        y = row_y[r] + (row_h[r] - hgt[nid]) / 2
        geo_nodes[nid] = {'x': x, 'y': round(y, 1), 'w': NODE_W, 'h': hgt[nid], 'lines': lines[nid]}
    width = MARGIN * 2 + cols * NODE_W + (cols - 1) * GAP_X + 40
    height = row_y[-1] + row_h[-1] + MARGIN

    edges, gutter = [], {}
    for nid, bs in out.items():
        by_target = {}
        for k, t in bs:
            by_target.setdefault(t, []).append(k)
        for t, keys in by_target.items():
            a, b = geo_nodes[nid], geo_nodes[t]
            (ac, ar), (bc, br) = pos[nid], pos[t]
            n = nodes[nid]
            label = ' / '.join(dict.fromkeys(_edge_label(n, k, spec) for k in keys if _edge_label(n, k, spec)))
            if n['type'] in SINGLE_NEXT and not n.get('labels'):
                label = ''
            acx, acy, bcx, bcy = a['x'] + a['w'] / 2, a['y'] + a['h'] / 2, b['x'] + b['w'] / 2, b['y'] + b['h'] / 2
            blocked_col = any(pos.get(x) == (ac, rr) for x in pos for rr in range(ar + 1, br))
            if ac == bc and br > ar and not blocked_col:
                d = f"M{acx} {a['y'] + a['h']} L{bcx} {b['y'] - 2}"
                lx, ly, anchor = acx + 10, a['y'] + a['h'] + 16, 'start'
            elif bc > ac and br == ar:
                d = f"M{a['x'] + a['w']} {acy} L{b['x'] - 2} {bcy}"
                lx, ly, anchor = a['x'] + a['w'] + GAP_X / 2, acy - 8, 'middle'
            elif bc > ac and br == ar + 1:     # fan-out to the next row: bus through the gap between the rows
                gy = row_y[ar] + row_h[ar] + GAP_Y / 2
                d = f"M{acx} {a['y'] + a['h']} L{acx} {gy} L{bcx} {gy} L{bcx} {b['y'] - 2}"
                lx, ly, anchor = bcx + 6, b['y'] - 6, 'start'
            elif bc > ac and br > ar:
                d = f"M{a['x'] + a['w']} {acy} L{bcx} {acy} L{bcx} {b['y'] - 2}"
                lx, ly, anchor = bcx + 6, b['y'] - 8, 'start'
            elif bc < ac and br > ar:
                d = f"M{acx} {a['y'] + a['h']} L{acx} {bcy} L{b['x'] + b['w'] + 2} {bcy}"
                lx, ly, anchor = acx - 8, a['y'] + a['h'] + 16, 'end'
            else:  # same column but blocked, upward, or leftward-up: go round through the gutter on the right
                g = gutter.get(max(ac, bc), 0)
                gutter[max(ac, bc)] = g + 1
                gx = MARGIN + max(ac, bc) * (NODE_W + GAP_X) + NODE_W + 14 + 7 * g
                d = f"M{a['x'] + a['w']} {acy} L{gx} {acy} L{gx} {bcy} L{b['x'] + b['w'] + 2} {bcy}"
                lx, ly, anchor = a['x'] + a['w'] + 6, acy - 8, 'start'
            edges.append({'from': nid, 'to': t, 'keys': [f'{nid}>{k}' for k in keys], 'd': d, 'label': label,
                          'lx': round(lx, 1), 'ly': round(ly, 1), 'anchor': anchor})
    return {'w': round(width), 'h': round(height), 'nodes': geo_nodes, 'edges': edges, 'grid': {k: list(v) for k, v in pos.items()}}


def _edge_label(n, k, spec):
    if (n.get('labels') or {}).get(k):
        return n['labels'][k]
    t = n['type']
    if t == 'switch':
        for v, lab in spec['switches'].get(n.get('switch'), {}).get('states', []):
            if v == k:
                return lab
    if t == 'ivr' and k != 'timeout':
        return f'Press {k}' if len(k) <= 2 else f'Dials {k}'
    if t == 'ivr' and k == 'timeout':
        return 'No key'
    if t == 'decide':
        return k
    return DEFAULT_LABELS.get(k, k if k != 'next' else '')


# ---------------------------------------------------------------- tabs

INLINE_CHIP = re.compile(r'\[(o|v|d|c|x)(?::\s*([^\]]*))?\]')


def inline(s):
    """Escape text, then apply `code`, **bold** and [o: chip] markup."""
    s = html.escape(str(s), quote=False)
    s = re.sub(r'`([^`]+)`', r'<code>\1</code>', s)
    s = re.sub(r'\*\*([^*]+)\*\*', r'<b>\1</b>', s)
    names = {'o': 'Open', 'v': 'Verify', 'd': 'Done', 'c': 'To create', 'x': 'Problem'}
    return INLINE_CHIP.sub(lambda m: f'<span class="chip {m.group(1)}">{m.group(2) or names[m.group(1)]}</span>', s)


def blocks_html(blocks):
    out = []
    for b in blocks or []:
        if isinstance(b, str):
            out.append(f'<p>{inline(b)}</p>')
            continue
        k, v = next(iter(b.items()))
        if k in ('h2', 'h3'):
            out.append(f'<{k}>{inline(v)}</{k}>')
        elif k == 'p':
            out.append(f'<p>{inline(v)}</p>')
        elif k == 'note':
            out.append(f'<p class="note">{inline(v)}</p>')
        elif k == 'list':
            out.append('<ul class="tight">' + ''.join(f'<li>{inline(x)}</li>' for x in v) + '</ul>')
        elif k == 'table':
            rows = ''.join('<tr><th>' + inline(r[0]) + '</th>' + ''.join(f'<td>{inline(c)}</td>' for c in r[1:]) + '</tr>'
                           for r in v)
            out.append(f'<div class="tw"><table class="kv">{rows}</table></div>')
        elif k == 'grid':
            head, *rows = v
            th = ''.join(f'<th>{inline(c)}</th>' for c in head)
            body = ''.join('<tr>' + ''.join(f'<td>{inline(c)}</td>' for c in r) + '</tr>' for r in rows)
            out.append(f'<div class="tw"><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>')
        elif k == 'steps':
            items = ''.join(f'<li class="grp">{inline(x["group"])}</li>' if isinstance(x, dict) else f'<li>{inline(x)}</li>'
                            for x in v)
            out.append(f'<ol class="steps">{items}</ol>')
        elif k == 'cols':
            out.append('<div class="cols">' + ''.join(f'<div>{blocks_html(c)}</div>' for c in v) + '</div>')
        else:
            raise SpecError(f'unknown block type {k!r} (use h2, h3, p, note, list, table, grid, steps, cols)')
    return ''.join(out)


def objects_html(objects):
    if not objects:
        return ''
    rows = [['Object', 'Name', 'Status', 'Detail']]
    for o in objects:
        cls, txt = STATUS_CHIP[o['status']]
        rows.append([o['type'], f"`{o['name']}`", f'[{cls}: {txt}]', o.get('detail') or ''])
    return blocks_html([{'h2': 'Objects in the plan'}, {'grid': rows}])


def checklist_panel(kind, title, intro):
    return (f'<section class="panel cl" id="p-{kind}" role="tabpanel" aria-labelledby="t-{kind}" hidden>'
            f'<h2>{html.escape(title)}</h2><p>{inline(intro)}</p>'
            f'<div class="progress" aria-hidden="true"><div id="{kind}Bar"></div></div>'
            f'<div><span id="{kind}Count"></span><button class="resetbtn" type="button" id="{kind}Reset">Clear ticks</button></div>'
            f'<div id="{kind}List"></div></section>')


def panels(spec, tabs, objects):
    out, index = [], []
    build = blocks_html(tabs.get('build')) + objects_html(objects)
    if build:
        index.append({'id': 'build', 'label': tabs.get('build_label', 'Build plan')})
        out.append(f'<section class="panel" id="p-build" role="tabpanel" aria-labelledby="t-build" hidden>{build}</section>')
    for i, x in enumerate(tabs.get('extra') or [], 1):
        index.append({'id': f'x{i}', 'label': x['title']})
        out.append(f'<section class="panel" id="p-x{i}" role="tabpanel" aria-labelledby="t-x{i}" hidden>'
                   f'{blocks_html(x.get("blocks"))}</section>')
    if spec['uat']:
        index.append({'id': 'uat', 'label': 'UAT checklist'})
        out.append(checklist_panel('uat', 'UAT checklist', tabs.get('uat_intro', 'Ticks are saved in this browser only.')))
    if spec['open']:
        index.append({'id': 'open', 'label': 'Open items'})
        out.append(checklist_panel('open', 'Open items', tabs.get(
            'open_intro', '**Open** needs an answer from the client or from us. **Verify** means check it in WMS or '
                          'with Wildix before promising it. Ticks are saved in this browser only.')))
    return '\n'.join(out), index


# ---------------------------------------------------------------- presets

def default_presets(spec):
    """Generic scenarios for every flow: in hours, after hours, nobody answers, everyone busy, queue full, outage."""
    hours = next((t['hours'] for t in spec['timetables'].values() if t.get('kind') == 'weekly' and t.get('hours')), {})
    din, tin = find_moment(hours, True)
    dout, tout = find_moment(hours, False)
    people = list(spec['people'])
    picks = {f'pick:{nid}': False for f in spec['flows'] for nid, n in f['nodes'].items() if n['type'] in ('ring', 'queue')}
    base = {'dow': din, 'time': tin}
    if hours:
        ps = [{'label': f'{DAY_NAMES[din]} {tin}', 'hint': 'In hours, everyone free', 'state': dict(base)},
              {'label': f'{DAY_NAMES[dout]} {tout}', 'hint': 'Out of hours', 'state': {'dow': dout, 'time': tout}}]
    else:
        ps = [{'label': 'Everyone free', 'state': dict(base)}]
    if picks:
        ps.append({'label': 'Nobody picks up', 'state': dict(base, **picks)})
    if people:
        ps.append({'label': 'Everyone on a call', 'state': dict(base, **picks, **{f'm:{p}': 'busy' for p in people})})
    full = {f'q:{nid}': n['max_len'] for f in spec['flows'] for nid, n in f['nodes'].items()
            if n['type'] == 'queue' and n.get('max_len')}
    if full:
        ps.append({'label': 'Queue full', 'state': dict(base, **picks, **{f'm:{p}': 'busy' for p in people}, **full)})
    if people:
        ps.append({'label': 'Internet down', 'hint': 'Every phone unregistered',
                   'state': dict(base, **{f'm:{p}': 'offline' for p in people})})
    return ps


# ---------------------------------------------------------------- build

def build_spec(plan=None, model=None, full_numbers=False, with_current=False, title=None):
    warnings, report = [], []
    open_auto = []
    spec = {'title': title, 'subtitle': '', 'version': '1', 'flows': [], 'timetables': {}, 'switches': {},
            'people': {}, 'groups': {}, 'flags': {}, 'presets': [], 'uat': [], 'open': []}
    src = (model or {}).get('source', {})
    if plan:
        spec['title'] = title or plan.get('title') or 'Call flow plan'
        spec['subtitle'] = plan.get('subtitle', '')
        spec['version'] = str(plan.get('version', '1'))
        spec['timetables'] = plan_timetables(plan)
        spec['switches'] = plan_switches(plan)
        spec['people'] = {k: dict(v or {}) for k, v in (plan.get('people') or {}).items()}
        spec['groups'] = copy.deepcopy(plan.get('groups') or {})
        spec['flags'] = dict(plan.get('flags') or {})
        spec['people_legend'] = plan.get('people_legend')
        for f in plan['flows']:
            f = copy.deepcopy(f)
            f.setdefault('id', f'f{len(spec["flows"]) + 1}')
            f.setdefault('label', f['id'])
            spec['flows'].append(f)
        spec['uat'] = copy.deepcopy(plan.get('uat') or [])
        spec['open'] = copy.deepcopy(plan.get('open') or [])
    if model is not None and (not plan or with_current):
        flows, people, notes = flows_from_backup(model, full_numbers, prefix='cur-' if plan else '')
        report += notes
        for name, tt in backup_timetables(model).items():
            spec['timetables'].setdefault(name, tt)
        for name, sw in backup_switches(model).items():
            spec['switches'].setdefault(name, sw)
        for name, p in people.items():
            spec['people'].setdefault(name, dict(p, source='backup'))
        spec['flows'] += flows
        used_tt = {n.get('timetable') for f in flows for n in f['nodes'].values()}
        for name in [k for k, v in spec['timetables'].items() if v.get('source') == 'backup' and k not in used_tt]:
            del spec['timetables'][name]
        used_sw = {n.get('switch') for f in flows for n in f['nodes'].values() if n['type'] == 'switch'}
        for name in [k for k, v in spec['switches'].items() if v.get('source') == 'backup' and k not in used_sw]:
            del spec['switches'][name]
        if not plan:
            host = src.get('hostname') or 'PBX'
            spec['title'] = title or f'{host}: inbound call flows'
            spec['subtitle'] = (f"Current configuration from the WMS backup of {src.get('backup_time', '?')} "
                                f"(serial {src.get('serial', '?')}). Pick a called number, set up a call and see where it goes.")
        if not flows:
            warnings.append('no inbound routes found: no trunk points at a dialplan with rules')
    if not spec['flows']:
        raise SpecError('nothing to simulate: no flows in the plan and no inbound routes in the backup')

    objects = resolve(spec, model, warnings, open_auto) if plan and model is not None else []
    if open_auto:
        spec['open'].append({'g': 'From the PBX backup check', 'i': open_auto})

    presets = list(copy.deepcopy((plan or {}).get('presets') or []))
    if not plan or plan.get('default_presets', True):
        presets = default_presets(spec) + presets
    for p in presets:
        p['state'] = {k: (str(v) if k == 'dow' else v) for k, v in (p.get('state') or {}).items()}
    spec['presets'] = presets
    validate(spec)

    for f in spec['flows']:
        f['geo'] = layout(f, spec)
        overlaps = check_overlaps(f['geo'])
        if overlaps:
            warnings.append(f"flow {f['id']}: overlapping nodes {overlaps}")
    spec['storage_key'] = f"{slugify(spec['title'])}-v{spec['version']}"
    spec['footer'] = (f"Generated by {GENERATOR}" + (f" from backup {src.get('file')}" if src.get('file') else '')
                      + (f", plan version {spec['version']}" if plan else '') + '. Simulation of the configured logic, '
                      'not a live test: confirm with test calls.')
    return spec, objects, warnings, report


def check_overlaps(geo):
    """Pairs of overlapping nodes, plus ('edge', from, to, node) for each edge drawn through a node."""
    boxes = list(geo['nodes'].items())
    bad = []
    for i, (a, A) in enumerate(boxes):
        for b, B in boxes[i + 1:]:
            if A['x'] < B['x'] + B['w'] and B['x'] < A['x'] + A['w'] and A['y'] < B['y'] + B['h'] and B['y'] < A['y'] + A['h']:
                bad.append((a, b))
    for e in geo['edges']:
        pts = [tuple(map(float, p.split())) for p in re.findall(r'[ML]([\d.\-]+ [\d.\-]+)', e['d'])]
        for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
            lo_x, hi_x, lo_y, hi_y = min(x1, x2), max(x1, x2), min(y1, y2), max(y1, y2)
            for nid, B in boxes:
                if nid in (e['from'], e['to']):
                    continue
                if lo_x < B['x'] + B['w'] - 1 and hi_x > B['x'] + 1 and lo_y < B['y'] + B['h'] - 1 and hi_y > B['y'] + 1:
                    bad.append(('edge', e['from'], e['to'], nid))
    return bad


def render(spec, tabs, objects):
    tpl = TEMPLATE.read_text(encoding='utf-8')
    panel_html, index = panels(spec, tabs, objects)
    spec = dict(spec, tabs_index=index)
    data = json.dumps(spec, ensure_ascii=False).replace('</', '<\\/').replace('<!--', '<\\!--')
    return (tpl.replace('__TITLE__', html.escape(spec['title']))
               .replace('__TAB_PANELS__', panel_html)
               .replace('__FLOW_SPEC__', data))


def main(argv=None):
    ap = argparse.ArgumentParser(description='Interactive Wildix call-flow simulator page from a backup and/or a plan.')
    ap.add_argument('--backup', help='WMS system backup (*_wms_b_*.tar.gz) or an extracted folder')
    ap.add_argument('--plan', help='plan JSON (see references/flow-spec.md)')
    ap.add_argument('--output', help='output folder (default: next to the backup or the plan)')
    ap.add_argument('--title', help='page title (default: from the plan, or the PBX hostname)')
    ap.add_argument('--with-current', action='store_true', help='with --plan: also add the backup\'s current inbound flows')
    ap.add_argument('--full-numbers', action='store_true', help='show external numbers in full (default: last 4 digits)')
    ap.add_argument('--spec-only', action='store_true', help='write flow_spec.json and the report, no HTML')
    a = ap.parse_args(argv)
    if not a.backup and not a.plan:
        ap.error('give --backup, --plan or both')

    try:
        plan = load_plan(a.plan) if a.plan else None
        model = P.load_model(P.Backup(Path(a.backup))) if a.backup else None
        spec, objects, warnings, report = build_spec(plan, model, a.full_numbers, a.with_current, a.title)
    except SpecError as e:
        print(f'ERROR:\n{e}', file=sys.stderr)
        return 2

    if a.output:
        out = Path(a.output)
    elif a.backup:
        b = Path(a.backup)
        out = b.parent / (b.name[:-7] if b.name.endswith('.tar.gz') else b.name + '-out')
        out = out.parent / (out.name + '-callflow')
    else:
        out = Path(a.plan).resolve().parent
    out.mkdir(parents=True, exist_ok=True)
    (out / 'flow_spec.json').write_text(json.dumps(dict(spec, objects=objects), indent=1, ensure_ascii=False),
                                        encoding='utf-8')
    page = out / f"{slugify(spec['title'])}-call-flow.html"
    if not a.spec_only:
        page.write_text(render(spec, (plan or {}).get('tabs') or {}, objects), encoding='utf-8')

    print(f"Title: {spec['title']}")
    print(f"Flows ({len(spec['flows'])}):")
    for f in spec['flows']:
        types = {}
        for n in f['nodes'].values():
            types[n['type']] = types.get(n['type'], 0) + 1
        print(f"  {f['id']}: {f['label']}  [{len(f['nodes'])} nodes: "
              + ', '.join(f'{k} {v}' for k, v in sorted(types.items())) + ']')
    print(f"Presets: {', '.join(p['label'] for p in spec['presets'])}")
    if objects:
        print('Objects:')
        for o in objects:
            print(f"  {STATUS_CHIP[o['status']][1]:<17} {o['type']}: {o['name']}  {o.get('detail') or ''}")
    n_open = sum(1 for g in spec['open'] for it in g['i'] if isinstance(it, list) and it[0] == 'o')
    n_ver = sum(1 for g in spec['open'] for it in g['i'] if isinstance(it, list) and it[0] == 'v')
    if spec['open']:
        print(f'Open items: {n_open} open, {n_ver} verify')
    for w in report:
        print(f'NOTE: {w}')
    for w in warnings:
        print(f'WARNING: {w}')
    print(f"Wrote {out / 'flow_spec.json'}" + ('' if a.spec_only else f'\nWrote {page}'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
