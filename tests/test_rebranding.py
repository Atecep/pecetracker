"""Pecetracker identity with stable SLT storage, instances and release assets."""
import hashlib
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import app_paths
import database
import global_bootstrap as bootstrap
import launcher
import protondb_service
import settings
import steam_store
import update_checker
import version

ROOT = Path(__file__).resolve().parents[1]


class IdentityTests(unittest.TestCase):
    def test_identity_and_archive_names_keep_current_version(self):
        self.assertEqual(version.APP_NAME, 'Pecetracker')
        self.assertEqual(version.APP_TAGLINE, 'Your Steam Library, Tamed.')
        self.assertEqual(version.APP_DESCRIPTION, 'A Steam library and backlog manager.')
        self.assertEqual(version.GITHUB_URL, 'https://github.com/Atecep/pecetracker')
        self.assertEqual(version.distribution_archive_name('Linux','x86_64'), f'Pecetracker-v{version.APP_VERSION}-linux-x86_64.tar.xz')
        self.assertEqual(version.distribution_archive_name('Windows','AMD64'), f'Pecetracker-v{version.APP_VERSION}-windows-x86_64.zip')
        self.assertEqual(version.distribution_archive_name('Windows','ARM64'), f'Pecetracker-v{version.APP_VERSION}-windows-aarch64.zip')

    def test_legacy_paths_are_defaults_with_and_without_platform_environment(self):
        with tempfile.TemporaryDirectory() as scratch, patch.object(Path,'home',return_value=Path(scratch)):
            home=Path(scratch)
            for system,env,expected in (
                ('Linux',{'XDG_DATA_HOME':str(home/'xdg')},home/'xdg'/'SteamLibraryTracker'),
                ('Linux',{},home/'.local/share/SteamLibraryTracker'),
                ('Windows',{'LOCALAPPDATA':str(home/'local')},home/'local'/'SteamLibraryTracker'),
                ('Windows',{},home/'AppData/Local/SteamLibraryTracker'),
                ('Darwin',{},home/'Library/Application Support/SteamLibraryTracker'),
            ):
                with self.subTest(system=system,env=env), patch.object(app_paths.platform,'system',return_value=system), patch.dict(os.environ,env,clear=True):
                    self.assertEqual(app_paths.get_app_data_dir(),expected)
                    self.assertFalse(expected.exists())

    def test_network_identity_has_new_name_and_canonical_urls(self):
        self.assertEqual(steam_store.HEADERS['User-Agent'],f'Pecetracker/{version.APP_VERSION}')
        self.assertEqual(protondb_service._request_headers()['User-Agent'],
                         f'Pecetracker/{version.APP_VERSION} (+https://github.com/Atecep/pecetracker)')
        self.assertEqual(bootstrap.GLOBAL_DB_ZST_URL,
                         'https://github.com/Atecep/pecetracker/releases/download/global-db/global_metadata.db.zst')
        self.assertEqual(bootstrap.GLOBAL_DB_ZST_SHA256_URL, bootstrap.GLOBAL_DB_ZST_URL+'.sha256')
        self.assertEqual(bootstrap.GLOBAL_BOOTSTRAP_STATE_KEY,'global_metadata_bootstrap_v1')
        self.assertEqual(bootstrap.GLOBAL_BOOTSTRAP_LAST_ATTEMPT_KEY,'global_metadata_bootstrap_v1_last_attempt')

    def test_specs_build_new_executables_and_keep_review_and_platform_dependencies(self):
        for filename,console in (('Pecetracker.spec',True),('Pecetracker.windows.spec',False)):
            analysis=Mock(return_value=SimpleNamespace(pure=[],scripts=[],binaries=[],datas=[]))
            exe=Mock();collect=Mock()
            with self.subTest(filename=filename), patch('PyInstaller.utils.hooks.collect_data_files',return_value=[]), patch('PyInstaller.utils.hooks.collect_submodules',return_value=[]), patch('PyInstaller.utils.hooks.copy_metadata',return_value=[]):
                runpy.run_path(str(ROOT/filename),init_globals={'Analysis':analysis,'PYZ':Mock(),'EXE':exe,'COLLECT':collect})
            self.assertEqual(exe.call_args.kwargs['name'],'Pecetracker')
            self.assertEqual(collect.call_args.kwargs['name'],'Pecetracker')
            self.assertEqual(exe.call_args.kwargs['console'],console)
            self.assertIn('steam_reviews',analysis.call_args.kwargs['hiddenimports'])
            self.assertIn('protondb_service',analysis.call_args.kwargs['hiddenimports'])
            self.assertIn(('app.py','.'),analysis.call_args.kwargs['datas'])

    def test_workflow_keeps_dynamic_repository_and_existing_publication_contract(self):
        workflow=(ROOT/'.github/workflows/update-global-metadata.yml').read_text()
        self.assertNotIn('Atecep/steam-library-tracker',workflow)
        self.assertIn('${GITHUB_REPOSITORY}',workflow)
        self.assertIn('group: global-metadata-update',workflow)
        self.assertEqual(workflow.count('RELEASE_TAG="global-db"'),2)
        for asset in ('global_metadata.db','global_metadata.db.sha256','global_metadata.db.zst','global_metadata.db.zst.sha256'):
            self.assertIn(asset,workflow)


