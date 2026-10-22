import inspect
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd
import streamlit as st
from streamlit.testing.v1 import AppTest

from constants import STATUS_BACKLOG, STATUS_FINISHED, STATUS_PLAYING
from ui import common, game_dialog


class SessionState(dict):
    def __getattr__(self, name):
        return self[name]

    def __setattr__(self, name, value):
        self[name] = value


class RerunRequested(Exception):
    pass


class GameDialogNavigationTests(unittest.TestCase):
    def setUp(self):
        self.analysis = {"summary": None}
        self.state = SessionState(
            selected_game_appid=620,
            open_game_dialog=True,
            open_protondb_dialog=True,
            game_dialog_source="library",
            smart_pick_context=None,
            smart_pick_history=[],
            protondb_analysis_cache={620: self.analysis},
        )
        self.ui = MagicMock(session_state=self.state)
        self.ui.columns.side_effect = lambda spec, **kwargs: [
            MagicMock() for _ in range(spec if isinstance(spec, int) else len(spec))
        ]
        self.ui.button.return_value = False
        self.ui.rerun.side_effect = RerunRequested
        self.game = {
            "AppID": 620,
            "Game": "Portal 2",
            "Hours": 5.0,
            "Status": STATUS_BACKLOG,
            "Notes": "",
        }
        self.df = pd.DataFrame([self.game])
        self.patches = [
            patch.object(game_dialog, "st", self.ui),
            patch.object(common, "st", self.ui),
        ]
        for mocked in self.patches:
            mocked.start()
            self.addCleanup(mocked.stop)

    def test_dismiss_returns_to_library_and_retains_cache(self):
        game_dialog._dismiss_protondb_dialog()
        self.assertFalse(self.state.open_protondb_dialog)
        self.assertFalse(self.state.open_game_dialog)
        self.assertIsNone(self.state.selected_game_appid)
        self.assertIsNone(self.state.game_dialog_source)
        self.assertIs(self.state.protondb_analysis_cache[620], self.analysis)
        self.ui.rerun.assert_not_called()
        with (
            patch.object(game_dialog, "show_game_details") as details,
            patch.object(game_dialog, "show_protondb_analysis_dialog") as analysis,
        ):
            game_dialog.show_selected_game_dialog(self.df)
            game_dialog.show_selected_game_dialog(self.df)
            details.assert_not_called()
            analysis.assert_not_called()

    def test_back_routes_to_details_of_same_game(self):
        self.ui.button.side_effect = lambda label, **kwargs: label == "← Back"
        with self.assertRaises(RerunRequested):
            inspect.unwrap(game_dialog.show_protondb_analysis_dialog)(620, "Portal 2")
        self.assertFalse(self.state.open_protondb_dialog)
        self.assertTrue(self.state.open_game_dialog)
        self.assertEqual(self.state.selected_game_appid, 620)
        self.assertIs(self.state.protondb_analysis_cache[620], self.analysis)
        self.ui.rerun.assert_called_once_with()
        with patch.object(game_dialog, "show_game_details") as details:
            game_dialog.show_selected_game_dialog(self.df)
            self.assertEqual(details.call_args.args[0]["AppID"], 620)

    def test_reopening_reuses_cached_analysis(self):
        game_dialog._dismiss_protondb_dialog()
        common.open_game_dialog(620)
        game_dialog._open_protondb_dialog()
        with patch.object(game_dialog, "analyse_protondb") as fetch:
            inspect.unwrap(game_dialog.show_protondb_analysis_dialog)(620, "Portal 2")
            fetch.assert_not_called()
        self.assertIs(self.state.protondb_analysis_cache[620], self.analysis)
        self.ui.rerun.assert_not_called()

    def test_analyse_button_is_visible_only_on_linux(self):
        for system, visible in (("Linux", True), ("Windows", False)):
            with (
                self.subTest(system=system),
                patch.object(game_dialog.platform, "system", return_value=system),
                patch.object(game_dialog, "get_game_metadata", return_value=None),
                patch.object(game_dialog, "get_metadata_fetch_status", return_value=None),
                patch.object(game_dialog, "get_hltb_metadata", return_value=None),
            ):
                self.ui.button.reset_mock()
                inspect.unwrap(game_dialog.show_game_details)(self.game, self.df)
                labels = [call.args[0] for call in self.ui.button.call_args_list]
                self.assertEqual("🐧 Analyse ProtonDB" in labels, visible)

    def test_analyse_button_opens_analysis_on_linux(self):
        self.state.open_protondb_dialog = False
        self.ui.button.side_effect = lambda label, **kwargs: label == "🐧 Analyse ProtonDB"
        with (
            patch.object(game_dialog.platform, "system", return_value="Linux"),
            patch.object(game_dialog, "get_game_metadata", return_value=None),
            patch.object(game_dialog, "get_metadata_fetch_status", return_value=None),
            patch.object(game_dialog, "get_hltb_metadata", return_value=None),
        ):
            with self.assertRaises(RerunRequested):
                inspect.unwrap(game_dialog.show_game_details)(self.game, self.df)
        self.assertTrue(self.state.open_protondb_dialog)
        self.assertEqual(self.state.selected_game_appid, 620)
        self.ui.rerun.assert_called_once_with()
        with patch.object(game_dialog, "show_protondb_analysis_dialog") as analysis:
            game_dialog.show_selected_game_dialog(self.df)
            analysis.assert_called_once_with(620, "Portal 2")


