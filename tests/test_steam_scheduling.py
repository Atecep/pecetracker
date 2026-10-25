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
from unittest.mock import Mock, patch

import requests
import steam_reviews

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'global_metadata'))
import build_global_database as builder
import global_database as db
import steam_fetcher

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

    def test_repeated_invalid_summaries_and_access_errors_remain_publishable(self):
        self.ready([1, 2, 3, 4])
        denied = requests.Response()
        denied.status_code = 403
        for failure in (steam_reviews.ReviewResponseError('invalid contract'),
                        requests.HTTPError(response=denied), ValueError('invalid JSON')):
            with self.subTest(failure=failure):
                with db.connect(self.path) as conn:
                    conn.execute('DELETE FROM steam_fetch_retries')
                with patch.object(builder.ReviewClient, 'get_summary', side_effect=failure) as fetch:
                    report = self.run_batch(limit=0, reviews_limit=4)
                self.assertFalse(builder.steam_run_failed(report))
                self.assertTrue(builder.steam_run_partial(report))
                self.assertEqual(fetch.call_count, 4)
                self.assertEqual(report['reviews']['errors'], 4)
                self.assertEqual(report['reviews']['pending'], 4)

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

    def test_repeated_malformed_details_remain_pending_and_publishable(self):
        with patch.object(builder, 'get_store_details', side_effect=ValueError('invalid Store JSON')) as fetch:
            report = self.run_batch(reviews_limit=0)
        self.assertEqual(fetch.call_count, 4)
        self.assertFalse(builder.steam_run_failed(report))
        self.assertTrue(builder.steam_run_partial(report))
        self.assertEqual(report['details']['pending'], 4)
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

    def test_2500_requests_with_400_sequential_failures_publish_2100_per_lane(self):
        db.upsert_catalog(self.path, [{'appid': n, 'name': f'Game {n}'} for n in range(1, 2501)])
        # Seed valid old data to prove a failed refresh preserves the full row.
        with db.connect(self.path) as conn:
            conn.execute("""INSERT INTO steam_metadata
                (appid,state,genres,total_reviews,reviews_fetched_at,fetched_at)
                SELECT appid,'ready','["Old genre"]',17,'2020-01-01','2020-01-01'
                FROM steam_catalog""")

        def details(appid):
            if appid <= 400:
                raise ValueError('invalid Store JSON')
            return {'appid': appid, 'genres': ['New genre']}

        def reviews(appid):
            if appid <= 400:
                raise steam_reviews.ReviewResponseError('invalid reviews summary')
            return SUMMARY

        report_path = self.path.with_suffix('.json')
        with patch.object(sys, 'argv', ['builder', '--db', str(self.path), '--skip-catalog',
                                       '--steam-limit', '2500', '--steam-reviews-limit', '2500',
                                       '--steam-pause', '0', '--steam-reviews-pause', '0',
                                       '--hltb-limit', '0', '--report', str(report_path)]), \
             patch.object(builder, 'get_store_details', side_effect=details) as store, \
             patch.object(builder.ReviewClient, 'get_summary', side_effect=reviews) as review, \
             redirect_stdout(io.StringIO()):
            self.assertEqual(builder.main(), 0)
        report = json.loads(report_path.read_text())
        self.assertTrue(report['publishable'])
        self.assertEqual(report['status'], 'partial')
        self.assertEqual((store.call_count, review.call_count), (2500, 2500))
        for lane in report['steam'].values():
            self.assertEqual((lane['selected'], lane['attempted'], lane['completed'],
                              lane['errors'], lane['pending'], lane['updated']),
                             (2500, 2500, 2100, 400, 400, 2100))
            self.assertIsNone(lane['fatal_error'])
        with db.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM steam_metadata WHERE genres='[\"New genre\"]' AND total_reviews=10").fetchone()[0], 2100)
            self.assertEqual(conn.execute("SELECT count(*) FROM steam_metadata WHERE genres='[\"Old genre\"]' AND total_reviews=17 AND fetched_at='2020-01-01' AND reviews_fetched_at='2020-01-01'").fetchone()[0], 400)
            self.assertEqual(conn.execute('SELECT count(*) FROM steam_fetch_retries').fetchone()[0], 800)
            self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
        with patch.object(db, '_now', return_value=db._now() + timedelta(seconds=601)):
            for candidates in (db.steam_details_candidates, db.steam_review_candidates):
                self.assertEqual([r['appid'] for r in candidates(self.path, 2500)], list(range(1, 401)))

    def test_review_is_not_retried_in_same_run_even_after_its_cooldown_expires(self):
        self.ready([1, 2])
        done = Mock()
        done.is_set.side_effect = [False, False, True]

        def expire_retry(*args):
            with db.connect(self.path) as conn:
                conn.execute('UPDATE steam_fetch_retries SET retry_after=0')

        done.wait.side_effect = expire_retry
        report = builder._lane_report(3)
        with patch.object(builder.ReviewClient, 'get_summary', side_effect=[
                steam_reviews.ReviewResponseError('bad app'), SUMMARY]) as fetch, \
             redirect_stdout(io.StringIO()):
            builder._update_reviews(self.path, report, 0, builder.time.monotonic() + 60, done)
        self.assertEqual([call.args[0] for call in fetch.call_args_list], [1, 2])
        self.assertEqual((report['selected'], report['errors'], report['updated']), (2, 1, 1))
        self.assertEqual([row['appid'] for row in db.steam_review_candidates(self.path, 10)], [1])

    def test_real_reviews_client_keeps_exact_two_second_spacing_after_invalid_responses(self):
        self.ready([1, 2, 3, 4])
        clock = [100.0]
        starts = []
        valid = json.loads((Path(__file__).parent / 'fixtures/steam_reviews_portal2.json').read_text())

        def get(*args, **kwargs):
            starts.append(clock[0])
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps({} if len(starts) <= 3 else valid).encode()
            return response

        with patch.object(steam_reviews.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(steam_reviews.time, 'sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)), \
             patch.object(steam_reviews.requests, 'get', side_effect=get):
            with redirect_stdout(io.StringIO()):
                report = builder.update_steam(self.path, limit=0, pause_seconds=0, reviews_limit=4)
        self.assertEqual(starts, [100, 102, 104, 106])
        self.assertFalse(builder.steam_run_failed(report))
        self.assertEqual((report['reviews']['errors'], report['reviews']['updated']), (3, 1))

    def test_transport_and_http_errors_defer_only_the_affected_lane_and_publish_progress(self):
        self.ready([1, 2, 3, 4])
        for service in ('details', 'reviews'):
            for failure in (requests.Timeout('timeout'), requests.ConnectionError('connection'),
                            *[requests.HTTPError(response=self.http_response(code)) for code in (429, 500, 503)]):
                with self.subTest(service=service, failure=failure):
                    with db.connect(self.path) as conn:
                        conn.execute('DELETE FROM steam_fetch_retries')
                        conn.execute('DELETE FROM updater_state')
                        conn.execute('UPDATE steam_catalog SET steam_dirty=1')
                        conn.execute('UPDATE steam_metadata SET reviews_fetched_at=NULL')
                    with patch.object(builder, 'get_store_details',
                                      side_effect=[{'appid': 1}, failure] if service == 'details' else lambda n: {'appid': n}), \
                         patch.object(builder.ReviewClient, 'get_summary',
                                      side_effect=[SUMMARY, failure] if service == 'reviews' else None,
                                      return_value=SUMMARY):
                        report = self.run_batch()
                    self.assertFalse(builder.steam_run_failed(report))
                    lane = report[service]
                    self.assertEqual((lane['updated'], lane['errors'], lane['pending'], lane['not_attempted']), (1, 1, 3, 2))
                    self.assertTrue(lane['deferred'])
                    other = report['reviews' if service == 'details' else 'details']
                    self.assertEqual(other['updated'], 4)

    @staticmethod
    def http_response(code):
        response = requests.Response()
        response.status_code = code
        return response

    def test_invalid_store_payloads_preserve_existing_details_and_never_mark_unavailable(self):
        self.ready([1])
        with db.connect(self.path) as conn:
            conn.execute("UPDATE steam_metadata SET genres='[\"Old\"]',fetched_at='2020-01-01' WHERE appid=1")
        valid_data = {'steam_appid': 1, 'name': 'Game 1'}
        invalid = [[], {}, {'other': {'success': False}}, {'1': {}},
                   {'1': {'success': True}}, {'1': {'success': True, 'data': {}}},
                   {'1': {'success': True, 'data': []}},
                   *[{'1': {'success': True, 'data': {**valid_data, **bad}}} for bad in (
                       {'steam_appid': None}, {'steam_appid': 99}, {'genres': [None]},
                       {'genres': [{'description': []}]}, {'categories': 'wrong'},
                       {'developers': {}}, {'release_date': 'wrong'}, {'release_date': []},
                       {'release_date': {'date': []}}, {'is_free': 'false'})]]
        for payload in invalid:
            with self.subTest(payload=payload):
                with db.connect(self.path) as conn:
                    conn.execute('DELETE FROM steam_fetch_retries')
                response = self.http_response(200)
                response._content = json.dumps(payload).encode()
                with patch.object(steam_fetcher.requests, 'get', return_value=response):
                    report = self.run_batch(limit=1, reviews_limit=0)
                self.assertFalse(builder.steam_run_failed(report))
                self.assertEqual((report['details']['errors'], report['details']['unavailable']), (1, 0))
                with db.connect(self.path) as conn:
                    self.assertEqual(tuple(conn.execute("SELECT state,genres,fetched_at FROM steam_metadata WHERE appid=1").fetchone()),
                                     ('ready', '["Old"]', '2020-01-01'))

    def test_store_unavailable_requires_explicit_negative_in_both_markets(self):
        response = self.http_response(200)
        response._content = b'{"1":{"success":false}}'
        with patch.object(steam_fetcher.requests, 'get', return_value=response) as fetch:
            report = self.run_batch(limit=1, reviews_limit=0)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(report['details']['unavailable'], 1)
        self.assertEqual(report['details']['errors'], 0)

    def test_valid_store_fallback_is_still_saved_with_the_original_fields(self):
        unavailable = self.http_response(200)
        unavailable._content = b'{"1":{"success":false}}'
        valid = self.http_response(200)
        valid._content = json.dumps({'1': {'success': True, 'data': {
            'steam_appid': 1, 'name': 'Game 1', 'genres': [{'description': 'Adventure'}],
            'categories': [{'description': 'Single-player'}], 'developers': ['Developer'],
            'publishers': ['Publisher'], 'release_date': {'date': '1 Jan, 2020'},
            'is_free': False}}}).encode()
        with patch.object(steam_fetcher.requests, 'get', side_effect=[unavailable, valid]) as fetch:
            report = self.run_batch(limit=1, reviews_limit=0)
        self.assertEqual(fetch.call_args_list[1].kwargs['params']['cc'], 'us')
        self.assertEqual((report['details']['updated'], report['details']['errors']), (1, 0))
        with db.connect(self.path) as conn:
            row = conn.execute('SELECT genres,categories,developers,publishers,release_date,is_free FROM steam_metadata WHERE appid=1').fetchone()
        self.assertEqual(tuple(row), ('["Adventure"]', '["Single-player"]', '["Developer"]',
                                     '["Publisher"]', '1 Jan, 2020', 0))

    def test_hltb_failures_are_partial_but_persistence_errors_are_fatal(self):
        report_path = self.path.with_suffix('.json')
        db.save_hltb_match(self.path, 1, 'Game 1', {'hltb_id': 99, 'main_story': 10})
        with db.connect(self.path) as conn:
            conn.execute('UPDATE steam_catalog SET hltb_dirty=1')
            before = tuple(conn.execute('SELECT * FROM hltb_metadata WHERE appid=1').fetchone())
        argv = ['builder', '--db', str(self.path), '--skip-catalog', '--steam-limit', '0',
                '--hltb-limit', '4', '--report', str(report_path)]
        with patch.object(sys, 'argv', argv), \
             patch.object(builder, 'search_hltb_global', side_effect=[requests.Timeout('timeout'),
                  ValueError('invalid JSON'), RuntimeError('request failed'), {'hltb_id': 4}]), \
             patch.object(builder.time, 'sleep'), redirect_stdout(io.StringIO()):
            self.assertEqual(builder.main(), 0)
        report = json.loads(report_path.read_text())
        self.assertEqual(report['status'], 'partial')
        self.assertTrue(report['publishable'])
        self.assertEqual((report['hltb']['completed'], report['hltb']['errors'], report['hltb']['pending']), (1, 3, 3))
        with db.connect(self.path) as conn:
            self.assertEqual(tuple(conn.execute('SELECT * FROM hltb_metadata WHERE appid=1').fetchone()), before)
            self.assertEqual(conn.execute("SELECT count(*) FROM hltb_metadata WHERE state='no_match'").fetchone()[0], 0)
            conn.execute('UPDATE steam_catalog SET hltb_retry_after=NULL')
        with patch.object(sys, 'argv', argv), \
             patch.object(builder, 'search_hltb_global', return_value=None), \
             patch.object(builder, 'save_hltb_no_match', side_effect=sqlite3.IntegrityError('invalid transaction')), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(builder.main(), 1)
        report = json.loads(report_path.read_text())
        self.assertEqual(report['status'], 'fatal')
        self.assertFalse(report['publishable'])
        self.assertEqual(report['hltb']['errors'], 0)
        self.assertIn('invalid transaction', report['hltb']['fatal_error'])
