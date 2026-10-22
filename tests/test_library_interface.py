"""Real Streamlit widget regressions against disposable SQLite data."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest
import database as db
from constants import STATUSES, STATUS_BACKLOG, STATUS_FINISHED, STATUS_PLAYING


LIBRARY_APP = '''
import pandas as pd
from constants import STATUS_BACKLOG
from database import get_all_game_data
from ui.common import init_session_state
from ui.game_dialog import show_selected_game_dialog
from ui.library import render_library
init_session_state()
saved = get_all_game_data().get(620)
df = pd.DataFrame([{"AppID":620,"Game":"Game (Edition) [x]+.","Hours":1.0,
 "Status":saved["status"] if saved else STATUS_BACKLOG,
 "Notes":saved["notes"] if saved else "", "_saved_data":saved}])
show_selected_game_dialog(df)
render_library(df)
'''


class LibraryInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        for p in (patch.object(db,'DATABASE_PATH',Path(self.temp.name)/'library.db'),
                  patch.object(db,'ensure_data_dir'),
                  patch('requests.adapters.HTTPAdapter.send',side_effect=AssertionError('Real HTTP forbidden'))):
            p.start();self.addCleanup(p.stop)
        db.init_database();db.save_game_data(620,STATUS_BACKLOG,'saved notes')
        self.app=AppTest.from_string(LIBRARY_APP,default_timeout=10).run()
        self.assertEqual(len(self.app.exception),0)

    def test_search_treats_metacharacters_literally(self):
        for term in ('[','(','+','.'):
            with self.subTest(term=term):
                self.app.text_input(key='library_search').set_value(term).run()
                self.assertEqual(len(self.app.exception),0)
                self.assertTrue(any(b.key=='open_game_620' for b in self.app.button))
        self.app.text_input(key='library_search').set_value('\\').run()
        self.assertEqual(len(self.app.exception),0)
        self.assertFalse(any(b.key=='open_game_620' for b in self.app.button))

    def test_restored_status_is_displayed_and_quick_change_preserves_notes(self):
        db.import_game_data([{'appid':620,'status':STATUS_FINISHED,'notes':'restored notes'}])
        self.app.run()
        self.assertEqual(self.app.selectbox(key=f'gallery_status_620_{STATUSES.index(STATUS_FINISHED)}').value,STATUS_FINISHED)
        db.save_game_data(620,STATUS_FINISHED,'newer notes in another window')
        self.app.selectbox(key=f'gallery_status_620_{STATUSES.index(STATUS_FINISHED)}').select(STATUS_PLAYING).run()
        self.assertEqual(db.get_all_game_data()[620],{'status':STATUS_PLAYING,'notes':'newer notes in another window'})
        self.assertEqual(len(self.app.exception),0)

    def test_conflicting_full_edit_keeps_draft_and_external_notes(self):
        self.app.button(key='open_game_620').click().run()
        self.app.text_area(key='detail_notes_620').set_value('unsaved draft').run()
        db.save_game_data(620,STATUS_PLAYING,'external notes')
        # A full rerun reloads SQLite, but must not advance the draft baseline.
        self.app.run()
        self.app.button(key='detail_save_620').click().run()
        self.assertEqual(self.app.text_area(key='detail_notes_620').value,'unsaved draft')
        self.assertEqual(db.get_all_game_data()[620]['notes'],'external notes')
        self.assertTrue(any('Another window changed' in w.value for w in self.app.warning))
        self.assertEqual(len(self.app.exception),0)

    def test_empty_detail_status_cannot_overwrite_personal_data(self):
        self.app.button(key='open_game_620').click().run()
        status_id=self.app.selectbox(key='detail_status_620').id
        self.app.button(key='detail_save_620').click()
        widgets=self.app._tree.get_widget_states()
        for state in widgets.widgets:
            if state.id==status_id:state.string_value=''
        self.app._run(widgets)
        self.assertEqual(db.get_all_game_data()[620]['status'],STATUS_BACKLOG)
        self.assertTrue(any('Choose a status' in w.value for w in self.app.warning))

    def test_first_personal_save_uses_absent_row_baseline(self):
        with db.get_connection() as c:c.execute('DELETE FROM game_data')
        self.app.run();self.app.button(key='open_game_620').click().run()
        self.app.text_area(key='detail_notes_620').set_value('first notes').run()
        self.app.button(key='detail_save_620').click().run()
        self.assertEqual(db.get_all_game_data()[620]['notes'],'first notes')
        self.assertEqual(len(self.app.exception),0)


class ApplicationStartupTests(unittest.TestCase):
    def test_full_application_renders_configured_and_unconfigured_without_network(self):
        import settings, steam_cache, update_checker, global_bootstrap, metadata_background, hltb_background, app_logging
        with tempfile.TemporaryDirectory() as scratch:
            patches=(patch.object(db,'DATABASE_PATH',Path(scratch)/'library.db'),patch.object(db,'ensure_data_dir'),
                     patch.object(app_logging,'configure_logging'),patch.object(update_checker,'check_for_updates',return_value=None),
                     patch.object(global_bootstrap,'start_global_bootstrap'),patch.object(global_bootstrap,'get_bootstrap_result',return_value=None),
                     patch.object(metadata_background,'start_metadata_background_refresh'),patch.object(hltb_background,'start_hltb_background_refresh'),
                     patch.object(steam_cache,'load_games',return_value=[{'appid':620,'name':'Fixture','playtime_forever':0}]),
                     patch.object(steam_cache,'load_player_summary',return_value={'personaname':'Fixture'}),
                     patch('requests.adapters.HTTPAdapter.send',side_effect=AssertionError('Real HTTP forbidden')))
            for p in patches:p.start();self.addCleanup(p.stop)
            for configured in (False,True):
                values={'steam_profile':'fixture','steam_id':'123' if configured else '', 'steam_api_key':'fixture-key' if configured else ''}
                with self.subTest(configured=configured),patch.object(settings,'load_settings',return_value=values):
                    app=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'app.py'),default_timeout=10).run()
                    self.assertEqual(len(app.exception),0)
                    if configured:self.assertTrue(any(b.key=='open_game_620' for b in app.button))
