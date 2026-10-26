"""Global-only review rotation and durable AppID cooldowns on temporary SQLite."""
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

import requests
import steam_reviews

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'global_metadata'))
import build_global_database as builder
import global_database as db


SUMMARY = dict(total_reviews=10, total_positive=8, total_negative=2,
               review_score=8, review_description='Very Positive', positive_percentage=80.0)
PAYLOAD = json.loads((Path(__file__).parent / 'fixtures/steam_reviews_portal2.json').read_text())


class SteamReviewRotationTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.path = Path(scratch.name) / 'global.db'
        self.now = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
        for p in (patch.object(db, '_now', side_effect=lambda: self.now),
                  patch.object(builder.time, 'time', side_effect=lambda: self.now.timestamp()),
                  patch('requests.adapters.HTTPAdapter.send', side_effect=AssertionError('Real HTTP forbidden'))):
            p.start()
            self.addCleanup(p.stop)
        db.init_database(self.path)

    def seed(self, count):
        db.upsert_catalog(self.path, [{'appid': n, 'name': f'Game {n}'} for n in range(1, count + 1)])
        for appid in range(1, count + 1):
            db.save_steam_ready(self.path, {'appid': appid, 'genres': ['Old genre']})
            db.save_steam_reviews(self.path, appid, SUMMARY)
        with db.connect(self.path) as conn:
            conn.execute("UPDATE steam_metadata SET fetched_at='2020-01-01', reviews_fetched_at='2020-01-01'")

    def run_batch(self, limit=100, details_limit=0):
        with redirect_stdout(io.StringIO()):
            return builder.update_steam(self.path, limit=details_limit, pause_seconds=0,
                                        reviews_limit=limit, reviews_pause_seconds=0)

    @staticmethod
    def response(status=200, payload=PAYLOAD, headers=None):
        result = requests.Response()
        result.status_code = status
        result.url = steam_reviews.REVIEWS_URL
        result.headers.update(headers or {})
        result._content = json.dumps(payload).encode()
        return result

    def rows(self, table='steam_metadata'):
        with db.connect(self.path) as conn:
            return [tuple(row) for row in conn.execute(f'SELECT * FROM {table} ORDER BY appid')]

    def test_one_invalid_app_in_100_continues_and_retries_only_after_48_hours(self):
        self.seed(100)
        db.save_hltb_match(self.path, 1, 'Game 1', {'hltb_id': 99, 'main_story': 10})
        before = self.rows()[0]
        hltb = self.rows('hltb_metadata')
        calls = []

        def get(*args, **kwargs):
            appid = json.loads(kwargs['params']['input_json'])['appid']
            calls.append(appid)
            return self.response(payload={'response': {}}) if appid == 1 else self.response()

        with patch.object(steam_reviews.requests, 'get', side_effect=get):
            report = self.run_batch()
        lane = report['reviews']
        self.assertEqual(calls, list(range(1, 101)))
        self.assertEqual((lane['eligible'], lane['attempted'], lane['individual_errors'],
                          lane['updated'], lane['unused_capacity']), (100, 100, 1, 99, 0))
        self.assertFalse(lane['deferred'])
        self.assertTrue(builder.steam_run_partial(report))
        self.assertFalse(builder.steam_run_failed(report))
        self.assertEqual(self.rows()[0], before)
        self.assertEqual(self.rows('hltb_metadata'), hltb)
        with db.connect(self.path) as conn:
            retry = conn.execute("SELECT retry_after,last_error FROM steam_fetch_retries WHERE appid=1 AND service='reviews'").fetchone()
            self.assertEqual(retry['retry_after'], self.now.timestamp() + 48 * 3600)
            self.assertIn('ReviewResponseError', retry['last_error'])
            self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])

        # Reopen the database and start a new client: cooldown must survive runs.
        db.init_database(self.path)
        with patch.object(builder, 'get_store_details', side_effect=lambda n: {'appid': n}) as details, \
             patch.object(steam_reviews.requests, 'get') as fetch:
            blocked = self.run_batch(details_limit=100)['reviews']
        self.assertIn(1, [call.args[0] for call in details.call_args_list])
        fetch.assert_not_called()
        self.assertEqual((blocked['eligible'], blocked['cooldown_excluded'],
                          blocked['min_age_excluded'], blocked['unused_capacity']), (0, 1, 99, 100))
        with db.connect(self.path) as conn:
            self.assertEqual(tuple(conn.execute('SELECT total_reviews,reviews_fetched_at FROM steam_metadata WHERE appid=1').fetchone()),
                             (10, '2020-01-01'))
        self.now += timedelta(hours=48, seconds=-1)
        with patch.object(steam_reviews.requests, 'get') as fetch:
            self.assertEqual(self.run_batch()['reviews']['attempted'], 0)
        fetch.assert_not_called()
        self.now += timedelta(seconds=1)
        with patch.object(steam_reviews.requests, 'get', return_value=self.response()) as fetch:
            resumed = self.run_batch()['reviews']
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(json.loads(fetch.call_args.kwargs['params']['input_json'])['appid'], 1)
        self.assertEqual((resumed['eligible'], resumed['updated'], resumed['unused_capacity']), (1, 1, 99))
        with db.connect(self.path) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM steam_fetch_retries').fetchone()[0], 0)

    def test_valid_oldest_summaries_fill_100_slots_then_rotate_without_repeats(self):
        self.seed(130)
        timestamps = {}
        for appid in range(1, 131):
            if appid == 130:
                timestamps[appid] = None
            elif appid >= 127:
                timestamps[appid] = (self.now - timedelta(days=120, hours=appid)).isoformat()
            elif appid <= 10:
                timestamps[appid] = (self.now - timedelta(days=2)).isoformat()
            else:
                timestamps[appid] = (self.now - timedelta(days=8, hours=appid)).isoformat()
        with db.connect(self.path) as conn:
            conn.executemany('UPDATE steam_metadata SET reviews_fetched_at=? WHERE appid=?',
                             [(value, appid) for appid, value in timestamps.items()])
        oldest = sorted(range(11, 131), key=lambda appid: (timestamps[appid] or '', appid))
        calls = []

        def review(appid):
            calls.append(appid)
            return SUMMARY

        with patch.object(builder.ReviewClient, 'get_summary', side_effect=review):
            first = self.run_batch()['reviews']
            self.assertEqual(calls, oldest[:100])
            self.assertEqual((first['eligible'], first['min_age_excluded'], first['updated'],
                              first['unused_capacity']), (120, 10, 100, 0))
            second = self.run_batch()['reviews']
            self.assertEqual(calls, oldest)
            self.assertEqual((second['eligible'], second['updated'], second['unused_capacity']), (20, 20, 80))
            self.assertEqual(self.run_batch()['reviews']['attempted'], 0)
        self.assertEqual(len(calls), len(set(calls)))

    def test_minimum_age_boundary_and_disjoint_selection_counters(self):
        self.seed(7)
        cutoff = self.now - timedelta(days=db.GLOBAL_STEAM_REVIEWS_MIN_AGE_DAYS)
        with db.connect(self.path) as conn:
            conn.execute('UPDATE steam_metadata SET reviews_fetched_at=? WHERE appid=1', ((cutoff + timedelta(seconds=1)).isoformat(),))
            conn.execute('UPDATE steam_metadata SET reviews_fetched_at=? WHERE appid=2', (cutoff.isoformat(),))
            conn.execute('UPDATE steam_metadata SET reviews_fetched_at=? WHERE appid=3', ((cutoff - timedelta(seconds=1)).isoformat(),))
            conn.execute("UPDATE steam_metadata SET state='unavailable' WHERE appid=7")
        db.save_steam_error(self.path, 4, 'reviews', ValueError('cooldown'), 48 * 3600)
        db.save_steam_error(self.path, 5, 'details', ValueError('Store only'), 48 * 3600)
        candidates = db.steam_review_candidates(self.path, 100)
        self.assertEqual([r['appid'] for r in candidates], [5, 6, 3, 2])
        self.assertEqual(db.steam_review_selection_counts(self.path),
                         dict(eligible=4, cooldown_excluded=1, min_age_excluded=1))
        self.assertEqual(db.steam_review_selection_counts(self.path, exclude_appids={2, 4, 5}),
                         dict(eligible=2, cooldown_excluded=0, min_age_excluded=1))

    def test_429_persists_service_delay_and_preserves_all_reviews(self):
        self.seed(100)
        before = self.rows()
        with patch.object(steam_reviews.requests, 'get', return_value=self.response(
                status=429, headers={'Retry-After': '259200'})) as fetch:
            first = self.run_batch()['reviews']
            self.now += timedelta(hours=48)
            second = self.run_batch()['reviews']
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual((first['errors'], first['individual_errors'], first['unused_capacity']), (1, 0, 99))
        self.assertTrue(first['deferred'])
        self.assertTrue(second['deferred'])
        self.assertEqual(second['unused_capacity'], 100)
        self.assertGreater(float(db.get_state(self.path, 'steam_reviews_retry_after')), self.now.timestamp())
        self.assertEqual(self.rows(), before)
        self.now += timedelta(hours=24)
        with patch.object(steam_reviews.requests, 'get', return_value=self.response()) as fetch:
            self.assertEqual(self.run_batch()['reviews']['updated'], 100)
        self.assertEqual(fetch.call_count, 100)

    def test_database_error_recording_review_failure_is_fatal_and_preserves_data(self):
        self.seed(3)
        before = self.rows()
        with patch.object(builder.ReviewClient, 'get_summary', side_effect=steam_reviews.ReviewResponseError('bad app')), \
             patch.object(builder, 'save_steam_error', side_effect=sqlite3.IntegrityError('retry write failed')):
            report = self.run_batch()
        self.assertTrue(builder.steam_run_failed(report))
        self.assertIn('retry write failed', report['reviews']['fatal_error'])
        self.assertEqual(report['reviews']['attempted'], 1)
        self.assertEqual(self.rows(), before)
