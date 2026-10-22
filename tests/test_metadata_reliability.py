"""Offline cache, matching, acquisition-budget and bootstrap regressions."""
from contextlib import redirect_stdout
from datetime import datetime, timezone
import importlib
import io
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import requests
import database as db
import metadata_service as ms
import steam_store
import hltb_service as hs
import hltb_transport as transport
import global_bootstrap as bootstrap
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'global_metadata'))
import global_database as global_db
import build_global_database as builder
from test_hltb_global import FakeHTTP


class MetadataReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'local.db'
        for p in (patch.object(db,'DATABASE_PATH',self.path), patch.object(db,'ensure_data_dir'),
                  patch('requests.adapters.HTTPAdapter.send',side_effect=AssertionError('Real HTTP forbidden'))):
            p.start(); self.addCleanup(p.stop)
        db.init_database()

    def test_reviews_failure_preserves_values_and_old_review_timestamp(self):
        db.save_game_metadata({'appid':1,'reviews':{'positive_percentage':95,'total_reviews':100}})
        with db.get_connection() as c:
            c.execute("UPDATE game_metadata SET reviews_fetched_at='2020-01-01T00:00:00'")
        details=Mock();details.json.return_value={'1':{'success':True,'data':{}}}
        reviews=Mock();reviews.json.return_value={'success':0}
        with patch.object(steam_store.requests,'get',side_effect=[details,reviews]):
            result=ms.get_game_metadata(1,force_refresh=True)
        self.assertEqual((result['positive_percentage'],result['total_reviews']),(95,100))
        self.assertEqual(result['reviews_fetched_at'],'2020-01-01T00:00:00')
        self.assertFalse(ms.is_cache_fresh(result))
        self.assertEqual(db.get_metadata_fetch_status(1)['last_error'],'reviews_unavailable')

    def test_v1_review_timestamp_migration_preserves_personal_data(self):
        db.save_game_data(1,'playing','keep')
        db.save_game_metadata({'appid':1,'reviews':{'total_reviews':0}})
        with db.get_connection() as c:
            c.execute('ALTER TABLE game_metadata DROP COLUMN reviews_fetched_at')
            c.execute('PRAGMA user_version=1')
        db.init_database()
        row=db.get_game_metadata(1)
        self.assertEqual(row['fetched_at'],row['reviews_fetched_at'])
        self.assertEqual(db.get_all_game_data()[1]['notes'],'keep')

    def test_display_reads_never_fetch_or_wait_on_fetch_locks(self):
        with patch.object(ms,'_get_fetch_lock',side_effect=AssertionError('waited')), patch.object(hs,'_get_fetch_lock',side_effect=AssertionError('waited')):
            self.assertIsNone(ms.get_game_metadata(1,allow_fetch=False))
            self.assertIsNone(hs.get_hltb_metadata(1,'Fixture',allow_fetch=False))

    def test_real_dependency_parser_rejects_numeric_and_roman_sequel_conflicts(self):
        module=importlib.import_module('howlongtobeatpy.HTMLRequests')
        for title,matched in [('Dark Souls 2','Dark Souls'),('Dark Souls II','Dark Souls III')]:
            queries=[title,*hs._structural_fallback_queries(title,[title])]
            fake=FakeHTTP(games={q:[{'game_id':10,'game_name':matched,'comp_main':36000}] for q in queries})
            with self.subTest(title=title), patch('requests.adapters.HTTPAdapter.send',side_effect=fake.send), patch.object(module,'UserAgent',return_value=SimpleNamespace(random='fixture')), patch.object(module.HTMLRequests,'SEARCH_URL',module.HTMLRequests.SEARCH_URL):
                self.assertIsNone(hs._search_hltb(title))
        self.assertFalse(hs.has_sequel_conflict('Resident Evil 4','Resident Evil IV'))
        self.assertFalse(hs.has_sequel_conflict('GRID (2019)','GRID'))

    def test_suspect_automatic_match_is_hidden_but_manual_override_is_untouched(self):
        for confidence in ('high','manual'):
            db.save_hltb_metadata(1,'Dark Souls 2','matched',hltb_id=10,matched_name='Dark Souls',match_confidence=confidence)
            row=db.get_hltb_metadata(1)
            self.assertEqual(hs.is_hltb_cache_fresh(row),confidence=='manual')
            self.assertEqual(hs.get_hltb_metadata(1,'Dark Souls 2',allow_fetch=False) is not None,confidence=='manual')
            self.assertEqual(db.get_hltb_metadata(1)['hltb_id'],10)
            if confidence=='manual':
                with patch.object(hs,'_search_hltb',side_effect=AssertionError('manual was fetched')):
                    self.assertEqual(hs.get_hltb_metadata(1,'Dark Souls 2')['hltb_id'],10)
                    self.assertEqual(hs.get_hltb_metadata(1,'Dark Souls 2',force_refresh=True)['hltb_id'],10)

    def test_valid_older_automatic_matches_are_not_blanket_invalidated(self):
        db.save_hltb_metadata(1,'Portal','matched',hltb_id=10,matched_name='Portal',match_confidence='high',matcher_version=1)
        with patch.object(hs,'_search_hltb',side_effect=AssertionError('valid match invalidated')):
            self.assertEqual(hs.get_hltb_metadata(1,'Portal')['hltb_id'],10)

    def test_exhausted_budget_is_not_cached_as_no_match(self):
        with patch.object(transport,'MAX_REQUESTS',0):
            with self.assertRaises(transport.BudgetExceeded):hs.get_hltb_metadata(1,'Fixture')
        self.assertIsNone(db.get_hltb_metadata(1))

    def test_transport_paces_requests_and_clips_timeout_to_budget(self):
        clock=[100.0]
        with patch.object(transport.time,'monotonic',side_effect=lambda:clock[0]), patch.object(transport.time,'sleep',side_effect=lambda n:clock.__setitem__(0,clock[0]+n)), patch.object(transport,'_last_started',0), patch.object(transport,'MAX_REQUESTS',2), patch.object(transport.requests,'get',return_value=Mock()) as get:
            t=transport.Transport();t.get('https://fixture.invalid');t.get('https://fixture.invalid')
            self.assertEqual(clock[0],100+transport.REQUEST_INTERVAL)
            self.assertLessEqual(get.call_args.kwargs['timeout'],10)
            with self.assertRaises(transport.BudgetExceeded):t.get('https://fixture.invalid')
            self.assertEqual(get.call_count,2)

    def test_budgeted_client_keeps_dependency_and_other_services_unmodified(self):
        module=importlib.import_module('howlongtobeatpy.HTMLRequests')
        original_url=module.HTMLRequests.SEARCH_URL
        fake=FakeHTTP()
        with patch('requests.adapters.HTTPAdapter.send',side_effect=fake.send), patch.object(module,'UserAgent',return_value=SimpleNamespace(random='fixture')), patch.object(transport,'REQUEST_INTERVAL',0), transport.search_budget():
            self.assertEqual(transport.client(.4).search('Missing'),[])
        self.assertEqual(module.HTMLRequests.SEARCH_URL,original_url)
        self.assertIs(module.requests,requests)
        self.assertEqual(len(fake.calls),4)

    def test_global_import_preserves_age_and_manual_override(self):
        gp=Path(self.temp.name)/'global.db'
        with patch.object(global_db,'_now',return_value=datetime(2020,1,1,tzinfo=timezone.utc)):
            global_db.init_database(gp)
            global_db.upsert_catalog(gp,[{'appid':n,'name':'Fixture'} for n in (1,2)])
            for n in (1,2):
                global_db.save_steam_ready(gp,{'appid':n})
                global_db.save_steam_reviews(gp,n,{'total_reviews':0})
                global_db.save_hltb_match(gp,n,'Fixture',{'hltb_id':10,'matcher_version':hs.HLTB_MATCHER_VERSION})
        db.save_hltb_metadata(2,'Fixture','matched',hltb_id=99,match_confidence='manual')
        db.import_global_metadata(gp,[{'AppID':1},{'AppID':2}])
        self.assertFalse(ms.is_cache_fresh(db.get_game_metadata(1)))
        self.assertFalse(hs.is_hltb_cache_fresh(db.get_hltb_metadata(1)))
        self.assertEqual(db.get_hltb_metadata(2)['hltb_id'],99)

    def test_global_candidates_revalidate_only_conflicting_automatic_matches(self):
        gp=Path(self.temp.name)/'global.db';global_db.init_database(gp)
        global_db.upsert_catalog(gp,[{'appid':n,'name':'Dark Souls 2'} for n in (1,2)])
        for n,confidence in ((1,'high'),(2,'manual')):
            global_db.save_hltb_match(gp,n,'Dark Souls 2',{'hltb_id':10,'matched_name':'Dark Souls','match_confidence':confidence})
        self.assertEqual([r['appid'] for r in global_db.hltb_candidates(gp,10)],[1])

    def test_global_429_stops_batch_and_cooldown_survives_next_run(self):
        gp=Path(self.temp.name)/'global.db';global_db.init_database(gp)
        global_db.upsert_catalog(gp,[{'appid':n,'name':str(n)} for n in (1,2,3)])
        response=requests.Response();response.status_code=429;response.headers['Retry-After']='3600'
        with patch.object(builder,'get_store_details',side_effect=requests.HTTPError('fixture',response=response)) as fetch, redirect_stdout(io.StringIO()):
            builder.update_steam(gp,limit=3,pause_seconds=0)
            builder.update_steam(gp,limit=3,pause_seconds=0)
        self.assertEqual(fetch.call_count,1)
        self.assertEqual(len(global_db.steam_candidates(gp,10)),3)

    def test_bootstrap_attempt_state_failure_is_nonfatal_and_retryable(self):
        with patch.object(bootstrap,'get_app_state',return_value=None), patch.object(bootstrap,'set_app_state',side_effect=sqlite3.OperationalError('database is locked')), self.assertLogs('slt.global_bootstrap',level='WARNING'):
            result=bootstrap.bootstrap_global_metadata([{'AppID':1}])
        self.assertFalse(result['completed']);self.assertTrue(result['retry_soon'])

    def test_bootstrap_runs_once_off_caller_thread(self):
        entered=threading.Event();release=threading.Event()
        def blocked(records):
            entered.set();self.assertTrue(release.wait(3))
            return {'attempted':True,'completed':True}
        with patch.object(bootstrap,'_worker_thread',None), patch.object(bootstrap,'_next_check',0), patch.object(bootstrap,'_last_result',None), patch.object(bootstrap,'bootstrap_global_metadata',side_effect=blocked) as run:
            try:
                bootstrap.start_global_bootstrap([{'AppID':1}]);self.assertTrue(entered.wait(2))
                worker=bootstrap._worker_thread
                bootstrap.start_global_bootstrap([{'AppID':1}])
                self.assertIsNone(bootstrap.get_bootstrap_result())
            finally:
                release.set();worker.join(3)
            self.assertEqual(run.call_count,1)
            self.assertTrue(bootstrap.get_bootstrap_result()['completed'])
