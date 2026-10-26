"""Execute the Actions diagnostics and integrity gates against disposable data."""
from contextlib import closing, redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch


WORKFLOW = Path(__file__).resolve().parents[1] / '.github/workflows/update-global-metadata.yml'


def workflow_bash():
    """Resolve native Bash explicitly; Windows' PATH may prefer the WSL launcher."""
    if sys.platform == 'win32':
        roots = []
        git = shutil.which('git')
        if git:
            # Git for Windows exposes git.exe in cmd, bin or mingw64/bin.
            roots.extend(Path(git).resolve().parents[:3])
        for variable in ('ProgramW6432', 'ProgramFiles', 'ProgramFiles(x86)'):
            if os.environ.get(variable):
                roots.append(Path(os.environ[variable]) / 'Git')
        for root in roots:
            for relative in ('bin/bash.exe', 'usr/bin/bash.exe'):
                candidate = root / relative
                if candidate.is_file():
                    return str(candidate)
        raise FileNotFoundError('Git Bash is required to validate workflow syntax on Windows; '
                                'install Git for Windows (WSL bash.exe is not used).')
    bash = shutil.which('bash')
    if not bash:
        raise FileNotFoundError('Bash is required to validate workflow syntax.')
    return bash


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

    def test_review_selection_counters_are_visible_in_acquisition_summary(self):
        self.report['steam']['reviews'].update(eligible=120, cooldown_excluded=3,
                                               min_age_excluded=10, individual_errors=1,
                                               updated=99, unused_capacity=0)
        self.write_report()
        result = self.run_python_step('Report acquisition health')
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = self.summary.read_text()
        self.assertIn('Reviews eligible | Excluded by cooldown', summary)
        self.assertIn('| 120 | 3 | 10 | 1 | 99 | 0 |', summary)

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
        with closing(sqlite3.connect(self.data / 'global_metadata.db')) as connection:
            connection.executescript('''
                CREATE TABLE parent (id INTEGER PRIMARY KEY);
                CREATE TABLE child (id INTEGER REFERENCES parent(id));
                INSERT INTO child VALUES (99);
            ''')
        # Keep the reference alive: transaction exit alone would pass on Linux
        # but leave the database open and block TemporaryDirectory on Windows.
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute('SELECT 1')
        result = self.run_python_step('Checkpoint and verify SQLite database')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('foreign keys', result.stderr)

        # Execute the actual gate with retained connections, so Linux also
        # detects leaks on rejection, success and a PRAGMA exception.
        block = dict(run_blocks())['Checkpoint and verify SQLite database']
        code = block.split("python - <<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
        connect = sqlite3.connect
        connections = []

        class FailingConnection(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                if sql == 'PRAGMA integrity_check':
                    raise sqlite3.OperationalError('injected integrity query failure')
                return super().execute(sql, *args, **kwargs)

        for outcome in ('corrupt', 'healthy', 'query_error'):
            with self.subTest(checkpoint=outcome):
                if outcome == 'healthy':
                    with closing(connect(self.data / 'global_metadata.db')) as repair:
                        repair.execute('DELETE FROM child')
                        repair.commit()

                def tracked_connect(path):
                    self.assertEqual(path, Path('global_metadata/data/global_metadata.db'))
                    conn = connect(self.data / 'global_metadata.db',
                                   factory=FailingConnection if outcome == 'query_error' else sqlite3.Connection)
                    connections.append(conn)
                    return conn

                with patch.object(sqlite3, 'connect', side_effect=tracked_connect), redirect_stdout(io.StringIO()):
                    if outcome == 'corrupt':
                        with self.assertRaisesRegex(SystemExit, 'foreign keys'):
                            exec(compile(code, '<workflow checkpoint>', 'exec'), {})
                    elif outcome == 'query_error':
                        with self.assertRaisesRegex(sqlite3.OperationalError, 'injected integrity query failure'):
                            exec(compile(code, '<workflow checkpoint>', 'exec'), {})
                    else:
                        exec(compile(code, '<workflow checkpoint>', 'exec'), {})
                self.assertEqual(len(connections), 1)
                with self.assertRaises(sqlite3.ProgrammingError):
                    connections.pop().execute('SELECT 1')

    def test_workflow_shells_parse_and_scheduled_capacity_is_unchanged(self):
        bash = workflow_bash()
        for name, script in run_blocks():
            with self.subTest(step=name):
                # Binary stdin preserves LF on Windows instead of translating
                # it to CRLF before Git Bash parses the workflow script.
                result = subprocess.run([bash, '--noprofile', '--norc', '-n'], input=script.encode('utf-8'),
                                        capture_output=True)
                self.assertEqual(result.returncode, 0,
                                 f'{bash}: stdout={result.stdout!r}; stderr={result.stderr!r}')
        invalid = subprocess.run([bash, '--noprofile', '--norc', '-n'],
                                 input=b'if then\n', capture_output=True)
        self.assertNotEqual(invalid.returncode, 0, f'{bash} accepted invalid Bash syntax')

        # Reproduce a Windows PATH with WSL first, independently of host OS.
        git_root = self.root / 'Program Files' / 'Git'
        git_bash = git_root / 'bin' / 'bash.exe'
        git_bash.parent.mkdir(parents=True)
        git_bash.touch()
        wsl_bash = self.root / 'Windows' / 'System32' / 'bash.exe'
        wsl_bash.parent.mkdir(parents=True)
        wsl_bash.touch()
        for directory in ('cmd', 'bin', 'mingw64/bin'):
            with self.subTest(git_directory=directory):
                git = git_root / directory / 'git.exe'
                git.parent.mkdir(parents=True, exist_ok=True)
                git.touch()
                with patch.object(sys, 'platform', 'win32'), \
                     patch.object(shutil, 'which', side_effect=lambda name: str(git) if name == 'git' else str(wsl_bash)):
                    # resolve() can expand Windows 8.3 names from the temp path.
                    self.assertTrue(git_bash.samefile(workflow_bash()))
        with patch.object(sys, 'platform', 'win32'), patch.object(shutil, 'which', return_value=None), \
             patch.dict(os.environ, {'ProgramFiles': str(self.root / 'Program Files')}, clear=True):
            self.assertTrue(git_bash.samefile(workflow_bash()))
        with patch.object(sys, 'platform', 'win32'), patch.object(shutil, 'which', return_value=str(wsl_bash)), \
             patch.dict(os.environ, {}, clear=True), self.assertRaises(FileNotFoundError):
            workflow_bash()
        workflow = WORKFLOW.read_text()
        for setting in ('STEAM_LIMIT=2500', 'STEAM_REVIEWS_LIMIT=2500',
                        '--steam-reviews-pause 2', 'HLTB_LIMIT=1300', '--hltb-pause 1.9',
                        'timeout-minutes: 300', 'cron: "12 4,16 * * *"'):
            self.assertIn(setting, workflow)
