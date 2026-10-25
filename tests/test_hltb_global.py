"""Offline regressions for the original global HLTB matcher and timing."""
from contextlib import closing, redirect_stdout
from datetime import datetime, timedelta, timezone
import importlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
from urllib.parse import urlsplit
import unittest
from unittest.mock import patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "global_metadata"))
import build_global_database as builder
import global_database as db
import hltb_service as matcher
import constants as app_constants


def entry(game_id, name, similarity=1.0, year=2020, main=10.0):
    return SimpleNamespace(
        game_id=game_id, game_name=name, similarity=similarity, release_world=year,
        main_story=main, main_extra=15.5, completionist=22.0, all_styles=12.0,
        game_web_link=f"https://howlongtobeat.com/game/{game_id}",
    )


def response(status=200, body=None, headers=None):
    result = requests.Response()
    result.status_code = status
    result._content = (json.dumps(body) if not isinstance(body, str) else body).encode()
    result._content_consumed = True
    result.headers.update(headers or {})
    return result


class FakeHTTP:
    """Queues failures per phase, then serves a valid empty search by default."""
    def __init__(self, failures=None, games=None):
        self.failures = {key: list(value) for key, value in (failures or {}).items()}
        self.games = games or {}
        self.calls = []

    def get(self, url, **kwargs):
        phase = "script" if "/chunks/" in url else "authentication" if urlsplit(url).path.endswith("/init") else "discovery"
        return self._request(phase, url, kwargs)

    def post(self, url, **kwargs):
        return self._request("search", url, kwargs)

    def send(self, request, **kwargs):
        if request.method == "POST":
            result = self.post(request.url, data=request.body, **kwargs)
        else:
            result = self.get(request.url, **kwargs)
        result.request = request
        result.url = request.url
        return result

    def _request(self, phase, url, kwargs):
        self.calls.append((phase, url, kwargs))
        queue = self.failures.get(phase, [])
        if queue:
            result = queue.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        if phase == "discovery":
            return response(body='<script src="/_next/static/chunks/test.js"></script>')
        if phase == "script":
            return response(body='fetch("/api/search/site", {method: "POST"})')
        if phase == "authentication":
            return response(body={"token": "SECRET_TOKEN", "key": "SECRET_KEY", "value": "SECRET_VALUE"})
        query = " ".join(json.loads(kwargs["data"])["searchTerms"])
        return response(body={"data": self.games.get(query, [])})


class FakeClock:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def wall_time(self):
        return 1_800_000_000 + self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class OfflineTest(unittest.TestCase):
    def setUp(self):
        guard = patch("requests.adapters.HTTPAdapter.send", side_effect=AssertionError("Real HTTP forbidden"))
        guard.start()
        self.addCleanup(guard.stop)
        self.output = io.StringIO()
        redirect = redirect_stdout(self.output)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        module = importlib.import_module("howlongtobeatpy.HTMLRequests")
        agent = patch.object(module, "UserAgent", return_value=SimpleNamespace(random="fixture-agent"))
        agent.start()
        self.addCleanup(agent.stop)
        original_url = module.HTMLRequests.SEARCH_URL
        self.addCleanup(setattr, module.HTMLRequests, "SEARCH_URL", original_url)
        sleep_patch = patch.object(builder.time, "sleep")
        self.sleep = sleep_patch.start()
        self.addCleanup(sleep_patch.stop)
        for name in ("_hltb_request_interval", "_hltb_last_request_started", "_hltb_retry_delay"):
            self.addCleanup(setattr, builder, name, getattr(builder, name))
        builder._hltb_last_request_started = 0.0
        builder.configure_hltb_request_throttle(0)

    def http(self, fake):
        mocked = patch("requests.adapters.HTTPAdapter.send", side_effect=fake.send)
        mocked.start()
        self.addCleanup(mocked.stop)


