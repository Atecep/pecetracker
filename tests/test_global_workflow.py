"""Execute the Actions diagnostics and integrity gates against disposable data."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest


WORKFLOW = Path(__file__).resolve().parents[1] / '.github/workflows/update-global-metadata.yml'


def run_blocks():
    name = None
    lines = WORKFLOW.read_text().splitlines()
    for index, line in enumerate(lines):
        if line.startswith('      - name: '):
            name = line.split(': ', 1)[1]
        if line == '        run: |':
            block = []
            for following in lines[index + 1:]:
                if following.strip() and not following.startswith('          '):
                    break
                block.append(following)
            yield name, textwrap.dedent('\n'.join(block))


class GlobalWorkflowTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.data = self.root / 'global_metadata/data'
        self.data.mkdir(parents=True)
        self.summary = self.root / 'summary.md'
        self.report = {'status': 'partial', 'publishable': True, 'hltb': None,
                       'steam': {name: dict(selected=2500, attempted=2500, completed=2100,
                                           errors=400, pending=400, updated=2100)
                                 for name in ('details', 'reviews')}}
        self.write_report()

    def write_report(self):
        (self.data / 'run-summary.json').write_text(json.dumps(self.report))

    def run_python_step(self, name, **environment):
        block = dict(run_blocks())[name]
        code = block.split("python - <<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
        return subprocess.run([sys.executable, '-B', '-c', code], cwd=self.root,
                              env={**os.environ, 'GITHUB_STEP_SUMMARY': str(self.summary),
                                   'GITHUB_SHA': 'fixture', 'ACQUISITION_OUTCOME': 'success',
                                   **environment}, text=True, capture_output=True)

    def test_partial_acquisition_reports_counts_and_warns_without_failing(self):
        result = self.run_python_step('Report acquisition health')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('::warning::', result.stdout)
        self.assertIn('| details | 2500 | 2500 | 2100 | 400 | 400 | 2100 |', self.summary.read_text())
        self.assertIn('published only after verified asset promotion', self.summary.read_text())

    def test_critical_acquisition_error_blocks_publication(self):
        self.report.update(status='fatal', publishable=False)
        self.write_report()
        result = self.run_python_step('Report acquisition health')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('publication is blocked', result.stderr)

    def test_missing_successful_acquisition_report_blocks_publication(self):
        (self.data / 'run-summary.json').unlink()
        self.assertNotEqual(self.run_python_step('Report acquisition health').returncode, 0)

    def test_publication_counts_require_successful_promotion_for_each_asset(self):
        for raw, compressed in (('success', 'success'), ('success', 'failure'),
                                ('failure', 'skipped'), ('skipped', 'skipped')):
            with self.subTest(raw=raw, compressed=compressed):
                result = self.run_python_step('Report publication outcome',
                                              RAW_OUTCOME=raw, COMPRESSED_OUTCOME=compressed)
                self.assertEqual(result.returncode, 0, result.stderr)
                publication = json.loads((self.data / 'publication-summary.json').read_text())
                for asset, outcome in (('raw', raw), ('compressed', compressed)):
                    self.assertEqual(publication[asset]['outcome'], outcome)
                    self.assertEqual(publication[asset]['published_updates'],
                                     dict(details=2100, reviews=2100) if outcome == 'success'
                                     else dict(details=0, reviews=0))

    def test_checkpoint_rejects_foreign_key_corruption(self):
        with sqlite3.connect(self.data / 'global_metadata.db') as connection:
            connection.executescript('''
                CREATE TABLE parent (id INTEGER PRIMARY KEY);
                CREATE TABLE child (id INTEGER REFERENCES parent(id));
                INSERT INTO child VALUES (99);
            ''')
        result = self.run_python_step('Checkpoint and verify SQLite database')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('foreign keys', result.stderr)

    def test_workflow_shells_parse_and_scheduled_capacity_is_unchanged(self):
        for name, script in run_blocks():
            with self.subTest(step=name):
                result = subprocess.run(['bash', '-n'], input=script, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
        workflow = WORKFLOW.read_text()
        for setting in ('STEAM_LIMIT=2500', 'STEAM_REVIEWS_LIMIT=2500',
                        '--steam-reviews-pause 2', 'HLTB_LIMIT=1300', '--hltb-pause 1.9',
                        'timeout-minutes: 300', 'cron: "12 4,16 * * *"'):
            self.assertIn(setting, workflow)