_NAVIGATION_APP = """
import pandas as pd
import streamlit as st
from constants import STATUS_BACKLOG, STATUS_FINISHED
from ui import game_dialog
from ui.common import init_session_state, open_game_dialog

init_session_state()
st.session_state.setdefault("fetch_calls", 0)
st.session_state.setdefault("save_calls", [])
st.session_state.setdefault("saved_games", {
    620: {"AppID": 620, "Game": "Portal 2", "Hours": 5.0,
          "Status": STATUS_BACKLOG, "Notes": "saved notes"},
    621: {"AppID": 621, "Game": "Other game", "Hours": 1.0,
          "Status": STATUS_FINISHED, "Notes": "other notes"},
})
df = pd.DataFrame(list(st.session_state.saved_games.values()))
st.button("Open game", key="open_test_game", on_click=open_game_dialog, args=(620,))
game_dialog.show_selected_game_dialog(df)
st.caption("Library")
"""


class GameDialogStreamlitTests(unittest.TestCase):
    """Exercise real widget cleanup and dialog callbacks without touching SQLite."""

    def setUp(self):
        def analyse(appid):
            st.session_state.fetch_calls += 1
            mode = st.session_state.get("analysis_mode")
            if mode == "fail_first" and st.session_state.fetch_calls == 1:
                raise RuntimeError("Simulated summary failure")
            if mode == "partial":
                return {
                    "summary": {"effective_tier": "gold", "total": 2},
                    "reports_analysed": 1,
                    "reports_partial": True,
                    "detail_error": "Some reports could not be loaded.",
                    "ranked_options": [{
                        "command": "SteamDeck=0 %command%", "count": 1,
                        "recent_count": 1, "latest_age_days": 0,
                    }],
                }
            return {"summary": None}

        def save(appid, status, notes, expected=None):
            st.session_state.save_calls.append((appid, status, notes))
            st.session_state.saved_games[appid].update(Status=status, Notes=notes)

        patches = (
            patch.object(game_dialog.platform, "system", return_value="Linux"),
            patch.object(game_dialog, "get_game_metadata", return_value=None),
            patch.object(game_dialog, "get_metadata_fetch_status", return_value=None),
            patch.object(game_dialog, "get_hltb_metadata", return_value=None),
            patch.object(game_dialog, "analyse_protondb", side_effect=analyse),
            patch.object(game_dialog, "save_game_data", side_effect=save),
        )
        for mocked in patches:
            mocked.start()
            self.addCleanup(mocked.stop)
        self.app = AppTest.from_string(_NAVIGATION_APP, default_timeout=15).run()
        self.app.button(key="open_test_game").click().run()
        self.assertEqual(self.app.session_state["fetch_calls"], 0)

    def edit(self, notes="draft notes", status=STATUS_PLAYING):
        self.app.text_area(key="detail_notes_620").set_value(notes).run()
        self.app.selectbox(key="detail_status_620").select(status).run()

    def open_analysis(self):
        self.app.button(key="protondb_open_620").click().run()
        self.assertEqual(len(self.app.exception), 0)

    def dismiss(self):
        # Streamlit's X, Escape and outside click send this same trigger.
        dialog = next(node for node in self.app._tree if getattr(node, "type", None) == "dialog")
        widgets = self.app._tree.get_widget_states()
        widgets.widgets.add(id=dialog.proto.dialog.id, trigger_value=True)
        self.app._run(widgets)

    def test_back_restores_notes_status_and_save_stays_explicit(self):
        self.edit()
        self.open_analysis()
        self.assertEqual(self.app.session_state["save_calls"], [])
        self.app.button(key="protondb_back_620").click().run()
        self.assertEqual(self.app.text_area(key="detail_notes_620").value, "draft notes")
        self.assertEqual(self.app.selectbox(key="detail_status_620").value, STATUS_PLAYING)
        self.assertEqual(self.app.session_state["saved_games"][620]["Notes"], "saved notes")
        self.assertEqual(self.app.session_state["save_calls"], [])

        self.edit("second draft", STATUS_FINISHED)
        self.open_analysis()
        self.app.button(key="protondb_back_620").click().run()
        self.assertEqual(self.app.text_area(key="detail_notes_620").value, "second draft")
        self.assertEqual(self.app.selectbox(key="detail_status_620").value, STATUS_FINISHED)
        self.assertEqual(self.app.session_state["fetch_calls"], 1)

        self.app.button(key="detail_save_620").click().run()
        self.assertEqual(self.app.session_state["save_calls"], [(620, STATUS_FINISHED, "second draft")])
        self.assertEqual(self.app.session_state["saved_games"][620]["Notes"], "second draft")
        self.assertEqual(self.app.session_state["saved_games"][621]["Notes"], "other notes")
        self.assertEqual(len(self.app.exception), 0)

    def test_open_captures_latest_fields_in_same_rerun(self):
        self.app.text_area(key="detail_notes_620").set_value("latest edit")
        self.app.selectbox(key="detail_status_620").select(STATUS_PLAYING)
        self.open_analysis()
        self.app.button(key="protondb_back_620").click().run()
        self.assertEqual(self.app.text_area(key="detail_notes_620").value, "latest edit")
        self.assertEqual(self.app.selectbox(key="detail_status_620").value, STATUS_PLAYING)
        self.assertEqual(self.app.session_state["save_calls"], [])

    def test_dismiss_closes_everything_discards_draft_and_keeps_cache(self):
        self.edit()
        self.open_analysis()
        self.dismiss()
        self.assertIsNone(self.app.session_state["selected_game_appid"])
        self.assertFalse(self.app.session_state["open_game_dialog"])
        self.assertFalse(self.app.session_state["open_protondb_dialog"])
        self.assertNotIn("protondb_game_draft", self.app.session_state)
        self.assertIn(620, self.app.session_state["protondb_analysis_cache"])
        self.app.run()
        self.assertFalse(any(getattr(node, "type", None) == "dialog" for node in self.app._tree))
        self.app.button(key="open_test_game").click().run()
        self.assertEqual(self.app.text_area(key="detail_notes_620").value, "saved notes")
        self.assertEqual(self.app.selectbox(key="detail_status_620").value, STATUS_BACKLOG)
        self.open_analysis()
        self.assertEqual(self.app.session_state["fetch_calls"], 1)
        self.assertEqual(self.app.session_state["save_calls"], [])

    def test_snapshot_cannot_be_restored_to_another_game(self):
        self.edit()
        self.open_analysis()
        self.app.session_state["selected_game_appid"] = 621
        self.app.session_state["open_protondb_dialog"] = False
        self.app.run()
        self.assertEqual(self.app.text_area(key="detail_notes_621").value, "other notes")
        self.assertEqual(self.app.selectbox(key="detail_status_621").value, STATUS_FINISHED)
        self.assertNotIn("protondb_game_draft", self.app.session_state)
        self.assertEqual(self.app.session_state["save_calls"], [])
        self.app.button(key="protondb_open_621").click().run()
        self.app.button(key="protondb_back_621").click().run()
        self.assertEqual(self.app.text_area(key="detail_notes_621").value, "other notes")
        self.assertEqual(self.app.selectbox(key="detail_status_621").value, STATUS_FINISHED)
        self.assertEqual(set(self.app.session_state["protondb_analysis_cache"]), {620, 621})
        self.assertEqual(self.app.session_state["fetch_calls"], 2)

    def test_refresh_after_initial_failure_and_back_preserve_draft(self):
        self.app.session_state["analysis_mode"] = "fail_first"
        self.edit()
        self.open_analysis()
        self.assertEqual(len(self.app.error), 1)
        self.assertNotIn(620, self.app.session_state["protondb_analysis_cache"])
        self.app.button(key="protondb_refresh_620").click().run()
        self.assertEqual(self.app.session_state["fetch_calls"], 2)
        self.assertEqual(len(self.app.error), 0)
        self.app.button(key="protondb_back_620").click().run()
        self.assertEqual(self.app.text_area(key="detail_notes_620").value, "draft notes")
        self.assertEqual(self.app.selectbox(key="detail_status_620").value, STATUS_PLAYING)
        self.assertEqual(self.app.session_state["save_calls"], [])

    def test_partial_results_are_visible_warned_cached_and_refreshable(self):
        self.app.session_state["analysis_mode"] = "partial"
        self.open_analysis()
        self.assertIn("Partial analysis", self.app.warning[0].value)
        self.assertEqual(self.app.code[0].value, "SteamDeck=0 %command%")
        self.app.button(key="protondb_back_620").click().run()
        self.open_analysis()
        self.assertEqual(self.app.session_state["fetch_calls"], 1)
        self.assertEqual(len(self.app.warning), 1)
        self.app.session_state["analysis_mode"] = None
        self.app.button(key="protondb_refresh_620").click().run()
        self.assertEqual(self.app.session_state["fetch_calls"], 2)
        self.assertEqual(len(self.app.warning), 0)
        self.assertEqual(len(self.app.exception), 0)


if __name__ == "__main__":
    unittest.main()