class MatcherBaselineTests(OfflineTest):
    def compare(self, title, fixtures, expected_queries, expected=None):
        before_calls = []
        after_calls = []

        def original(query):
            before_calls.append(query)
            return fixtures.get(query, [])

        with patch.object(matcher, "_search_once", side_effect=original):
            before = matcher._search_hltb(title)

        def request(query):
            after_calls.append(query)
            return fixtures.get(query, [])

        with patch.object(builder, "_run_hltb_request", side_effect=request):
            after = builder.search_hltb_global(title)
        self.assertEqual(before_calls, expected_queries)
        self.assertEqual(after_calls, expected_queries)
        self.assertEqual(after, before)
        if expected is None:
            self.assertIsNone(after)
        else:
            game_id, name, similarity, confidence, main = expected
            self.assertEqual(after, {
                "state": "matched", "hltb_id": game_id, "matched_name": name,
                "similarity": similarity, "match_confidence": confidence,
                "matcher_version": matcher.HLTB_MATCHER_VERSION,
                "main_story": main, "main_extra": 15.5, "completionist": 22.0,
                "all_styles": 12.0, "web_link": f"https://howlongtobeat.com/game/{game_id}",
            })

    def test_exact_match_preserves_all_data(self):
        self.compare("Portal", {"Portal": [entry(1, "Portal")]}, ["Portal"], (1, "Portal", 1.0, "high", 10.0))

    def test_no_match_keeps_all_variants_and_fallbacks(self):
        self.compare("Game: Complete Edition", {}, ["Game: Complete Edition", "Game Complete Edition", "Game", "Game Complete"])

    def test_best_candidate_from_later_variant_wins(self):
        self.compare("The Example: Game", {
            "The Example: Game": [entry(1, "The Example Other", .95)],
            "The Example Game": [entry(2, "The Example Game")],
        }, ["The Example: Game", "The Example Game"], (2, "The Example Game", 1.0, "high", 10.0))

    def test_safe_edition_fallback(self):
        self.compare("The Example Complete Edition", {"The Example": [entry(2, "The Example")]},
                     ["The Example Complete Edition", "The Example"], (2, "The Example", 1.0, "high", 10.0))

    def test_year_conflict_rejected(self):
        self.compare("GRID (2019)", {"GRID (2019)": [entry(1, "GRID", year=2013)], "GRID": [entry(1, "GRID", year=2013)]},
                     ["GRID (2019)", "GRID"])

    def test_competing_years_choose_matching_release(self):
        self.compare("GRID (2019)", {"GRID": [entry(1, "GRID", year=2013), entry(2, "GRID", year=2019)]},
                     ["GRID (2019)", "GRID"], (2, "GRID", 1.0, "high", 10.0))

    def test_structural_fallback_stops_on_match(self):
        self.compare("The Example HD", {"The Example": [entry(3, "The Example")]},
                     ["The Example HD", "The Example"], (3, "The Example", 1.0, "high", 10.0))

    def test_confirmed_match_keeps_similarity_and_confidence(self):
        self.compare("The Example HD", {"The Example HD": [entry(3, "The Example", .5)]},
                     ["The Example HD"], (3, "The Example", .88, "confirmed", 10.0))

    def test_remastered_fallbacks_remain_unchanged(self):
        self.compare("The Legend of Example Remastered", {}, [
            "The Legend of Example Remastered", "The Legend of Example", "The Legend of", "The Legend",
        ])

    def test_baseline_with_real_dependency_parser_and_mock_http(self):
        self.http(FakeHTTP(games={"The Example": [
            {"game_id": 20, "game_name": "The Example", "release_world": 2020,
             "comp_main": 36000, "comp_plus": 55800, "comp_100": 79200, "comp_all": 43200},
        ]}))
        # Both paths use the installed library's parser and response fixtures.
        # The unmodified desktop matcher supplies the baseline.
        before = matcher._search_hltb("The Example Complete Edition")
        after = builder.search_hltb_global("The Example Complete Edition")
        self.assertEqual(after, before)
        self.assertEqual(after["hltb_id"], 20)
        self.assertEqual((after["main_story"], after["main_extra"], after["completionist"], after["all_styles"]),
                         (10.0, 15.5, 22.0, 12.0))

    def test_duplicate_candidate_uses_stronger_variant_data(self):
        self.compare("The Example: Game", {
            "The Example: Game": [entry(2, "The Example Game", .9, main=8)],
            "The Example Game": [entry(2, "The Example Game", 1.0, main=16)],
        }, ["The Example: Game", "The Example Game"], (2, "The Example Game", 1.0, "high", 16.0))


