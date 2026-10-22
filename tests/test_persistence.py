"""Persistence regressions: every database/configuration lives in a temporary directory."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import database as db
import settings
from sqlite_support import ClosingConnection


LEGACY_HLTB = """CREATE TABLE hltb_metadata(
 appid INTEGER PRIMARY KEY, searched_name TEXT NOT NULL, state TEXT NOT NULL,
 hltb_id INTEGER, matched_name TEXT, similarity REAL, main_story REAL,
 main_extra REAL, completionist REAL, all_styles REAL, web_link TEXT,
 fetched_at TEXT NOT NULL)"""


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "library.db"
        for p in (patch.object(db, "DATABASE_PATH", self.path),
                  patch.object(db, "ensure_data_dir"),
                  patch.object(settings, "CONFIG_PATH", Path(self.temp.name) / "config.json"),
                  patch.object(settings, "ensure_data_dir")):
            p.start(); self.addCleanup(p.stop)

    def test_concurrent_legacy_migrations_preserve_data(self):
        with sqlite3.connect(self.path) as c:
            c.execute(LEGACY_HLTB)
            c.execute("INSERT INTO hltb_metadata(appid,searched_name,state,hltb_id,fetched_at) VALUES (1,'Manual','matched',42,'2020-01-01')")
            c.execute("CREATE TABLE game_data(appid INTEGER PRIMARY KEY,status TEXT,notes TEXT)")
            c.execute("INSERT INTO game_data VALUES(1,'playing','personal notes')")
        c.close()
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(lambda _: db.init_database(), range(4)))
        self.assertEqual(db.get_all_game_data()[1]["notes"], "personal notes")
        self.assertEqual(db.get_hltb_metadata(1)["hltb_id"], 42)
        with db.get_connection() as c:
            self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)

    def test_current_initialization_is_read_only_even_with_another_writer(self):
        db.init_database()
        db.save_metadata_fetch_status(1, "metadata_unavailable", last_error="store_unavailable")
        with db.get_connection() as writer:
            writer.execute("BEGIN IMMEDIATE")
            with patch.object(db, "CONNECTION_TIMEOUT", .05):
                db.init_database()
        with db.get_connection() as c:
            self.assertEqual(c.total_changes, 0)

    def test_pending_migration_busy_is_bounded_and_retryable(self):
        db.init_database()
        db.save_game_data(1, "playing", "keep")
        with db.get_connection() as writer:
            writer.execute("PRAGMA user_version=0")
            writer.execute("BEGIN IMMEDIATE")
            with patch.object(db, "CONNECTION_TIMEOUT", .05), self.assertRaises(sqlite3.OperationalError):
                db.init_database()
        db.init_database()
        self.assertEqual(db.get_all_game_data()[1]["notes"], "keep")

    def test_failed_migration_rolls_back_ddl_and_version(self):
        with sqlite3.connect(self.path) as c:
            c.execute(LEGACY_HLTB)
        c.close()
        class Failing(ClosingConnection):
            def execute(self, sql, *args, **kwargs):
                if sql.lstrip().startswith("UPDATE metadata_fetch_status"):
                    raise sqlite3.OperationalError("injected failure")
                return super().execute(sql, *args, **kwargs)
        with patch.object(db, "get_connection", side_effect=lambda: sqlite3.connect(self.path, factory=Failing)):
            with self.assertRaises(sqlite3.OperationalError): db.init_database()
        with db.get_connection() as c:
            self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], 0)
            columns = {r[1] for r in c.execute("PRAGMA table_info(hltb_metadata)")}
            self.assertNotIn("match_confidence", columns)
        db.init_database()

    def test_connection_closes_on_success_and_exception(self):
        for fail in (False, True):
            c = db.get_connection()
            try:
                with c:
                    c.execute("CREATE TABLE IF NOT EXISTS sample(n)")
                    if fail: raise ValueError("fixture")
            except ValueError: pass
            with self.assertRaises(sqlite3.ProgrammingError): c.execute("SELECT 1")

    def test_newer_schema_is_never_downgraded(self):
        db.init_database();db.save_game_data(1,'playing','keep')
        with db.get_connection() as c:c.execute(f'PRAGMA user_version={db.SCHEMA_VERSION+1}')
        with self.assertRaises(RuntimeError):db.init_database()
        with db.get_connection() as c:
            self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0],db.SCHEMA_VERSION+1)
        self.assertEqual(db.get_all_game_data()[1]['notes'],'keep')

    def test_status_only_preserves_newer_notes(self):
        db.init_database()
        db.save_game_data(1, "backlog", "new notes from other window")
        db.save_game_status(1, "finished")
        self.assertEqual(db.get_all_game_data()[1], {"status": "finished", "notes": "new notes from other window"})

    def test_full_edit_detects_conflicting_update_and_insert(self):
        db.init_database()
        db.save_game_data(1, "backlog", "original", expected=None)
        baseline = db.get_all_game_data()[1]
        db.save_game_data(1, "playing", "new notes")
        with self.assertRaises(db.GameDataConflict):
            db.save_game_data(1, "finished", "stale draft", expected=baseline)
        with self.assertRaises(db.GameDataConflict):
            db.save_game_data(1, "finished", "stale draft", expected=None)
        self.assertEqual(db.get_all_game_data()[1]["notes"], "new notes")

    def test_import_rolls_back_partial_changes(self):
        db.init_database(); db.save_game_data(1, "playing", "keep")
        with self.assertRaises(sqlite3.Error):
            db.import_game_data([{"appid": 1, "status": "finished"}, {"appid": 2, "status": {}}])
        self.assertEqual(db.get_all_game_data()[1]["notes"], "keep")
        self.assertEqual(db.get_all_game_data()[1]["status"], "playing")

    def test_settings_atomic_replace_and_private_backup(self):
        settings.save_settings("first", "1", "secret")
        before = settings.CONFIG_PATH.read_bytes()
        settings.save_settings("second", "2", "new")
        self.assertEqual(settings.CONFIG_PATH.with_suffix(".json.bak").read_bytes(), before)
        self.assertEqual(settings.load_settings()["steam_id"], "2")
        import os
        if os.name != "nt": self.assertEqual(settings.CONFIG_PATH.stat().st_mode & 0o777, 0o600)

    def test_settings_failed_replace_or_write_keeps_original(self):
        settings.save_settings("first", "1", "secret")
        before = settings.CONFIG_PATH.read_bytes()
        real_replace = settings.os.replace
        def fail_replace(src, dst):
            if dst == settings.CONFIG_PATH: raise OSError("injected replacement failure")
            return real_replace(src, dst)
        with patch.object(settings.os, "replace", side_effect=fail_replace), self.assertRaises(OSError):
            settings.save_settings("second", "2", "new")
        self.assertEqual(settings.CONFIG_PATH.read_bytes(), before)
        with patch.object(settings.os, "fsync", side_effect=OSError("injected write failure")), self.assertRaises(OSError):
            settings.save_settings("second", "2", "new")
        self.assertEqual(settings.CONFIG_PATH.read_bytes(), before)
        self.assertEqual(list(Path(self.temp.name).glob("*.tmp")), [])

    def test_unreadable_settings_are_not_silently_reset_or_modified(self):
        for payload in (b"[]", b"null", b"{", b"\xff", b'{"steam_id": []}'):
            with self.subTest(payload=payload):
                settings.CONFIG_PATH.write_bytes(payload)
                with self.assertRaises(settings.SettingsError): settings.load_settings()
                self.assertEqual(settings.CONFIG_PATH.read_bytes(), payload)
        settings.CONFIG_PATH.unlink()
        self.assertEqual(settings.load_settings(), settings.DEFAULT_SETTINGS)

    def test_legacy_numeric_steam_id(self):
        settings.CONFIG_PATH.write_text(json.dumps({"steam_id": 76561198000000000}))
        self.assertTrue(settings.load_settings()["steam_profile"].endswith("76561198000000000/"))
