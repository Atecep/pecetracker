"""Ownership coverage, JSON backup contract, persistent diagnostics."""
from contextlib import redirect_stdout
import inspect
import io
import json
import logging
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import app_logging
import database as db
import steam_cache
from ui import account
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'global_metadata'))
import global_database as gd
import check_library_coverage as coverage


class OperationalContractTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        for p in (patch.object(db,'DATABASE_PATH',self.root/'local.db'),patch.object(db,'ensure_data_dir')):
            p.start();self.addCleanup(p.stop)
        db.init_database()

    def test_successful_ownership_fetch_is_separate_from_personal_edits(self):
        games=[{'appid':n,'name':str(n)} for n in range(1,11)]
        db.save_game_data(1,'playing','only one edited game')
        with patch.object(steam_cache,'get_owned_games',return_value=games):
            self.assertEqual(inspect.unwrap(steam_cache.load_games)('key','account'),games)
        self.assertEqual(len(db.get_library_appids('account')),10)
        self.assertEqual(len(db.get_all_game_data()),1)
        with patch.object(steam_cache,'get_owned_games',return_value=[]):
            inspect.unwrap(steam_cache.load_games)('key','account')
        self.assertEqual(len(db.get_library_appids('account')),10)

    def test_coverage_uses_owned_games_and_refuses_missing_snapshot(self):
        gp=self.root/'global.db';gd.init_database(gp)
        db.save_game_data(1,'playing','one edit')
        gd.upsert_catalog(gp,[{'appid':n,'name':str(n)} for n in range(1,11)])
        with patch.object(sys,'argv',['coverage',str(gp),'--steam-id','account']):
            with self.assertRaisesRegex(SystemExit,'No ownership snapshot'):coverage.main()
            db.save_library_appids('account',[{'appid':n} for n in range(1,11)])
            output=io.StringIO()
            with redirect_stdout(output):coverage.main()
        self.assertIn('Total games:             10',output.getvalue())

    def test_backup_contains_personal_edits_only_and_does_not_modify_manual_ids(self):
        db.save_game_data(1,'playing','private notes')
        db.save_hltb_metadata(1,'Fixture','matched',hltb_id=99,match_confidence='manual')
        ui=MagicMock();ui.tabs.return_value=[MagicMock(),MagicMock()];ui.button.return_value=False
        with patch.object(account,'st',ui):account.render_backup_restore()
        exported=json.loads(ui.download_button.call_args.kwargs['data'])
        self.assertEqual(exported['games'],[{'appid':1,'status':'playing','notes':'private notes'}])
        self.assertEqual(db.get_hltb_metadata(1)['hltb_id'],99)
        self.assertNotIn('steam_api_key',exported)

    def test_windowed_diagnostics_are_written_to_rotating_log(self):
        logger=logging.getLogger('slt');previous=logger.handlers[:]
        logger.handlers=[]
        try:
            with patch.object(app_logging,'ensure_data_dir',return_value=self.root):
                app_logging.configure_logging()
                try:raise RuntimeError('fixture diagnostic')
                except RuntimeError:app_logging.get_logger('test').exception('Worker failed')
                for handler in logger.handlers:handler.flush()
            text=(self.root/'slt.log').read_text()
            self.assertIn('fixture diagnostic',text);self.assertIn('Traceback',text)
            self.assertTrue(all(getattr(h,'maxBytes',0)>0 for h in logger.handlers))
        finally:
            for handler in logger.handlers:handler.close()
            logger.handlers=previous