class UpdateCompatibilityTests(unittest.TestCase):
    def test_new_release_url_and_same_version_semantics(self):
        for tag,expected in (('v1.7.0',True),(f'v{version.APP_VERSION}',False),('v1.5.0',False),('global-db',False)):
            payload={'tag_name':tag,'html_url':version.GITHUB_RELEASES_URL+'/tag/'+tag}
            with self.subTest(tag=tag), patch.object(update_checker,'urlopen',return_value=io.BytesIO(json.dumps(payload).encode())) as fetch:
                result=update_checker.check_for_updates()
            request=fetch.call_args.args[0]
            self.assertEqual(request.full_url,'https://api.github.com/repos/Atecep/pecetracker/releases/latest')
            self.assertEqual(request.get_header('User-agent'),f'Pecetracker/{version.APP_VERSION}')
            self.assertEqual(result is not None,expected)
            if expected:self.assertEqual(result.release_url,payload['html_url'])

    def test_legacy_repository_override_can_receive_canonical_redirected_payload(self):
        payload={'tag_name':'v1.7.0','html_url':version.GITHUB_RELEASES_URL+'/tag/v1.7.0'}
        with patch.object(update_checker,'urlopen',return_value=io.BytesIO(json.dumps(payload).encode())) as fetch:
            result=update_checker.check_for_updates(repository='Atecep/steam-library-tracker',current_version='1.5.0')
        self.assertIn('/Atecep/steam-library-tracker/',fetch.call_args.args[0].full_url)
        self.assertEqual(result.current_version,'1.5.0')
        self.assertEqual(result.latest_version,'1.7.0')
        self.assertEqual(result.release_url,payload['html_url'])

    def test_existing_branded_instance_is_reopened_by_identity_without_renaming_state(self):
        identity={'created_at':1,'executable':'/legacy/SteamLibraryTracker','command':['/legacy/SteamLibraryTracker']}
        with tempfile.TemporaryDirectory() as scratch:
            state=Path(scratch)/'instance.json'
            payload=json.dumps({'pid':12345,'port':8501,'version':'1.5.0','protocol':2,'identity':identity,'instance_token':'unchanged'})
            state.write_text(payload)
            # Both launchers intentionally represent the legacy 1.5.0 build.
            with patch.object(launcher,'APP_VERSION','1.5.0'), patch.object(launcher,'INSTANCE_STATE_PATH',state), patch.object(launcher,'pid_is_running',return_value=True), patch.object(launcher,'process_identity',return_value=identity), patch.object(launcher,'server_is_healthy',return_value=True), patch.object(launcher.webbrowser,'open') as open_browser, patch.object(launcher,'terminate_process') as stop:
                self.assertTrue(launcher.reopen_existing_instance())
            stop.assert_not_called();open_browser.assert_called_once_with('http://localhost:8501')
            self.assertEqual(state.read_text(),payload)


class LegacyStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.data=self.root/'SteamLibraryTracker';self.data.mkdir()
        for p in (patch.object(database,'DATABASE_PATH',self.data/'library.db'),patch.object(database,'ensure_data_dir'),patch.object(settings,'CONFIG_PATH',self.data/'config.json'),patch.object(settings,'ensure_data_dir')):
            p.start();self.addCleanup(p.stop)
        database.init_database()

    def test_new_process_reads_legacy_personal_data_and_state_without_modifying_files(self):
        database.save_game_data(620,'Playing','personal note fixture')
        database.save_hltb_metadata(620,'Portal 2','matched',hltb_id=42,match_confidence='manual')
        database.set_app_state('global_metadata_bootstrap_v1','completed')
        (self.data/'config.json').write_text(json.dumps({'steam_id':'123','steam_api_key':'fixture-only'}))
        for file in ('slt.log','slt.log.1','instance.json','instance.lock'):
            (self.data/file).write_text('legacy fixture retained')
        before={p.name:p.read_bytes() for p in self.data.iterdir()}
        env=dict(os.environ,HOME=str(self.root),USERPROFILE=str(self.root),XDG_DATA_HOME=str(self.root),XDG_CONFIG_HOME=str(self.root),LOCALAPPDATA=str(self.root),PYTHONDONTWRITEBYTECODE='1')
        code='''import json, app_paths, database, settings, launcher, global_bootstrap
database.init_database()
print(json.dumps([str(app_paths.DATA_DIR),database.get_all_game_data()[620]['notes'],database.get_hltb_metadata(620)['hltb_id'],settings.load_settings()['steam_id'],global_bootstrap.global_bootstrap_completed(),launcher.INSTANCE_LOCK_PATH.name,launcher.INSTANCE_STATE_PATH.name]))
'''
        values=json.loads(subprocess.check_output([sys.executable,'-B','-c',code],env=env,cwd=ROOT,text=True))
        self.assertEqual(values,[str(self.data),'personal note fixture',42,'123',True,'instance.lock','instance.json'])
        self.assertEqual({p.name:p.read_bytes() for p in self.data.iterdir()},before)
        self.assertFalse((self.root/'Pecetracker').exists())

    def test_download_uses_canonical_url_and_retains_asset_filename(self):
        target=self.root/'global_metadata.db.zst'
        with patch.object(bootstrap,'urlopen',return_value=io.BytesIO(b'fixture compressed bytes')) as get:
            bootstrap._download(bootstrap.GLOBAL_DB_ZST_URL,target)
        request=get.call_args.args[0]
        self.assertEqual(request.full_url,bootstrap.GLOBAL_DB_ZST_URL)
        self.assertTrue(request.get_header('User-agent').startswith(f'Pecetracker/{version.APP_VERSION}'))
        self.assertEqual(target.read_bytes(),b'fixture compressed bytes')

    def test_bootstrap_new_repository_imports_only_missing_rows_and_preserves_manual_id(self):
        import zstandard
        sys.path.insert(0,str(ROOT/'global_metadata'))
        import global_database as gd
        gp=self.root/'global.db';gd.init_database(gp)
        gd.upsert_catalog(gp,[{'appid':620,'name':'Portal 2'}])
        gd.save_steam_ready(gp,{'appid':620})
        gd.save_steam_reviews(gp,620,{'total_reviews':100,'positive_percentage':95})
        gd.save_hltb_match(gp,620,'Portal 2',{'hltb_id':20,'match_confidence':'high'})
        database.save_hltb_metadata(620,'Portal 2','matched',hltb_id=42,match_confidence='manual')
        compressed=zstandard.ZstdCompressor().compress(gp.read_bytes())
        checksum=hashlib.sha256(compressed).hexdigest().encode()+b'  global_metadata.db.zst\n'
        downloads=[]
        def download(url,target):
            downloads.append(url)
            Path(target).write_bytes(checksum if url.endswith('.sha256') else compressed)
        with patch.object(bootstrap,'_download',side_effect=download):
            result=bootstrap.bootstrap_global_metadata([{'AppID':620}])
        self.assertTrue(result['completed'],result)
        self.assertEqual(downloads,[bootstrap.GLOBAL_DB_ZST_URL,bootstrap.GLOBAL_DB_ZST_SHA256_URL])
        self.assertEqual(database.get_game_metadata(620)['total_reviews'],100)
        self.assertEqual(database.get_hltb_metadata(620)['hltb_id'],42)
        self.assertEqual(database.get_app_state('global_metadata_bootstrap_v1'),'completed')
