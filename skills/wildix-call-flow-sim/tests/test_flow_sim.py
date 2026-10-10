"""Tests for wildix_flow_sim.py against the wildix-config-review synthetic backup and the example plan.

Run from the repo root:  python -m unittest discover -s skills/wildix-call-flow-sim/tests
"""
import contextlib
import copy
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
REVIEW = SKILL.parent / 'wildix-config-review'
sys.path.insert(0, str(SKILL / 'scripts'))
sys.path.insert(0, str(REVIEW / 'tests'))

import make_fixture  # noqa: E402
import wildix_flow_sim as F  # noqa: E402

P = F.P
EXAMPLE = SKILL / 'examples' / 'example-dental.plan.json'


def spec_json(html_text):
    m = re.search(r'<script type="application/json" id="flow-spec">(.*?)</script>', html_text, re.S)
    return json.loads(m.group(1).replace('<\\/', '</'))


class Case(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        cls.backup = cls.dir / 'fixture_2026.01.02_03.04.05_7.08.1_abcdef012345_wms_b_test.tar.gz'
        make_fixture.build(cls.backup)
        cls.model = P.load_model(P.Backup(cls.backup))
        cls.example = json.loads(EXAMPLE.read_text(encoding='utf-8'))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()


class TestBackup(Case):
    def setUp(self):
        self.spec, self.objects, self.warnings, _ = F.build_spec(model=self.model)

    def test_one_flow_per_inbound_number(self):
        labels = [f['label'] for f in self.spec['flows']]
        self.assertEqual(len(labels), 4)  # +15550100, +15550200, the +1555XXXXXXX catch-all, then park returns
        self.assertTrue(labels[0].startswith('+15550100 (number, Example Trunk'))
        self.assertIn('pattern', labels[2])
        self.assertEqual(labels[3], 'Parked call returns')

    def test_park_flow_ends_in_receptionist(self):
        f = self.spec['flows'][3]
        n = f['nodes']
        ring = next(x for x in n.values() if x['type'] == 'ring')
        self.assertEqual(ring['members'], ['The parker'])
        fwd = n[ring['go']['no_answer']]
        self.assertEqual(n[fwd['go']['true']]['type'], 'forward')
        self.assertEqual(n[fwd['go']['false']]['title'], '[receptionist] default (s)')

    def test_node_types_follow_the_dialplan(self):
        f = self.spec['flows'][0]
        types = [n['type'] for n in f['nodes'].values()]
        self.assertEqual(types[:3], ['start', 'route', 'ring'])
        ring = next(n for n in f['nodes'].values() if n['type'] == 'ring')
        self.assertEqual(ring['timeout'], 20)
        self.assertEqual(ring['members'], ['201 (Front Desk)'])
        # 201 forwards on no answer (UCF=VOICEMAIL), so the call group after it isn't reached, as in the Mermaid chart
        fwd = f['nodes'][ring['go']['no_answer']]
        self.assertEqual(fwd['type'], 'forward')
        self.assertIn('VOICEMAIL', fwd['sub'][0])

    def test_catch_all_reuses_the_main_route(self):
        f = self.spec['flows'][2]
        route = [n for n in f['nodes'].values() if n['type'] == 'route']
        self.assertEqual([n['title'] for n in route], ['[inbound] +1555XXXXXXX', '[inbound] +15550100'])

    def test_no_secrets_in_page(self):
        page = F.render(self.spec, {}, [])
        for s in make_fixture.FAKE_SECRETS:
            self.assertNotIn(s, page)
        self.assertEqual(spec_json(page)['title'], 'fixture: inbound call flows')

    def test_layout_has_no_overlaps(self):
        for f in self.spec['flows']:
            self.assertEqual(F.check_overlaps(f['geo']), [])

    def test_default_presets(self):
        labels = [p['label'] for p in self.spec['presets']]
        self.assertEqual(labels[0], 'Everyone free')   # no timetable in these flows, so no day/time presets
        self.assertIn('Internet down', labels)


class TestMasking(unittest.TestCase):
    def test_mask(self):
        self.assertEqual(F.mask_number('+15875550123'), '…0123')
        self.assertEqual(F.mask_number('+15875550123', full=True), '+15875550123')
        self.assertEqual(F.mask_number('201'), '201')
        self.assertEqual(F.mask_number('+1${EXTEN}'), '+1${EXTEN}')

    def test_dial_trunk_target_masked(self):
        model = make_fixture_model_with_trunk_dial()
        spec, *_ = F.build_spec(model=model)
        titles = [n['title'] for n in spec['flows'][0]['nodes'].values() if n['type'] == 'dial_external']
        self.assertEqual(titles, ['Dial …9999'])


def make_fixture_model_with_trunk_dial():
    """The fixture model with the main DID's call group replaced by a dial-out to a mobile number."""
    with tempfile.TemporaryDirectory() as d:
        b = Path(d) / 'fixture_2026.01.02_03.04.05_7.08.1_abcdef012345_wms_b_test.tar.gz'
        make_fixture.build(b)
        m = P.load_model(P.Backup(b))
    rules = m['dialplans']['inbound']['numbers']['+15550100']
    rules[:] = [r for r in rules if r['app'] != 'DialPhone']
    rules.insert(0, {'rule_id': 99, 'priority': 0, 'app': 'DialTrunk', 'params': ['+15875559999', 'Example%20Trunk'],
                     'comment': ''})
    return m


class TestSwitchGate(Case):
    def test_switch_rules_become_switch_nodes(self):
        spec, *_ = F.build_spec(model=self.model)
        f = spec['flows'][1]                     # +15550200: call type, 2-state, 3-state and busy gates
        sw = {n['switch']: n for n in f['nodes'].values() if n['type'] == 'switch'}
        self.assertEqual(sorted(sw['Office Mode']['go']), ['blinking', 'off', 'on'])
        self.assertEqual(sorted(sw['Night Mode']['go']), ['off', 'on'])
        self.assertEqual(spec['switches']['Night Mode']['default'], 'on')        # state 1 in the backup
        self.assertEqual(spec['switches']['Office Mode']['default'], 'blinking')  # state 2 in the backup
        conds = sorted(n['title'] for n in f['nodes'].values() if n['type'] == 'condition')
        self.assertEqual(conds, ['Call type blacklist?', 'Last dial BUSY?'])
        self.assertEqual(f['nodes'][sw['Office Mode']['go']['blinking']]['title'], '[inbound] +15550100')

    def test_gotoif_on_switch_db_is_named_after_the_switch(self):
        m = copy.deepcopy(self.model)
        m['switches'] = [{'id': 1, 'switchname': 'Night Mode', 'state': 0, 'swtype': 3},
                         {'id': 10, 'switchname': 'Other', 'state': 0, 'swtype': 0}]
        rules = m['dialplans']['inbound']['numbers']['+15550100']
        rules.insert(0, {'rule_id': 98, 'priority': 0, 'app': 'GotoIf',
                         'params': ['${DB(TIMETABLE/1)}=1', '9001', 'park return', '1', ''], 'comment': ''})
        spec, *_ = F.build_spec(model=m)
        cond = next(n for n in spec['flows'][0]['nodes'].values() if n['type'] == 'condition')
        self.assertEqual(cond['title'], "Switch 'Night Mode'")
        self.assertEqual(spec['timetables'], {})   # switches are dialplan gates, not timetable overrides


class TestPlan(Case):
    def test_example_matches_the_demo(self):
        spec, objects, warnings, _ = F.build_spec(plan=self.example)
        self.assertEqual([p['label'] for p in spec['presets']],
                         ['Tuesday 10:00', 'Both on calls', 'Queue full', 'Monday with laptop', 'Saturday evening',
                          'Staff meeting', 'Winter closure', 'Internet down'])
        self.assertEqual(objects, [])          # no backup: nothing to check against
        self.assertEqual(warnings, [])
        self.assertEqual(spec['timetables']['Business Hours']['hours']['2'], [[480, 960]])
        self.assertEqual(spec['timetables']['Business Hours']['hours']['5'], [[420, 900]])

    def test_example_renders_tabs(self):
        spec, objects, *_ = F.build_spec(plan=self.example)
        page = F.render(spec, self.example['tabs'], objects)
        for tab in ('p-build', 'p-x1', 'p-uat', 'p-open'):
            self.assertIn(f'id="{tab}"', page)
        self.assertIn('<code>Business Hours</code>', page)
        self.assertIn('<span class="chip o">Open: confirm names and extensions</span>', page)
        self.assertEqual([t['label'] for t in spec_json(page)['tabs_index']],
                         ['Build plan', 'Phones, fax, outage', 'UAT checklist', 'Open items'])

    def test_skipped_ring_edge_carries_both_keys(self):
        spec, *_ = F.build_spec(plan=self.example)
        e = next(e for e in spec['flows'][0]['geo']['edges'] if e['from'] == 'ring')
        self.assertEqual(sorted(e['keys']), ['ring>no_answer', 'ring>skipped'])

    def test_validation_errors(self):
        bad = copy.deepcopy(self.example)
        bad['flows'][0]['nodes']['ring']['go']['no_answer'] = 'nowhere'
        bad['flows'][0]['nodes']['meetq']['go']['maybe'] = 'vm'
        bad['flows'][0]['nodes']['openq']['cases'][2]['timetable'] = 'Summer'
        with self.assertRaises(F.SpecError) as cm:
            F.build_spec(plan=bad)
        msg = str(cm.exception)
        self.assertIn("branch 'no_answer' points to missing node 'nowhere'", msg)
        self.assertIn("go key 'maybe' is not a state of switch 'Meeting Mode'", msg)
        self.assertIn("timetable 'Summer' is not defined", msg)

    def test_hours_parsing(self):
        self.assertEqual(F.parse_hours({'fri-mon': '08:00-12:00, 13:00-17:00'})['0'], [[480, 720], [780, 1020]])
        self.assertEqual(sorted(F.parse_hours({'fri-mon': '9:00-17:00'})), ['0', '1', '5', '6'])
        with self.assertRaises(F.SpecError):
            F.parse_hours({'funday': '08:00-12:00'})


class TestPlanWithBackup(Case):
    PLAN = {
        'title': 'Fixture Co main number', 'default_presets': False,
        'timetables': {'Business Hours': {'hours': {'mon-fri': '08:00-17:00'}}},
        'people': {'Front Desk': {}, 'Back': {}, 'Nobody Here': {}},
        'groups': {'Reception': {}, 'New Group': {'members': ['Nobody Here']}},
        'flows': [{'id': 'main', 'label': 'Main', 'start': 's', 'nodes': {
            's': {'type': 'start', 'title': 'Call', 'next': 'tt'},
            'tt': {'type': 'timetable', 'title': 'Open?', 'timetable': 'Business Hours', 'go': {'open': 'q', 'closed': 'vm'}},
            'q': {'type': 'queue', 'title': 'Reception', 'group': 'Reception', 'max_wait': 30, 'go': {'timeout': 'q2'}},
            'q2': {'type': 'queue', 'title': 'New', 'group': 'New Group', 'max_wait': 30, 'go': {'timeout': 'vm'}},
            'vm': {'type': 'voicemail', 'title': 'Voicemail'}}}],
    }

    def test_resolution(self):
        spec, objects, warnings, _ = F.build_spec(plan=copy.deepcopy(self.PLAN), model=self.model)
        st = {(o['type'], o['name']): o['status'] for o in objects}
        self.assertEqual(st[('Timetable', 'Business Hours')], 'to_create')
        self.assertEqual(st[('Call group', 'Reception')], 'existing')
        self.assertEqual(st[('Call group', 'New Group')], 'to_create')
        self.assertEqual(st[('User', 'Front Desk')], 'existing')
        self.assertEqual(st[('User', 'Back')], 'existing')          # first-name match: 'Back Office With A Name...'
        self.assertEqual(st[('User', 'Nobody Here')], 'not_found')
        # Reception's members come from the PBX because the plan didn't list them
        self.assertEqual(spec['groups']['Reception']['members'], ['201 (Front Desk)', '202 (Back Office With A Name Long Enough To Be Folded)'])
        self.assertEqual(spec['people']['Front Desk']['label'], 'Front Desk (201)')
        self.assertTrue(any("'Nobody Here' not found" in w for w in warnings))
        auto = next(g for g in spec['open'] if g['g'] == 'From the PBX backup check')
        self.assertTrue(any('Nobody Here' in i[1] for i in auto['i']))
        page = F.render(spec, {}, objects)
        self.assertIn('<span class="chip c">To create</span>', page)
        self.assertIn('<span class="chip d">In PBX</span>', page)

    def test_with_current_adds_backup_flows(self):
        spec, *_ = F.build_spec(plan=copy.deepcopy(self.PLAN), model=self.model, with_current=True)
        self.assertEqual([f['id'] for f in spec['flows']], ['main', 'cur-b1', 'cur-b2', 'cur-b3', 'cur-park'])
        self.assertTrue(spec['flows'][1]['label'].startswith('Current: '))


class TestCli(Case):
    def test_backup_and_plan(self):
        out = self.dir / 'out'
        with contextlib.redirect_stdout(io.StringIO()):
            rc = F.main(['--backup', str(self.backup), '--plan', str(EXAMPLE), '--output', str(out)])
        self.assertEqual(rc, 0)
        pages = list(out.glob('*-call-flow.html'))
        self.assertEqual([p.name for p in pages], ['example-dental-clinic-main-number-build-plan-call-flow.html'])
        spec = json.loads((out / 'flow_spec.json').read_text(encoding='utf-8'))
        self.assertTrue(any(o['status'] == 'to_create' for o in spec['objects']))

    def test_bad_plan_exit_code(self):
        p = self.dir / 'bad.json'
        p.write_text('{"flows": [{"id": "x", "start": "a", "nodes": {"a": {"type": "teleport", "title": "A"}}}]}')
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(F.main(['--plan', str(p), '--output', str(self.dir / 'bad')]), 2)


if __name__ == '__main__':
    unittest.main()
