"""Steam lane independence, partial publication and transactional failures."""
from contextlib import redirect_stdout
from datetime import timedelta
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import requests
import steam_reviews

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'global_metadata'))
import build_global_database as builder
import global_database as db

SUMMARY = dict(total_reviews=10, total_positive=8, total_negative=2,
               review_score=8, review_description='Very Positive', positive_percentage=80.0)


class SteamSchedulingTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.path = Path(scratch.name) / 'global.db'
        db.init_database(self.path)
        db.upsert_catalog(self.path, [{'appid': n, 'name': f'Game {n}'} for n in range(1, 5)])

    def run_batch(self, **kwargs):
        with redirect_stdout(io.StringIO()):
            return builder.update_steam(self.path, limit=kwargs.pop('limit', 4),
                                        pause_seconds=0, reviews_pause_seconds=0, **kwargs)

    def ready(self, appids):
        for appid in appids:
            db.save_steam_ready(self.path, {'appid': appid})

    def test_reviews_start_before_details_finish_and_have_independent_limits(self):
        reviewed = threading.Event()

        def details(appid):
            if appid == 2:
                self.assertTrue(reviewed.wait(3), 'Reviews waited for the full details batch')
            return {'appid': appid}

        def review(appid):
            reviewed.set()
            return SUMMARY

        with patch.object(builder, 'get_store_details', side_effect=details), \
             patch.object(builder.ReviewClient, 'get_summary', side_effect=review):
            report = self.run_batch(reviews_limit=1)
        self.assertEqual(report['details']['updated'], 4)
        self.assertEqual(report['reviews']['updated'], 1)
        self.assertEqual(len(db.steam_review_candidates(self.path, 10)), 3)

    def test_review_rate_limit_and_persisted_cooldown_do_not_limit_details(self):
        self.ready([1])
        response = requests.Response()
        response.status_code = 429
        response.headers['Retry-After'] = '7200'
        with patch.object(builder, 'get_store_details', side_effect=lambda n: {'appid': n}), \
             patch.object(builder.ReviewClient, 'get_summary', side_effect=requests.HTTPError(response=response)) as fetch:
            first = self.run_batch()
            db.upsert_catalog(self.path, [{'appid': 5, 'name': 'New'}])
            second = self.run_batch()
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(first['details']['updated'], 3)
        self.assertEqual(second['details']['updated'], 1)
        self.assertTrue(second['reviews']['deferred'])
        self.assertIsNone(db.get_state(self.path, 'steam_details_retry_after'))

    def test_details_rate_limit_does_not_limit_existing_reviews(self):
        self.ready([1])
        response = requests.Response()
        response.status_code = 429
        with patch.object(builder, 'get_store_details', side_effect=requests.HTTPError(response=response)), \
             patch.object(builder.ReviewClient, 'get_summary', return_value=SUMMARY):
            report = self.run_batch()
        self.assertTrue(report['details']['deferred'])
        self.assertEqual(report['reviews']['updated'], 1)
        self.assertIsNone(db.get_state(self.path, 'steam_reviews_retry_after'))

    def test_isolated_invalid_review_preserves_old_data_and_retries_later(self):
        self.ready([1, 2])
        db.save_steam_reviews(self.path, 1, SUMMARY)
        with db.connect(self.path) as conn:
            conn.execute("UPDATE steam_metadata SET reviews_fetched_at='2020-01-01' WHERE appid=1")
        # Missing reviews sort before stale reviews: app 2 succeeds, app 1 fails.
        with patch.object(builder.ReviewClient, 'get_summary', side_effect=[SUMMARY, steam_reviews.ReviewResponseError('bad summary')]):
            report = self.run_batch(limit=0, reviews_limit=2)
        self.assertEqual((report['reviews']['updated'], report['reviews']['errors']), (1, 1))
        self.assertFalse(builder.steam_run_failed(report))
        with db.connect(self.path) as conn:
            row = conn.execute('SELECT total_reviews,reviews_fetched_at FROM steam_metadata WHERE appid=1').fetchone()
        self.assertEqual(tuple(row), (10, '2020-01-01'))
        self.assertEqual(db.steam_review_candidates(self.path, 10), [])
        with patch.object(db, '_now', return_value=db._now() + timedelta(seconds=301)):
            self.assertEqual([r['appid'] for r in db.steam_review_candidates(self.path, 10)], [1])
        with db.connect(self.path) as conn:
            conn.execute('UPDATE steam_fetch_retries SET retry_after=0')
        with patch.object(builder.ReviewClient, 'get_summary', return_value=SUMMARY):
            self.assertEqual(self.run_batch(limit=0, reviews_limit=2)['reviews']['updated'], 1)
        with db.connect(self.path) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM steam_fetch_retries').fetchone()[0], 0)

    def test_zero_time_budget_sends_no_requests_and_leaves_work_due(self):
        with patch.object(builder, 'get_store_details') as details, \
             patch.object(builder.ReviewClient, 'get_summary') as reviews:
            report = self.run_batch(budget_seconds=0)
        details.assert_not_called()
        reviews.assert_not_called()
        self.assertTrue(report['details']['budget_exhausted'])
        self.assertEqual(len(db.steam_details_candidates(self.path, 10)), 4)

    def test_fatal_contract_and_access_errors_are_not_publishable(self):
        self.ready([1, 2, 3, 4])
        denied = requests.Response()
        denied.status_code = 403
        for failure, expected_calls in ((steam_reviews.ReviewResponseError('invalid contract'), 3),
                                        (requests.HTTPError(response=denied), 1)):
            with self.subTest(failure=failure):
                with db.connect(self.path) as conn:
                    conn.execute('DELETE FROM steam_fetch_retries')
                with patch.object(builder.ReviewClient, 'get_summary', side_effect=failure) as fetch:
                    report = self.run_batch(limit=0, reviews_limit=4)
                self.assertTrue(builder.steam_run_failed(report))
                self.assertEqual(fetch.call_count, expected_calls)

    def test_database_failure_blocks_publication_and_is_reported(self):
        report_path = self.path.with_suffix('.json')
        with patch.object(sys, 'argv', ['builder', '--db', str(self.path), '--skip-catalog',
                                      '--steam-limit', '1', '--steam-reviews-limit', '0',
                                      '--hltb-limit', '0', '--report', str(report_path)]), \
             patch.object(builder, 'get_store_details', return_value={'appid': 1}), \
             patch.object(builder, 'save_steam_ready', side_effect=sqlite3.DatabaseError('corrupt fixture')), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(builder.main(), 1)
        report = json.loads(report_path.read_text())
        self.assertEqual(report['status'], 'fatal')
        self.assertFalse(report['publishable'])
        self.assertIn('corrupt fixture', report['steam']['details']['fatal_error'])

    def test_repeated_malformed_details_are_fatal_and_remain_pending(self):
        with patch.object(builder, 'get_store_details', side_effect=ValueError('invalid Store JSON')) as fetch:
            report = self.run_batch(reviews_limit=0)
        self.assertEqual(fetch.call_count, 3)
        self.assertTrue(builder.steam_run_failed(report))
        self.assertEqual(report['details']['unavailable'], 0)
        with db.connect(self.path) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM steam_metadata').fetchone()[0], 0)

    def test_isolated_detail_error_does_not_discard_other_valid_details(self):
        with patch.object(builder, 'get_store_details', side_effect=[ValueError('bad app'), {'appid': 2}, {'appid': 3}, {'appid': 4}]):
            report = self.run_batch(reviews_limit=0)
        self.assertFalse(builder.steam_run_failed(report))
        self.assertTrue(builder.steam_run_partial(report))
        self.assertEqual((report['details']['updated'], report['details']['errors']), (3, 1))
        with db.connect(self.path) as conn:
            self.assertEqual(conn.execute('SELECT steam_dirty FROM steam_catalog WHERE appid=1').fetchone()[0], 1)

    def test_detail_transaction_rolls_back_if_dirty_flag_update_fails(self):
        with db.connect(self.path) as conn:
            conn.execute("""CREATE TRIGGER reject_dirty BEFORE UPDATE OF steam_dirty ON steam_catalog
                         BEGIN SELECT RAISE(ABORT, 'fixture write failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            db.save_steam_ready(self.path, {'appid': 1})
        with db.connect(self.path) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM steam_metadata').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT steam_dirty FROM steam_catalog WHERE appid=1').fetchone()[0], 1)

    def test_review_counter_counts_committed_rows_only(self):
        self.ready([1])

        def review(appid):
            db.save_steam_unavailable(self.path, appid)
            return SUMMARY

        with patch.object(builder.ReviewClient, 'get_summary', side_effect=review):
            report = self.run_batch(limit=0, reviews_limit=1)
        self.assertEqual(report['reviews']['updated'], 0)
        self.assertEqual(report['reviews']['skipped'], 1)

    def test_legacy_review_cooldown_still_allows_details(self):
        import time
        db.set_state(self.path, 'steam_service_retry_after', time.time() + 3600)
        with patch.object(builder, 'get_store_details', side_effect=lambda n: {'appid': n}), \
             patch.object(builder.ReviewClient, 'get_summary') as reviews:
            report = self.run_batch()
        self.assertEqual(report['details']['updated'], 4)
        self.assertTrue(report['reviews']['deferred'])
        reviews.assert_not_called()