class SearchTimingTests(OfflineTest):
    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        self.sleep.side_effect = self.clock.sleep
        timer = patch.object(builder.time, "monotonic", side_effect=self.clock.monotonic)
        timer.start()
        self.addCleanup(timer.stop)
        builder.configure_hltb_request_throttle(2)

    def test_defaults_and_original_library_arguments(self):
        with patch.object(sys, "argv", ["builder"]):
            args = builder.parse_args()
        self.assertEqual((args.hltb_limit, args.hltb_pause), (500, 2))
        self.assertEqual((builder.HLTB_ERROR_BACKOFF_SECONDS, builder.HLTB_REPEATED_ERROR_BACKOFF_SECONDS), (5, 10))
        with patch.object(builder, "HowLongToBeat") as library:
            library.return_value.search.return_value = []
            builder._run_hltb_request("Portal")
        library.assert_called_once_with(0.4)
        library.return_value.search.assert_called_once_with("Portal", similarity_case_sensitive=False)

    def test_variants_and_structural_fallbacks_are_spaced_two_seconds(self):
        starts = []
        with patch.object(builder, "HowLongToBeat") as library:
            def search(query, **kwargs):
                starts.append((query, self.clock.now))
                return []
            library.return_value.search.side_effect = search
            self.assertIsNone(builder.search_hltb_global("Game: Complete Edition"))
        self.assertEqual(starts, [("Game: Complete Edition", 100), ("Game Complete Edition", 102),
                                  ("Game", 104), ("Game Complete", 106)])
        self.assertEqual(self.clock.sleeps, [2, 2, 2])

    def test_gap_is_shared_between_games(self):
        with patch.object(builder, "HowLongToBeat") as library:
            library.return_value.search.return_value = []
            builder.search_hltb_global("Portal")
            builder.search_hltb_global("Missing")
        self.assertEqual(self.clock.now, 102)
        self.assertEqual(self.clock.sleeps, [2])

    def test_none_retries_once_after_five_seconds_without_double_wait(self):
        starts = []
        with patch.object(builder, "HowLongToBeat") as library:
            def search(*args, **kwargs):
                starts.append(self.clock.now)
                return None if len(starts) == 1 else []
            library.return_value.search.side_effect = search
            self.assertEqual(builder._throttled_hltb_search_once("Portal"), [])
        self.assertEqual(starts, [100, 105])
        self.assertEqual(self.clock.sleeps, [5])

    def test_two_none_results_raise_instead_of_no_match(self):
        with patch.object(builder, "HowLongToBeat") as library:
            library.return_value.search.return_value = None
            with self.assertRaisesRegex(RuntimeError, "HowLongToBeat search failed for query"):
                builder.search_hltb_global("Portal")
            self.assertEqual(library.return_value.search.call_count, 2)
        self.assertEqual(self.clock.sleeps, [5])

    def test_empty_list_is_no_match_without_retry(self):
        with patch.object(builder, "HowLongToBeat") as library:
            library.return_value.search.return_value = []
            self.assertIsNone(builder.search_hltb_global("Portal"))
            library.return_value.search.assert_called_once()
        self.sleep.assert_not_called()

    def test_malformed_search_results_are_errors_instead_of_no_match(self):
        for results in ({}, '', [None], [SimpleNamespace(game_id=None, game_name='Portal')],
                        [SimpleNamespace(game_id=20, game_name=None)]):
            with self.subTest(results=results), \
                 patch.object(builder, '_run_hltb_request', return_value=results):
                with self.assertRaises(ValueError):
                    builder.search_hltb_global('Portal')

    def test_exceptions_keep_original_no_retry_behaviour_and_restore_matcher(self):
        original = matcher._search_once
        with patch.object(builder, "HowLongToBeat") as library:
            library.return_value.search.side_effect = requests.Timeout("fixture timeout")
            with self.assertRaises(requests.Timeout):
                builder.search_hltb_global("Portal")
            library.return_value.search.assert_called_once()
        self.assertIs(matcher._search_once, original)
        self.sleep.assert_not_called()

    def test_request_duration_counts_towards_interval(self):
        with patch.object(builder, "HowLongToBeat") as library:
            def search(*args, **kwargs):
                self.clock.now += .5
                return []
            library.return_value.search.side_effect = search
            builder._run_hltb_request("Portal")
            builder._run_hltb_request("Missing")
        self.assertEqual(self.clock.sleeps, [1.5])
        self.assertEqual(self.clock.now, 102.5)

    def test_slow_search_has_no_extra_pause(self):
        with patch.object(builder, "HowLongToBeat") as library:
            def search(*args, **kwargs):
                self.clock.now += 3
                return []
            library.return_value.search.side_effect = search
            builder._run_hltb_request("Portal")
            builder._run_hltb_request("Missing")
        self.sleep.assert_not_called()

    def test_retry_still_respects_a_longer_configured_interval(self):
        builder.configure_hltb_request_throttle(10)
        with patch.object(builder, "HowLongToBeat") as library:
            library.return_value.search.side_effect = [None, []]
            builder._throttled_hltb_search_once("Portal")
        self.assertEqual(self.clock.sleeps, [5, 5])
        self.assertEqual(self.clock.now, 110)

    def test_http_calls_inside_search_are_not_instrumented(self):
        times = []
        fake = FakeHTTP()
        def send(request, **kwargs):
            times.append(self.clock.now)
            return fake.send(request, **kwargs)
        with patch("requests.adapters.HTTPAdapter.send", side_effect=send):
            builder.search_hltb_global("Portal")
            requests.get("https://steam.invalid/")
            builder.search_hltb_global("Missing")
        self.assertEqual(times, [100] * 5 + [102] * 4)
        self.assertEqual(self.clock.sleeps, [2])


