"""Deterministic worker scheduling tests; no real DB, HTTP or long sleeps."""
from datetime import datetime
import sqlite3
import threading
import unittest
from unittest.mock import patch

import metadata_background as steam
import hltb_background as hltb

RECORDS=[{'AppID':1,'Game':'Fixture'}]


class BackgroundLifecycleTests(unittest.TestCase):
    def setUp(self):
        guard=patch('requests.adapters.HTTPAdapter.send',side_effect=AssertionError('Real HTTP forbidden'))
        guard.start();self.addCleanup(guard.stop)

    def test_transient_scan_failure_recovers_but_programming_failure_stops(self):
        for module in (steam,hltb):
            for error,retries in ((sqlite3.OperationalError('database is locked'),2),(sqlite3.ProgrammingError('bad schema'),1)):
                with self.subTest(module=module.__name__,error=error):
                    module._stop_event.clear();module._restart_requested=False
                    with patch.object(module,'_run_worker',side_effect=[error,None]) as run, patch.object(module,'_wait',return_value=False), self.assertLogs(module._log,level='ERROR'):
                        module._worker_loop()
                    self.assertEqual(run.call_count,retries)
                    self.assertIsNone(module._worker_thread)

    def test_stop_then_start_during_request_restarts_without_overlapping_work(self):
        for module,start,stop in ((steam,steam.start_metadata_background_refresh,steam.stop_metadata_background_refresh),(hltb,hltb.start_hltb_background_refresh,hltb.stop_hltb_background_refresh)):
            entered=threading.Event();release=threading.Event();restarted=threading.Event();finish=threading.Event()
            workers=[];active=[0,0];lock=threading.Lock();original=threading.Thread
            def factory(*a,**kw):
                t=original(*a,**kw);workers.append(t);return t
            def work():
                with lock:active[0]+=1;active[1]=max(active)
                try:
                    if len(workers)==1:
                        entered.set();self.assertTrue(release.wait(3))
                    else:
                        restarted.set();self.assertTrue(finish.wait(3))
                finally:
                    with lock:active[0]-=1
            module._worker_thread=None;module._restart_requested=False;module._stop_event.clear()
            with patch.object(module.threading,'Thread',side_effect=factory),patch.object(module,'_run_worker',side_effect=work):
                try:
                    self.assertTrue(start(RECORDS));self.assertTrue(entered.wait(2))
                    stop();self.assertFalse(start(RECORDS));release.set()
                    self.assertTrue(restarted.wait(2));self.assertEqual(len(workers),2)
                    self.assertEqual(active[1],1)
                finally:
                    stop();release.set();finish.set()
                    for worker in workers:worker.join(3)
            self.assertTrue(all(not w.is_alive() for w in workers))
            self.assertIsNone(module._worker_thread)

    def test_stop_without_restart_does_not_resurrect_worker(self):
        for module,stop in ((steam,steam.stop_metadata_background_refresh),(hltb,hltb.stop_hltb_background_refresh)):
            module._restart_requested=True
            stop()
            with patch.object(module,'_launch_worker_locked') as launch:
                module._worker_loop()
                launch.assert_not_called()

    def test_steam_retry_becomes_due_while_worker_is_idle_without_rerun(self):
        state={'next_retry_at':'2999-01-01T00:00:00'}
        class Wake:
            def wait(self,seconds):state['next_retry_at']='2000-01-01T00:00:00'
            def clear(self):pass
        steam._library_games=steam._normalise_library_games(RECORDS);steam._stop_event.clear()
        def fetch(*args,**kw):
            steam._stop_event.set()
            return {'fetched_at':datetime.now().isoformat(),'reviews_fetched_at':datetime.now().isoformat()}
        with patch.object(steam,'get_all_game_metadata',return_value={}), patch.object(steam,'get_all_metadata_fetch_statuses',side_effect=lambda:{1:state}),patch.object(steam,'_wake_event',Wake()),patch.object(steam,'get_game_metadata',side_effect=fetch) as get:
            steam._run_worker()
        self.assertEqual(get.call_count,1)

    def test_hltb_cache_expiration_is_rechecked_while_idle(self):
        cache={1:{'state':'matched','match_confidence':'high','fetched_at':datetime.now().isoformat()}}
        class Wake:
            def wait(self,seconds):cache[1]['fetched_at']='2020-01-01T00:00:00'
            def clear(self):pass
        hltb._library_games=hltb._normalise_library_games(RECORDS);hltb._stop_event.clear()
        def fetch(*args,**kw):hltb._stop_event.set();return {'state':'matched'}
        with patch.object(hltb,'get_all_hltb_metadata',side_effect=lambda:cache),patch.object(hltb,'_wake_event',Wake()),patch.object(hltb,'get_hltb_metadata',side_effect=fetch) as get:
            hltb._run_worker()
        self.assertEqual(get.call_count,1)
