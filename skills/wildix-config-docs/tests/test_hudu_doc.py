"""Tests for wildix_hudu_doc.py against the wildix-config-review synthetic backup.

Run from the repo root:  python -m unittest discover -s skills/wildix-config-docs/tests
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
REVIEW = HERE.parent.parent / 'wildix-config-review'
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REVIEW / 'tests'))

import make_fixture  # noqa: E402
import wildix_hudu_doc as D  # noqa: E402

P = D.P


class DocCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.backup = Path(cls.tmp.name) / 'fixture_2026.01.02_03.04.05_7.08.1_abcdef012345_wms_b_test.tar.gz'
        make_fixture.build(cls.backup)
        cls.model = P.load_model(P.Backup(cls.backup))
        cls.doc, cls.meta = D.build_article(cls.model, 'Fixture Co', '2026-10-02')

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()


class TestArticle(DocCase):
    def test_sections_and_title(self):
        self.assertEqual(self.meta['title'], 'Configuration - Wildix - PBX Configuration (fixture)')
        ids = re.findall(r'<details class="mce-accordion" id="([^"]+)"', self.doc)
        self.assertEqual(ids, [s for s, _ in D.SECTIONS])
        self.assertIn('id="wx-overview" open="open"', self.doc)
        self.assertEqual(self.doc.count('<details'), self.doc.count('</details>'))

    def test_content(self):
        self.assertIn('<td><strong>Serial</strong></td><td>abcdef012345</td>', self.doc)
        self.assertIn('Ring 201 (Front Desk) 20 s (if unanswered, forwards to VOICEMAIL', self.doc)
        self.assertIn('1. DialPhone(201, timeout_s=20)  -&gt; 201 (Front Desk)', self.doc)
        self.assertIn("call group 'Reception'", self.doc)
        self.assertIn('<strong>no members</strong>', self.doc)
        self.assertIn('<td><strong>Park timeout</strong></td><td>45 s</td>', self.doc)
        self.assertIn("<li>dialplan 'unused' is not used", self.doc)
        self.assertEqual(self.doc.count('<pre class="language-mermaid"><code class="language-mermaid">'), 3)  # inbound, park, outbound

    def test_switch_paths(self):
        path = D.describe_path(self.model, 'inbound', '+15550200')
        self.assertIn("if switch 'Night Mode' is on: Ring 202", path)
        self.assertIn("if switch 'Office Mode' is blinking:", path)
        self.assertIn('<td>Office Mode</td><td>3-state</td><td>blinking</td>', self.doc)

    def test_mermaid_is_escaped_text(self):
        for block in re.findall(r'<code class="language-mermaid">(.*?)</code></pre>', self.doc, re.S):
            self.assertTrue(block.startswith('flowchart '))
            self.assertNotIn('<', block)  # <br/> and --> must be entities, not markup Hudu would rewrite
            self.assertIn('&lt;br/&gt;', block)

    def test_no_secrets(self):
        for s in make_fixture.FAKE_SECRETS:
            self.assertNotIn(s, self.doc, s)
        self.assertNotIn('[REDACTED]', self.doc)
        self.assertNotIn('fixtureauthuser', self.doc)

    def test_deterministic(self):
        again, _ = D.build_article(P.load_model(P.Backup(self.backup)), 'Fixture Co', '2026-10-02')
        self.assertEqual(again, self.doc)

    def test_summary(self):
        self.assertIn(D.SUMMARY_PLACEHOLDER, self.doc)
        doc, meta = D.build_article(self.model, 'Fixture Co', '2026-10-02', summary='One <b>&\n\nTwo')
        self.assertIn('<div id="wx-summary"><p>One &lt;b&gt;&amp;</p><p>Two</p></div>', doc)
        self.assertTrue(meta['summary_written'])


class TestPrevious(DocCase):
    def edited_previous(self):
        """The first article as Hudu would hand it back after a technician added notes."""
        notes = ('<p>Carrier account 12345. Call Jo before changing hours.</p>'
                 '<details class="mce-accordion"><summary>Nested</summary><p>inner</p></details>')
        span = D.find_section(self.doc, 'wx-notes', 'Technician notes')
        old = self.doc[span[0]:span[1]]
        new = '<details class="mce-accordion" id="wx-notes"><summary>Technician notes</summary>\n' + notes + \
              '\n</details>'
        # Hudu's editor puts table tags on their own lines (seen on a real round trip)
        doc = re.sub(r'(</?(?:table|thead|tbody|tr|th|td)\b[^>]*>)(?=<)', r'\1\n', self.doc.replace(old, new))
        return doc, notes

    def test_notes_and_changelog_kept(self):
        prev, notes = self.edited_previous()
        doc, meta = D.build_article(self.model, 'Fixture Co', '2026-11-01', previous=prev,
                                    change='Added user 203')
        self.assertIn(notes, doc)
        self.assertTrue(meta['previous']['notes_kept'])
        self.assertEqual(meta['previous']['changelog_rows_kept'], 1)
        rows = re.findall(r'<tr>\s*<td>(\d{4}-\d\d-\d\d)</td>\s*<td>[^<]*</td>\s*<td>([^<]*)</td>\s*</tr>', doc)
        self.assertEqual(rows, [('2026-11-01', 'Added user 203'), ('2026-10-02', 'Initial documentation.')])
        self.assertTrue(meta['previous']['serial_matches'])
        self.assertFalse(meta['previous']['older_backup'])
        self.assertEqual(meta['previous']['backup_time'], '2026-01-02 03:04:05')

    def test_important_notes(self):
        self.assertTrue(self.doc.startswith('<div id="wx-important"><h3>Important notes</h3>'))
        self.assertLess(self.doc.index('wx-important'), self.doc.index('<details'))
        prev, _ = self.edited_previous()
        mine = ('<div id="wx-important"><h3>Important notes</h3>\n<p><strong>Do not reboot 8-5.</strong></p>'
                '<div><p>nested div</p></div>\n</div>')
        span = D.find_important(prev)
        prev = prev[:span[0]] + mine + prev[span[1]:]
        doc, meta = D.build_article(self.model, 'Fixture Co', '2026-11-01', previous=prev)
        self.assertTrue(doc.startswith(mine))
        self.assertTrue(meta['previous']['important_kept'])
        # editor dropped the id: found by its heading, id restored
        doc, _ = D.build_article(self.model, 'Fixture Co', '2026-11-01',
                                 previous=prev.replace(' id="wx-important"', ''))
        self.assertTrue(doc.startswith(mine))

    def test_fallback_on_summary_text(self):
        prev, notes = self.edited_previous()
        prev = prev.replace(' id="wx-notes"', '')  # editor dropped the id
        doc, meta = D.build_article(self.model, 'Fixture Co', '2026-11-01', previous=prev)
        self.assertIn(notes, doc)

    def test_after_hudu_editor_save(self):
        """What Hudu's editor does on save (seen 2026-10-02): data-id on everything, ids dropped from the
        accordions, an accordion-body div, header row moved into <tbody> as <th>, cell text wrapped in <p>."""
        prev, notes = self.edited_previous()
        prev = re.sub(r'<details class="mce-accordion" id="[^"]+"', '<details class="mce-accordion"', prev)
        prev = re.sub(r'(<summary)>(.*?)</summary>', r'\1 class="mce-accordion-summary">\2</summary>'
                      r'<div class="mce-accordion-body" data-mce-bogus="1">', prev)
        prev = prev.replace('</details>', '</div></details>')
        prev = re.sub(r'<thead>\s*(<tr>.*?</tr>)\s*</thead>\s*<tbody>', r'<tbody>\1', prev, flags=re.S)
        prev = re.sub(r'<(td|th)>(.*?)</\1>', r'<\1 colspan="1" rowspan="1"><p data-id="x">\2</p></\1>', prev,
                      flags=re.S)
        prev = re.sub(r'<(p|h3|h4|table)>', r'<\1 data-id="x">', prev)
        doc, meta = D.build_article(self.model, 'Fixture Co', '2026-11-01', previous=prev)
        p = meta['previous']
        self.assertEqual((p['serial'], p['hostname'], p['backup_time']),
                         ('abcdef012345', 'fixture', '2026-01-02 03:04:05'))
        self.assertTrue(p['serial_matches'])
        self.assertTrue(p['notes_kept'])
        self.assertIn('Carrier account 12345', doc)
        self.assertEqual(p['changelog_rows_kept'], 1)  # the header row is not a data row

    def test_unreadable_serial_is_not_a_match(self):
        prev, _ = self.edited_previous()
        _, meta = D.build_article(self.model, 'Fixture Co', '2026-11-01',
                                  previous=prev.replace('<strong>Serial</strong>', 'Serial'))
        self.assertIsNone(meta['previous']['serial_matches'])

    def test_other_pbx_flagged(self):
        prev = self.doc.replace('<td>abcdef012345</td>', '<td>999999999999</td>') \
                       .replace('2026-01-02 03:04:05 (PBX local time)', '2027-01-01 00:00:00 (PBX local time)')
        _, meta = D.build_article(self.model, 'Fixture Co', '2026-11-01', previous=prev)
        self.assertFalse(meta['previous']['serial_matches'])
        self.assertTrue(meta['previous']['older_backup'])


class TestCli(DocCase):
    def test_cli(self):
        out = Path(self.tmp.name) / 'out'
        summ = Path(self.tmp.name) / 'summary.txt'
        summ.write_text('Calls ring the front desk first.', encoding='utf-8')
        r = subprocess.run([sys.executable, str(SCRIPTS / 'wildix_hudu_doc.py'), str(self.backup),
                            '--company', 'Fixture Co', '--output', str(out), '--date', '2026-10-02',
                            '--summary-file', str(summ)],
                           capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(r.returncode, 0, r.stderr)
        meta = json.loads((out / 'hudu_meta.json').read_text(encoding='utf-8'))
        self.assertEqual(meta['serial'], 'abcdef012345')
        self.assertIn('Calls ring the front desk first.', (out / 'hudu_article.html').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
