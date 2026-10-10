"""Tests for wildix_config_parser.py and lua51.py against a synthetic backup.

Run from the repo root:  python -m unittest discover -s skills/wildix-config-review/tests
"""
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / 'scripts'
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(HERE))

import lua51  # noqa: E402
import make_fixture  # noqa: E402
import wildix_config_parser as P  # noqa: E402


class FixtureCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.backup = Path(cls.tmp.name) / 'fixture_2026.01.02_03.04.05_7.08.1_abcdef012345_wms_b_test.tar.gz'
        make_fixture.build(cls.backup)
        cls.model = P.load_model(P.Backup(cls.backup))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()


class TestLua(unittest.TestCase):
    def test_roundtrip(self):
        data = make_fixture.lua_chunk({'features': {'A': {'status': '1', 'value': 'x'}, 'B': 'y'}})
        res = lua51.decode(data)
        self.assertTrue(res['ok'], res['error'])
        self.assertEqual(res['globals'], {'features': {'A': {'status': '1', 'value': 'x'}, 'B': 'y'}})

    def test_garbage_falls_back(self):
        res = lua51.decode(b'not lua at all')
        self.assertFalse(res['ok'])


class TestPatterns(unittest.TestCase):
    def test_matching(self):
        self.assertTrue(P.matches('NXXXXXXXXX', '7805551234'))
        self.assertFalse(P.matches('NXXXXXXXXX', '1805551234'))
        self.assertTrue(P.matches('+1XXXXXXXXXX', '+17805551234'))
        self.assertTrue(P.matches('X.', '12'))
        self.assertFalse(P.matches('X.', '1'))
        self.assertTrue(P.matches('9!', '9'))
        self.assertTrue(P.matches('_[2-4]XX', '345'))
        self.assertFalse(P.matches('[2-4]XX', '545'))
        self.assertTrue(P.matches('ivr', 'ivr'))
        self.assertFalse(P.matches('ivr', 'ivr2'))

    def test_specificity(self):
        nums = ['X.', '1XXXXXXXXXX', '17805551234', 'NXXXXXXXXX', '[13]XXXXXXXXXX']
        self.assertEqual(sorted(nums, key=P.specificity)[0], '17805551234')
        self.assertLess(P.specificity('1XXXXXXXXXX'), P.specificity('X.'))
        self.assertLess(P.specificity('[13]XXXXXXXXXX'), P.specificity('XXXXXXXXXXX'))

    def test_split_params(self):
        self.assertEqual(P.split_params('"a","b\\,c",""'), ['a', 'b,c', ''])


class TestModel(FixtureCase):
    def test_source_and_system(self):
        self.assertEqual(self.model['source']['version'], '7.08.1')
        self.assertEqual(self.model['system']['timezone'], 'America/Edmonton')

    def test_park(self):
        park = self.model['park']
        self.assertEqual(park['timeout_ms'], 45000)
        self.assertEqual([o['number'] for o in park['orbits']], ['901', '902'])
        cb = {(c['dialplan'], c['number']) for c in park['callback_rules']}
        self.assertIn(('users', '901'), cb)
        self.assertIn(('park return', '9001'), cb)
        self.assertIn({'user': '201', 'name': 'Front Desk', 'orbit': '901', 'label': 'Park 1'}, park['park_keys'])

    def test_users_and_features(self):
        users = {u['telephoneNumber']: u for u in self.model['users']}
        self.assertEqual(users['202']['cn'], 'Back Office With A Name Long Enough To Be Folded')
        ext = P.active_features(users['201'], 'external')
        self.assertEqual(ext.get('UCF'), 'VOICEMAIL')
        self.assertEqual(ext.get('TOUT'), '25')
        self.assertEqual(users['202']['features']['function_keys'][0]['type'], 'EXT')

    def test_queues_trunks(self):
        q = self.model['queues']['1']
        self.assertEqual([m['member'] for m in q['members']], ['201', '202'])
        trunks = {t['name']: t for t in self.model['trunks']}
        self.assertEqual(trunks['Example Trunk']['inbound_dialplan'], 'inbound')
        self.assertIn('managed', trunks)
        self.assertEqual(trunks['managed']['codecs'], 'alaw,ulaw')

    def test_warnings(self):
        w = '\n'.join(self.model['warnings'])
        self.assertIn("park-orbit key for '999'", w)
        self.assertIn('park orbit 902 has no dialplan rule', w)
        self.assertIn('[park return] 9001: ends with Goto back to itself', w)
        self.assertIn("call group 2 'Empty' has no members", w)
        self.assertIn("dialplan 'unused' is not used", w)
        self.assertIn("trunk 'managed' inbound dialplan 'empty' is empty", w)

    def test_redaction(self):
        dump = json.dumps(self.model) + P.render_summary(self.model)
        for s in make_fixture.FAKE_SECRETS:
            self.assertNotIn(s, dump, s)
        self.assertIn('[REDACTED]', dump)