class SQLiteProgressTests(OfflineTest):
    def setUp(self):
        super().setUp()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "global.db"
        connections = []
        connect = db.connect
        def tracked_connect(path):
            connection = connect(path)
            connections.append(connection)
            return connection
        tracked = patch.object(db, "connect", side_effect=tracked_connect)
        tracked.start()
        self.addCleanup(tracked.stop)
        self.addCleanup(lambda: [connection.close() for connection in connections])
        self.now = datetime(2026, 10, 11, tzinfo=timezone.utc)
        timer = patch.object(db, "_now", return_value=self.now)
        timer.start()
        self.addCleanup(timer.stop)
        db.init_database(self.path)
        db.upsert_catalog(self.path, [{"appid": i, "name": name} for i, name in enumerate(("Portal", "Missing", "Later"), 1)])

    def rows(self):
        with closing(sqlite3.connect(self.path)) as connection:
            return connection.execute("SELECT appid,hltb_dirty,hltb_error_count,hltb_retry_after FROM steam_catalog ORDER BY appid").fetchall()

    def result(self):
        with patch.object(builder, "_run_hltb_request", return_value=[entry(20, "Portal")]):
            return builder.search_hltb_global("Portal")

    def test_matched_and_no_match_persist_with_original_data(self):
        expected = self.result()
        with patch.object(builder, "_run_hltb_request", side_effect=lambda name: [entry(20, "Portal")] if name == "Portal" else []):
            builder.update_hltb(self.path, limit=3, pause_seconds=2)
        self.assertEqual(self.rows(), [(1, 0, 0, None), (2, 0, 0, None), (3, 0, 0, None)])
        with closing(sqlite3.connect(self.path)) as connection:
            rows = connection.execute("SELECT appid,state,hltb_id,main_story,main_extra,completionist,all_styles FROM hltb_metadata ORDER BY appid").fetchall()
        self.assertEqual(rows[0], (1, "matched", expected["hltb_id"], 10.0, 15.5, 22.0, 12.0))
        self.assertEqual([row[1] for row in rows[1:]], ["no_match", "no_match"])
        self.sleep.assert_not_called()

    def test_consecutive_failures_continue_batch_with_individual_cooldowns(self):
        with patch.object(builder, "_run_hltb_request", return_value=None) as search:
            builder.update_hltb(self.path, limit=3, pause_seconds=2)
        self.assertEqual(search.call_count, 6)
        self.assertEqual([row[2] for row in self.rows()], [1, 1, 1])
        self.assertEqual([row[3] for row in self.rows()], [(self.now + timedelta(hours=1)).isoformat(timespec="seconds")] * 3)
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [5, 5, 5, 10, 5, 10])
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM hltb_metadata").fetchone()[0], 0)

    def test_success_resets_consecutive_error_pause(self):
        with patch.object(builder, "_run_hltb_request", side_effect=[None, None, [], None, None]):
            builder.update_hltb(self.path, limit=3, pause_seconds=2)
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [5, 5, 5, 5])
        self.assertEqual([row[2] for row in self.rows()], [1, 0, 1])

    def test_variant_failure_is_an_error_even_after_an_empty_first_search(self):
        db.upsert_catalog(self.path, [{"appid": 1, "name": "Game: Complete Edition"}])
        with patch.object(builder, "_run_hltb_request", side_effect=[[], None, None]) as search:
            builder.update_hltb(self.path, limit=1, pause_seconds=2)
        self.assertEqual([call.args[0] for call in search.call_args_list],
                         ["Game: Complete Edition", "Game Complete Edition", "Game Complete Edition"])
        self.assertEqual(self.rows()[0][1:3], (1, 1))
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM hltb_metadata").fetchone()[0], 0)

    def test_cooldowns_remain_one_six_and_twenty_four_hours(self):
        for count, hours in enumerate((1, 6, 24, 24), 1):
            with self.subTest(count=count):
                cooldown = db.save_hltb_error(self.path, 1, RuntimeError("fixture error"))
                self.assertEqual((cooldown["error_count"], cooldown["cooldown_hours"]), (count, hours))
                self.assertEqual(cooldown["retry_after"], (self.now + timedelta(hours=hours)).isoformat(timespec="seconds"))
                self.assertNotIn(1, [row["appid"] for row in db.hltb_candidates(self.path, 3)])
                with patch.object(db, "_now", return_value=self.now + timedelta(hours=hours, seconds=1)):
                    self.assertIn(1, [row["appid"] for row in db.hltb_candidates(self.path, 3)])

    def test_old_suspension_state_is_preserved_but_does_not_stop_processing(self):
        key = "hltb_service_retry_after"
        db.set_state(self.path, key, "9999999999")
        with patch.object(builder, "_run_hltb_request", return_value=[]):
            builder.update_hltb(self.path, limit=3, pause_seconds=2)
        self.assertEqual(self.rows(), [(1, 0, 0, None), (2, 0, 0, None), (3, 0, 0, None)])
        self.assertEqual(db.get_state(self.path, key), "9999999999")

    def test_failure_keeps_existing_cached_metadata_and_saved_progress(self):
        expected = self.result()
        db.save_hltb_match(self.path, 1, "Portal", expected)
        with closing(sqlite3.connect(self.path)) as connection:
            before = connection.execute("SELECT * FROM hltb_metadata WHERE appid=1").fetchone()
            connection.execute("UPDATE steam_catalog SET hltb_dirty=1 WHERE appid=1")
            connection.commit()
        with patch.object(builder, "_run_hltb_request", side_effect=[None, None, [], []]):
            builder.update_hltb(self.path, limit=3, pause_seconds=2)
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT * FROM hltb_metadata WHERE appid=1").fetchone(), before)
            self.assertEqual(connection.execute("SELECT appid,state FROM hltb_metadata ORDER BY appid").fetchall(),
                             [(1, "matched"), (2, "no_match"), (3, "no_match")])
        with patch.object(db, "_now", return_value=self.now + timedelta(hours=1, seconds=1)), \
             patch.object(builder, "_run_hltb_request", return_value=[]):
            builder.update_hltb(self.path, limit=3, pause_seconds=2)
        self.assertEqual(self.rows(), [(1, 0, 0, None), (2, 0, 0, None), (3, 0, 0, None)])

    def test_global_hltb_ttls_and_local_no_match_ttl_are_separate(self):
        self.assertEqual(db.HLTB_CACHE_MAX_AGE_DAYS, 180)
        self.assertEqual(db.GLOBAL_HLTB_NO_MATCH_CACHE_DAYS, 90)
        self.assertEqual(app_constants.HLTB_NO_MATCH_CACHE_DAYS, 30)
        self.assertEqual(matcher.HLTB_NO_MATCH_CACHE_DAYS, 30)
        db.save_hltb_match(self.path, 1, "Portal", self.result())
        db.save_hltb_no_match(self.path, 2, "Missing")
        for days, expected in ((29, [3]), (31, [3]), (89, [3]), (91, [2, 3]), (181, [1, 2, 3])):
            with self.subTest(days=days), patch.object(db, "_now", return_value=self.now + timedelta(days=days)):
                self.assertEqual(sorted(row["appid"] for row in db.hltb_candidates(self.path, 3)), expected)

    def test_production_arguments_reach_both_workers_without_real_requests(self):
        argv = ["builder", "--db", str(self.path), "--steam-limit", "100", "--hltb-limit", "500", "--hltb-pause", "2"]
        with patch.object(sys, "argv", argv), patch.dict(builder.os.environ, {"STEAM_WEB_API_KEY": "fixture-key"}), \
             patch.object(builder, "sync_catalog") as catalog, \
             patch.object(builder, "update_steam", return_value=None) as steam, patch.object(builder, "update_hltb", return_value=None) as hltb:
            self.assertEqual(builder.main(), 0)
        catalog.assert_called_once()
        steam.assert_called_once_with(self.path, limit=100, pause_seconds=2, reviews_limit=None,
                                      reviews_pause_seconds=2, budget_seconds=9000)
        hltb.assert_called_once_with(self.path, limit=500, pause_seconds=2)

    def test_batch_limit_of_500_leaves_remaining_game_eligible(self):
        db.upsert_catalog(self.path, [{"appid": i, "name": f"Game {i}"} for i in range(4, 502)])
        with patch.object(builder, "search_hltb_global", return_value=None) as search:
            builder.update_hltb(self.path, limit=500, pause_seconds=2)
        self.assertEqual(search.call_count, 500)
        self.assertEqual([row["appid"] for row in db.hltb_candidates(self.path, 501)], [501])


if __name__ == "__main__":
    unittest.main()
