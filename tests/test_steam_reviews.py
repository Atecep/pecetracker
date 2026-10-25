"""Official review contract, anonymous throttling and cache integrity."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import requests
import database as local_db
import metadata_service
import metadata_background
import steam_reviews as reviews
import steam_store

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'global_metadata'))
import steam_fetcher
import global_database as global_db
import build_global_database as builder

FIXTURES = Path(__file__).parent / 'fixtures'
REAL = json.loads((FIXTURES / 'steam_reviews_portal2.json').read_text())
EMPTY = json.loads((FIXTURES / 'steam_reviews_unavailable.json').read_text())


def response(payload=REAL, status=200, headers=None):
    result = requests.Response()
    result.status_code = status
    result.url = reviews.REVIEWS_URL
    result.headers.update(headers or {})
    result._content = json.dumps(payload).encode()
    return result


class OfficialReviewTests(unittest.TestCase):
    def setUp(self):
        self.clock = [100.0]
        self.client = reviews.ReviewClient()
        for p in (patch.object(reviews, '_client', self.client),
                  patch.object(reviews.time, 'monotonic', side_effect=lambda: self.clock[0]),
                  patch.object(reviews.time, 'sleep', side_effect=lambda n: self.clock.__setitem__(0, self.clock[0] + n))):
            p.start(); self.addCleanup(p.stop)

    def test_both_paths_use_real_wrapped_summary_and_identical_anonymous_filters(self):
        with patch.object(reviews.requests, 'get', return_value=response()) as get, patch.dict('os.environ', {'STEAM_WEB_API_KEY':'ordinary-not-publisher'}):
            self.assertEqual(steam_store.get_review_summary(620), steam_fetcher.get_review_summary(620))
        result = reviews.parse_summary(REAL)
        self.assertEqual(result['total_reviews'], 468841)
        self.assertEqual(result['positive_percentage'], 98.7)
        self.assertEqual(result['review_description'], 'Overwhelmingly Positive')
        self.assertEqual(self.clock[0], 110)
        for call in get.call_args_list:
            self.assertEqual(call.args, (reviews.REVIEWS_URL,))
            self.assertEqual(set(call.kwargs['params']), {'input_json'})
            q = json.loads(call.kwargs['params']['input_json'])
            self.assertEqual(q, {'appid':620,'filter':0,'languages':['all'],'review_type':0,
                                'purchase_type':1,'num_per_page':1,'cursor':'*',
                                'display_language':'english','filter_offtopic_activity':True})
            self.assertEqual(call.kwargs['timeout'], 15)
            self.assertNotIn('ordinary-not-publisher', str(call))

    def test_explicit_zero_is_success_but_missing_summary_is_not_zero(self):
        zero = deepcopy(REAL)
        zero['response']['query_summary'].update(total_positive=0,total_negative=0,total_reviews=0,review_score=0,review_score_desc='No user reviews')
        with patch.object(reviews.requests, 'get', return_value=response(zero)):
            value = self.client.get_summary(1)
        self.assertEqual(value['total_reviews'], 0)
        self.assertIsNone(value['positive_percentage'])
        with self.assertRaises(reviews.ReviewResponseError): reviews.parse_summary(EMPTY)

    def test_malformed_summaries_are_rejected_instead_of_refreshing_zero(self):
        variants = [None, [], {}, {'query_summary':REAL['response']['query_summary']}, EMPTY]
        for field, value in [('total_reviews',None),('total_positive','5'),('total_negative',-1),('total_reviews',True),('total_reviews',1),('review_score',None),('review_score_desc',None)]:
            p = deepcopy(REAL); p['response']['query_summary'][field] = value; variants.append(p)
        for payload in variants:
            with self.subTest(payload=payload), self.assertRaises(reviews.ReviewResponseError):
                reviews.parse_summary(payload)

    def test_http_json_and_api_errors_open_circuit_without_retry_bursts(self):
        invalid = response(); invalid._content = b'<html>upstream unavailable</html>'
        failures = [response(status=429, headers={'Retry-After':'7200'}), response(status=503),
                    response(status=403), response(EMPTY), invalid,
                    response(headers={'x-eresult':'2'}), requests.Timeout('fixture timeout')]
        for failure in failures:
            client = reviews.ReviewClient()
            with self.subTest(failure=failure), patch.object(reviews.requests,'get',side_effect=[failure] if isinstance(failure,Exception) else None,return_value=failure) as get:
                with self.assertRaises(requests.RequestException): client.get_summary(1)
                with self.assertRaises(reviews.ReviewBackoff) as blocked: client.get_summary(2)
                self.assertGreaterEqual(blocked.exception.retry_after_seconds, 300)
                self.assertEqual(get.call_count, 1)
                if getattr(failure,'status_code',None)==429:
                    self.assertEqual(blocked.exception.retry_after_seconds,7200)

    def test_retry_after_date_is_honoured(self):
        now=datetime(2026,10,23,tzinfo=timezone.utc)
        r=response(status=429,headers={'Retry-After':format_datetime(now+timedelta(hours=2),usegmt=True)})
        with patch.object(reviews.time,'time',return_value=now.timestamp()):
            self.assertEqual(reviews.retry_delay(requests.HTTPError(response=r)),7200)

    def test_client_resumes_after_cooldown_without_an_immediate_retry(self):
        with patch.object(reviews.requests,'get',side_effect=[response(status=429),response()]) as get:
            with self.assertRaises(requests.HTTPError):self.client.get_summary(1)
            with self.assertRaises(reviews.ReviewBackoff):self.client.get_summary(2)
            self.clock[0] += 901
            self.assertEqual(self.client.get_summary(2)['total_reviews'],468841)
        self.assertEqual(get.call_count,2)

    def test_parallel_calls_cannot_bypass_anonymous_spacing(self):
        starts=[]
        def get(*a, **kw):
            starts.append(self.clock[0]); return response()
        with patch.object(reviews.requests,'get',side_effect=get), ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(self.client.get_summary,(1,2,3)))
        self.assertEqual(starts,[100,110,120])

    def test_parallel_rate_limited_calls_send_only_one_request(self):
        def attempt(appid):
            try:
                self.client.get_summary(appid)
            except requests.RequestException:
                return
            self.fail('Rate-limited call unexpectedly succeeded')
        with patch.object(reviews.requests,'get',return_value=response(status=429)) as get, ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(attempt,(1,2,3)))
        self.assertEqual(get.call_count,1)

    def test_builder_interval_is_configurable_without_changing_desktop_default(self):
        client = reviews.ReviewClient(interval=2, circuit_on_invalid=False)
        with patch.object(reviews.requests, 'get', return_value=response()):
            client.get_summary(1)
            client.get_summary(2)
        self.assertEqual(self.clock[0], 102)
        self.assertEqual(reviews.ReviewClient().interval, 10)

    def test_builder_isolated_invalid_summary_does_not_open_shared_circuit(self):
        client = reviews.ReviewClient(interval=2, circuit_on_invalid=False)
        with patch.object(reviews.requests, 'get', side_effect=[response(EMPTY), response()]) as get:
            with self.assertRaises(reviews.ReviewResponseError):
                client.get_summary(1)
            self.assertEqual(client.get_summary(2)['total_reviews'], 468841)
        self.assertEqual(get.call_count, 2)

    def test_builder_transient_failures_do_not_open_hidden_client_circuit(self):
        for failure in (requests.Timeout('timeout'), requests.ConnectionError('connection'),
                        response(status=502)):
            client = reviews.ReviewClient(interval=2, circuit_on_invalid=False)
            with self.subTest(failure=failure), patch.object(reviews.requests, 'get',
                                                           side_effect=[failure, response()]) as get:
                with self.assertRaises(requests.RequestException):
                    client.get_summary(1)
                self.assertEqual(client.get_summary(2)['total_reviews'], 468841)
                self.assertEqual(get.call_count, 2)

    def test_builder_always_obeys_explicit_service_backoff(self):
        now = datetime(2026, 10, 23, tzinfo=timezone.utc)
        retry_date = format_datetime(now + timedelta(hours=2), usegmt=True)
        for failure in (response(status=429, headers={'Retry-After': '7200'}),
                        response(status=502, headers={'Retry-After': retry_date}),
                        response(EMPTY, headers={'Retry-After': '7200'})):
            client = reviews.ReviewClient(interval=2, circuit_on_invalid=False)
            with self.subTest(failure=failure), patch.object(reviews.time, 'time', return_value=now.timestamp()), \
                 patch.object(reviews.requests, 'get', return_value=failure) as get:
                with self.assertRaises(requests.RequestException):
                    client.get_summary(1)
                with self.assertRaises(reviews.ReviewBackoff) as blocked:
                    client.get_summary(2)
                self.assertEqual(blocked.exception.retry_after_seconds, 7200)
                self.assertEqual(get.call_count, 1)

    def test_invalid_retry_after_does_not_turn_app_failure_into_service_failure(self):
        for value in ('invalid', 'nan', 'inf', '-10'):
            error = requests.HTTPError(response=response(status=403, headers={'Retry-After': value}))
            with self.subTest(value=value):
                self.assertFalse(reviews.is_service_failure(error))
                self.assertFalse(reviews.is_transient_failure(error))
                self.assertEqual(reviews.retry_delay(error), 300)


class AcquisitionIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); self.gp=self.root/'global.db'
        for p in (patch.object(local_db,'DATABASE_PATH',self.root/'local.db'),
                  patch.object(local_db,'ensure_data_dir'),
                  patch.object(reviews,'_client',reviews.ReviewClient())):
            p.start(); self.addCleanup(p.stop)
        local_db.init_database(); global_db.init_database(self.gp)
        global_db.upsert_catalog(self.gp,[{'appid':1,'name':'fixture'},{'appid':2,'name':'other'}])

    def seed(self):
        summary=reviews.parse_summary(REAL)
        local_db.save_game_metadata({'appid':1,'reviews':summary})
        global_db.save_steam_ready(self.gp,{'appid':1})
        global_db.save_steam_reviews(self.gp,1,summary)
        with local_db.get_connection() as c:
            c.execute("UPDATE game_metadata SET reviews_fetched_at='2020-01-01T00:00:00'")
        with global_db.connect(self.gp) as c:
            c.execute("UPDATE steam_metadata SET reviews_fetched_at='2020-01-01T00:00:00'")

    def test_desktop_actual_acquisition_and_persistence_use_new_response(self):
        with patch.object(steam_store,'get_store_details',return_value={'appid':1}), patch.object(reviews.requests,'get',return_value=response()) as get:
            value=metadata_service.get_game_metadata(1)
        self.assertEqual(value['total_reviews'],468841)
        self.assertIsNotNone(value['reviews_fetched_at'])
        self.assertEqual(get.call_args.args[0],reviews.REVIEWS_URL)

    def test_desktop_failures_preserve_review_values_and_ttl_and_record_retry(self):
        for fail in (response(EMPTY), response(status=429,headers={'Retry-After':'7200'}), requests.Timeout('fixture')):
            self.seed()
            with self.subTest(fail=fail), patch.object(reviews,'_client',reviews.ReviewClient()), patch.object(steam_store,'get_store_details',return_value={'appid':1}), patch.object(reviews.requests,'get',side_effect=fail if isinstance(fail,Exception) else None,return_value=fail):
                value=metadata_service.get_game_metadata(1,force_refresh=True)
            self.assertEqual(value['total_reviews'],468841)
            self.assertEqual(value['reviews_fetched_at'],'2020-01-01T00:00:00')
            status=local_db.get_metadata_fetch_status(1)
            self.assertIsNotNone(status['next_retry_at'])
            if getattr(fail,'status_code',None)==429:
                self.assertGreater(datetime.fromisoformat(status['next_retry_at']),datetime.now()+timedelta(seconds=7100))

    def test_global_full_and_reviews_only_paths_persist_new_summary(self):
        with patch.object(builder, 'get_store_details', side_effect=lambda n: {'appid': n}), patch.object(reviews.requests, 'get', return_value=response()):
            first = builder.update_steam(self.gp, limit=1, pause_seconds=0, reviews_pause_seconds=0)
        with global_db.connect(self.gp) as c:
            c.execute("UPDATE steam_metadata SET reviews_fetched_at='2020-01-01'")
            c.execute('DELETE FROM steam_catalog WHERE appid=2')
        with patch.object(reviews.requests, 'get', return_value=response()):
            second = builder.update_steam(self.gp, limit=1, pause_seconds=0, reviews_pause_seconds=0)
        self.assertEqual((first['details']['updated'], first['reviews']['updated']), (1, 1))
        self.assertEqual((second['details']['updated'], second['reviews']['updated']), (0, 1))

    def test_global_failure_preserves_counts_and_timestamp_without_blocking_details(self):
        for failure in (response(EMPTY), response(status=429, headers={'Retry-After': '7200'}), requests.Timeout('fixture timeout')):
            self.seed()
            with global_db.connect(self.gp) as c:
                c.execute('DELETE FROM steam_fetch_retries')
                c.execute("DELETE FROM updater_state WHERE key LIKE 'steam_%retry_after'")
                c.execute('UPDATE steam_catalog SET steam_dirty=1')
            with self.subTest(failure=failure), patch.object(builder, 'get_store_details', side_effect=lambda n: {'appid': n}), patch.object(reviews.requests, 'get', side_effect=failure if isinstance(failure, Exception) else None, return_value=failure), redirect_stdout(io.StringIO()):
                report = builder.update_steam(self.gp, limit=2, pause_seconds=0, reviews_pause_seconds=0)
            self.assertEqual(report['details']['updated'], 2)
            self.assertGreater(report['reviews']['errors'], 0)
            self.assertFalse(builder.steam_run_failed(report))
            self.assertTrue(builder.steam_run_partial(report))
            with global_db.connect(self.gp) as c:
                row = c.execute('SELECT total_reviews,reviews_fetched_at FROM steam_metadata WHERE appid=1').fetchone()
            self.assertEqual(tuple(row), (468841, '2020-01-01T00:00:00'))
            if getattr(failure, 'status_code', None) == 429:
                self.assertGreater(float(global_db.get_state(self.gp, 'steam_reviews_retry_after')), reviews.time.time() + 7100)

    def test_main_allows_valid_partial_progress_and_writes_visible_report(self):
        report_path = self.root / 'report.json'
        with patch.object(sys, 'argv', ['builder', '--db', str(self.gp), '--skip-catalog', '--steam-limit', '2', '--steam-pause', '0', '--steam-reviews-pause', '0', '--hltb-limit', '0', '--report', str(report_path)]), patch.object(builder, 'get_store_details', side_effect=lambda n: {'appid': n}), patch.object(reviews.requests, 'get', return_value=response(EMPTY)), redirect_stdout(io.StringIO()):
            self.assertEqual(builder.main(), 0)
        report = json.loads(report_path.read_text())
        self.assertEqual(report['status'], 'partial')
        self.assertTrue(report['publishable'])
        self.assertEqual(report['steam']['reviews']['updated'], 0)
        self.assertEqual(report['steam']['reviews']['errors'], 2)

    def test_no_due_work_is_success_without_claiming_a_review_refresh(self):
        with patch.object(builder, 'steam_details_candidates', return_value=[]), patch.object(builder, 'steam_review_candidates', return_value=[]):
            report = builder.update_steam(self.gp, limit=2, pause_seconds=0)
        self.assertFalse(builder.steam_run_failed(report))
        self.assertEqual(report['details']['selected'], 0)
        self.assertEqual(report['reviews']['updated'], 0)

    def test_desktop_worker_obeys_long_retry_after_for_whole_batch(self):
        module=metadata_background
        module._stop_event.clear()
        self.addCleanup(module._stop_event.clear)
        r=response(status=429,headers={'Retry-After':'7200'})
        waits=[]
        def wait(seconds):
            waits.append(seconds); module._stop_event.set(); return True
        with patch.object(module,'_current_library_games',return_value=[{'appid':1}]), patch.object(module,'_candidate_appids',return_value=([1],1,0)), patch.object(module,'_take_priority_batch',return_value=[]), patch.object(module,'get_game_metadata',side_effect=requests.HTTPError(response=r)), patch.object(module,'_wait',side_effect=wait):
            module._run_worker()
        self.assertEqual(waits,[7200])

    def test_partial_review_failure_is_visible_and_publishable(self):
        with patch.object(builder, 'get_store_details', side_effect=lambda n: {'appid': n}), patch.object(reviews.requests, 'get', side_effect=[response(), response(EMPTY)]), redirect_stdout(io.StringIO()):
            report = builder.update_steam(self.gp, limit=2, pause_seconds=0, reviews_pause_seconds=0)
        self.assertEqual(report['reviews']['updated'], 1)
        self.assertEqual(report['reviews']['errors'], 1)
        self.assertFalse(builder.steam_run_failed(report))
        self.assertTrue(builder.steam_run_partial(report))