class TestTrace(FixtureCase):
    def test_inbound_trace(self):
        out = P.trace(self.model, '+15550100', 'trunk:Example Trunk')
        self.assertIn("[inbound] matches +15550100 with rule '+15550100'", out)
        self.assertIn('rings 201 (Front Desk) for 20 s', out)
        self.assertIn('forward-if-no-answer is ON (VOICEMAIL, after 25 s)', out)
        self.assertIn("call group 1 'Reception'", out)
        self.assertIn('call ends here', out)

    def test_pattern_goto_and_loop(self):
        out = P.trace(self.model, '+15551234567', 'trunk:Example Trunk')
        self.assertIn("rule '+1555XXXXXXX'", out)
        out = P.trace(self.model, '901', 'user:202')
        self.assertIn("[park return] matches 9001", out)
        self.assertIn('LOOP: back to 9001@[park return]', out)

    def test_switch_and_other_jumps(self):
        out = P.trace(self.model, '+15550200', 'trunk:Example Trunk')
        self.assertIn('IF the call type is blacklist:', out)
        self.assertIn("IF switch 'Night Mode' (2-state, on in the backup) is on:", out)
        self.assertIn("IF switch 'Office Mode' (3-state, blinking in the backup) is on:", out)
        self.assertIn("IF switch 'Office Mode' (3-state, blinking in the backup) is blinking:", out)
        self.assertIn("[inbound] matches +15550100", out)           # the blinking branch is followed
        self.assertIn('IF the last dial ended BUSY:', out)
        self.assertIn("Goto built-in 'pbxinternal'", out)
        summary = P.render_summary(self.model)
        self.assertIn("switch 2 'Office Mode' 3-state, blinking in the backup", summary)
        self.assertNotIn('pbxinternal', ' '.join(self.model['warnings']))

    def test_receptionist_is_the_park_return(self):
        w = ' '.join(self.model['warnings'])
        self.assertNotIn("dialplan 'receptionist' is not used", w)
        self.assertEqual(self.model['park']['return_route']['number'], 's')
        self.assertIn('probably off (country code 1', self.model['park']['return_from_transfer'])
        out = P.trace(self.model, '902', 'user:201')   # no rule for 902: built-in routing to the orbit
        self.assertIn('the PBX sends it to [receptionist] default (s)', out)
        self.assertIn('[receptionist] matches s', out)
        summary = P.render_summary(self.model)
        self.assertIn('  default (s)', summary)
        self.assertIn('-> [receptionist] default (s): Goto(9001', summary)
        m = json.loads(json.dumps(self.model))
        m['dialplans']['receptionist']['numbers'] = {}
        self.assertIn("dialplan 'receptionist' has no 'default' number", ' '.join(P.find_warnings(m)))
        md = P.render_mermaid(self.model)
        self.assertIn('no answer / busy, no forward', md)
        self.assertIn('[receptionist] default (s)', md)

    def test_builtin_user(self):
        out = P.trace(self.model, '202', 'user:201')
        self.assertIn('built-in internal routing rings user', out)

    def test_park_trace_shows_parker_forwards(self):
        out = P.trace(self.model, '902', 'user:201')
        self.assertIn('park orbit: caller hears music on hold for the park timeout (45000 ms)', out)
        self.assertIn('parker 201 (Front Desk) internal features', out)


class TestMermaid(FixtureCase):
    def test_render(self):
        md = P.render_mermaid(self.model)
        self.assertEqual(md.count('```mermaid'), 3)  # inbound, park, outbound (unused dialplans are empty)
        self.assertIn('Trunk: Example Trunk', md)
        self.assertIn('DID +15550100', md)
        self.assertIn('pattern +1555XXXXXXX', md)
        self.assertIn('fwd-noanswer: VOICEMAIL', md)
        self.assertIn('GPARK_TIMEOUT = 45 s', md)
        self.assertIn("Switch 'Office Mode'", md)
        self.assertIn('|"blinking"|', md)
        for s in make_fixture.FAKE_SECRETS:
            self.assertNotIn(s, md)
        # every edge endpoint is a declared node
        nodes = set(re.findall(r'^\s+([iopu]\d+)[\[\(\{]', md, re.M))
        for a, b in re.findall(r'^\s+([iopu]\d+) -\.?->(?:\|"[^"]*"\|)? ([iopu]\d+)$', md, re.M):
            self.assertIn(a, nodes)
            self.assertIn(b, nodes)

    def test_label_escaping(self):
        self.assertEqual(P.mtext('a "b" <c> | #'), 'a #quot;b#quot; #lt;c#gt; #124; #35;')


class TestMatch(FixtureCase):
    def run_cli(self, *extra):
        return subprocess.run([sys.executable, str(SCRIPTS / 'wildix_config_parser.py'), str(self.backup),
                               '--output', str(Path(self.tmp.name) / 'm'), *extra],
                              capture_output=True, text=True, encoding='utf-8')

    def test_identity_match(self):
        at = make_fixture.build_autotrace(Path(self.tmp.name) / 'autotrace_logs-1.tar.gz')
        tid = P.autotrace_identity(at)
        self.assertEqual((tid['serial'], tid['domain']), ('abcdef012345', 'fixture.wildixin.com'))
        self.assertEqual(P.check_match(self.model, tid)['status'], 'match')
        r = self.run_cli('--autotrace', str(at))
        self.assertEqual(r.returncode, 0, r.stderr)
        summary = (Path(self.tmp.name) / 'm' / 'config_summary.txt').read_text(encoding='utf-8')
        self.assertIn('PBX match   : MATCH', summary)

    def test_identity_mismatch_stops(self):
        at = make_fixture.build_autotrace(Path(self.tmp.name) / 'autotrace_logs-2.tar.gz',
                                          serial='999999999999', domain='other')
        r = self.run_cli('--autotrace', str(at))
        self.assertEqual(r.returncode, 3)
        self.assertIn('different PBX', r.stderr)
        r = self.run_cli('--autotrace', str(at), '--allow-mismatch')
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_identity_from_analyzer_output(self):
        d = Path(self.tmp.name) / 'analyzer_out'
        d.mkdir(exist_ok=True)
        (d / 'pbx_identity.json').write_text(json.dumps({'serial': 'abcdef012345', 'domain': None}))
        self.assertEqual(P.check_match(self.model, P.autotrace_identity(d))['status'], 'match')

    def test_cli(self):
        out_dir = Path(self.tmp.name) / 'out'
        r = subprocess.run([sys.executable, str(SCRIPTS / 'wildix_config_parser.py'), str(self.backup),
                            '--output', str(out_dir), '--focus', 'park'],
                           capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('Park timeout: 45000 ms (45 s)', r.stdout)
        self.assertTrue((out_dir / 'config_summary.txt').is_file())
        self.assertTrue((out_dir / 'config.json').is_file())


if __name__ == '__main__':
    unittest.main()
