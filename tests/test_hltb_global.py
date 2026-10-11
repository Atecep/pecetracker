"""Baseline and global-builder regression tests. All HTTP is mocked."""
from contextlib import closing, redirect_stdout
from datetime import datetime, timezone
from email.message import Message
import importlib
import io
import json
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import textwrap
from types import SimpleNamespace
from urllib.parse import urlsplit
import unittest
from unittest.mock import patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "global_metadata"))
import build_global_database as builder
import global_database as db
import hltb_service as matcher
from hltb_transport import (
    HLTBFailure, HLTBSuspended, HLTBTransport, HTTPThrottle, ServiceProtection, retry_after_seconds,
)


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


class TimedHTTP(FakeHTTP):
    def __init__(self, clock, *, latency=0, route=None, **kwargs):
        super().__init__(**kwargs)
        self.clock = clock
        self.latency = latency
        self.route = route
        self.dispatches = []

    def send(self, request, **kwargs):
        self.dispatches.append((self.clock.now, request.method, urlsplit(request.url).path, request.body))
        try:
            result = self.route(request) if self.route else None
            if result is None:
                result = super().send(request, **kwargs)
            else:
                result.request, result.url = request, request.url
            return result
        finally:
            self.clock.now += self.latency


class OfflineTest(unittest.TestCase):
    def setUp(self):
        # A missed mock must fail locally, never access the service.
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
        sleep_patch = patch.object(builder.time, "sleep")
        self.sleep = sleep_patch.start()
        self.addCleanup(sleep_patch.stop)
        throttle = builder.HLTB_HTTP_THROTTLE
        old_interval, old_started, old_deadline = throttle.interval, throttle.last_started, throttle.not_before
        self.addCleanup(throttle.configure, old_interval)
        self.addCleanup(setattr, throttle, "last_started", old_started)
        self.addCleanup(setattr, throttle, "not_before", old_deadline)
        throttle.last_started = None
        throttle.not_before = 0.0
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

        class FixtureTransport:
            def search(self, query, phase, attempt):
                after_calls.append(query)
                return fixtures.get(query, [])

        after = builder.search_hltb_global(title, transport=FixtureTransport())
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


class TransportTests(OfflineTest):
    def test_counts_phases_status_duration_and_no_secrets(self):
        self.http(FakeHTTP())
        transport = HLTBTransport(emit=self.output.write)
        self.assertIsNone(builder.search_hltb_global("Portal", transport=transport))
        self.assertEqual(transport.requests, 4)
        self.assertEqual(transport.attempts, 1)
        records = transport.records
        self.assertEqual([r["phase"] for r in records], ["discovery", "script", "authentication", "search"])
        self.assertTrue(all(r["status"] == 200 and r["duration_seconds"] >= 0 for r in records))
        self.assertTrue(all(r["query"] == "Portal" and r["matching_phase"] == "original" for r in records))
        self.assertNotIn("SECRET", self.output.getvalue())
        self.assertNotIn("cookie", self.output.getvalue().lower())

    def test_matching_phases(self):
        self.http(FakeHTTP())
        transport = HLTBTransport(emit=lambda line: None)
        phases = []
        search = transport.search
        def capture(query, phase, attempt):
            phases.append((query, phase))
            return search(query, phase, attempt)
        with patch.object(transport, "search", side_effect=capture):
            builder.search_hltb_global("Game: Complete Edition", transport=transport)
        self.assertEqual(phases, [
            ("Game: Complete Edition", "original"), ("Game Complete Edition", "title_variant"),
            ("Game", "edition_fallback"), ("Game Complete", "structural_fallback"),
        ])
        self.assertEqual(transport.requests, 16)

    def test_each_search_rediscovers_and_reauthenticates(self):
        fake = FakeHTTP()
        self.http(fake)
        transport = HLTBTransport(emit=lambda line: None)
        for _ in range(2):
            transport.search("Portal", "original", 1)
        self.assertEqual([phase for phase, _, _ in fake.calls],
                         ["discovery", "script", "authentication", "search"] * 2)
        self.assertEqual(transport.requests, 8)

    def test_instrumentation_does_not_wrap_shared_requests_module(self):
        self.http(FakeHTTP())
        transport = HLTBTransport(emit=lambda line: None)
        module = importlib.import_module("howlongtobeatpy.HTMLRequests")
        with transport._instrument():
            self.assertIsNot(module.requests, requests)
            requests.get("https://steam.invalid")
            self.assertEqual(transport.requests, 0)
            module.requests.get(module.HTMLRequests.BASE_URL)
            self.assertEqual(transport.requests, 1)

    def test_summary_counts_all_requests_and_failure_details(self):
        self.http(FakeHTTP({"search": [response(503, "bad", {"Retry-After": "20"})]}))
        lines = []
        transport = HLTBTransport(emit=lines.append)
        builder.search_hltb_global("Portal", transport=transport)
        transport.log_summary()
        summary = json.loads(lines[-1].removeprefix("[hltb-http-summary] "))
        self.assertEqual((summary["requests"], summary["search_attempts"]), (8, 2))
        self.assertEqual(sum(item["count"] for item in summary["outcomes"]), 8)
        failed = [json.loads(line.removeprefix("[hltb-http] ")) for line in lines
                  if line.startswith("[hltb-http] ") and '"status": 503' in line]
        self.assertEqual(failed[0]["retry_after_seconds"], 20)
        self.assertIn("started_at", failed[0])

    def test_script_404_can_be_skipped_for_a_valid_later_script(self):
        fake = FakeHTTP({
            "discovery": [response(body='<script src="/_next/static/chunks/a.js"></script><script src="/_next/static/chunks/b.js"></script>')],
            "script": [response(404, "missing")],
        })
        self.http(fake)
        transport = HLTBTransport(emit=lambda line: None)
        self.assertEqual(transport.search("Portal", "original", 1), [])
        self.assertEqual(transport.requests, 5)
        self.assertEqual(transport.records[1]["status"], 404)

    def test_discovery_authentication_and_search_http_failures(self):
        for phase, count in [("discovery", 1), ("script", 2), ("authentication", 3), ("search", 4)]:
            with self.subTest(phase=phase):
                fake = FakeHTTP({phase: [response(503, "SECRET_RESPONSE")]})
                with patch("requests.adapters.HTTPAdapter.send", side_effect=fake.send):
                    transport = HLTBTransport(emit=self.output.write)
                    with self.assertRaises(HLTBFailure) as caught:
                        transport.search("Portal", "original", 1)
                    self.assertEqual((caught.exception.phase, caught.exception.status), (phase, 503))
                    self.assertEqual(transport.requests, count)
                    self.assertEqual(transport.records[-1]["failure"], "http")
        self.assertNotIn("SECRET", self.output.getvalue())

    def test_timeout_each_phase(self):
        for phase in ("discovery", "script", "authentication", "search"):
            with self.subTest(phase=phase):
                fake = FakeHTTP({phase: [requests.Timeout("SECRET_TIMEOUT")]})
                with patch("requests.adapters.HTTPAdapter.send", side_effect=fake.send):
                    with self.assertRaises(HLTBFailure) as caught:
                        HLTBTransport(emit=self.output.write).search("Portal", "original", 1)
                    self.assertEqual((caught.exception.phase, caught.exception.kind), (phase, "timeout"))
        self.assertNotIn("SECRET", self.output.getvalue())

    def test_invalid_responses_never_become_no_match(self):
        cases = [
            ("discovery", "no scripts", "endpoint_not_found"),
            ("script", "no endpoint", "endpoint_not_found"),
            ("authentication", "not JSON", "invalid_json"),
            ("authentication", {}, "missing_fields"),
            ("search", "not JSON", "invalid_json"),
            ("search", {}, "invalid_response"),
            ("search", {"data": None}, "invalid_response"),
            ("search", {"data": [None]}, "invalid_response"),
        ]
        for phase, body, kind in cases:
            with self.subTest(phase=phase, body=body):
                fake = FakeHTTP({phase: [response(body=body)]})
                with patch("requests.adapters.HTTPAdapter.send", side_effect=fake.send):
                    with self.assertRaises(HLTBFailure) as caught:
                        HLTBTransport(emit=lambda line: None).search("Portal", "original", 1)
                    self.assertEqual(caught.exception.kind, kind)

    def test_item_parsing_failure_is_not_a_no_match(self):
        self.http(FakeHTTP(games={"Portal": [{"game_id": 1, "game_name": "Portal", "comp_main": None}]}))
        with self.assertRaises(HLTBFailure) as caught:
            HLTBTransport(emit=lambda line: None).search("Portal", "original", 1)
        self.assertEqual(caught.exception.kind, "parsing")
        self.assertFalse(caught.exception.global_candidate)

    def test_dependency_and_matcher_restored_after_failure(self):
        module = importlib.import_module("howlongtobeatpy.HTMLRequests")
        original = (module.requests, module.HTMLRequests.__dict__["send_website_request_getcode"],
                    module.HTMLRequests.__dict__["send_website_get_auth_token"], module.HTMLRequests.SEARCH_URL,
                    matcher._search_once, matcher._search_queries)
        self.http(FakeHTTP({"authentication": [response(429, "error")]}))
        with self.assertRaises(HLTBSuspended):
            builder.search_hltb_global("Portal")
        after = (module.requests, module.HTMLRequests.__dict__["send_website_request_getcode"],
                 module.HTMLRequests.__dict__["send_website_get_auth_token"], module.HTMLRequests.SEARCH_URL,
                 matcher._search_once, matcher._search_queries)
        self.assertEqual(after, original)

    def test_redirects_count_as_requests_without_logging_headers(self):
        redirected = response(302, "", {"Location": "/SECRET_LOCATION", "Set-Cookie": "SECRET_COOKIE"})
        self.http(FakeHTTP({"discovery": [redirected]}))
        transport = HLTBTransport(emit=self.output.write)
        transport.search("Portal", "original", 1)
        self.assertEqual(transport.requests, 5)
        self.assertEqual(sum(transport.counts.values()), 5)
        self.assertNotIn("SECRET", self.output.getvalue())


class AuthenticationTests(OfflineTest):
    def compare_original_and_instrumented(self, auth, redirects=False, endpoint="/api/search/site"):
        module = importlib.import_module("howlongtobeatpy.HTMLRequests")
        from howlongtobeatpy import HowLongToBeat
        calls = []
        for instrumented in (False, True):
            failures = {
                "authentication": [response(body=auth)],
                "script": [response(body=f'fetch("{endpoint}", {{method: "POST"}})')],
            }
            if redirects:
                redirected = response(302, "", {
                    "Location": "/api/search/site/init?redirected=1",
                    "Set-Cookie": "fixture=SECRET_COOKIE; Path=/",
                })
                headers = Message()
                headers["Set-Cookie"] = "fixture=SECRET_COOKIE; Path=/"
                redirected.raw = SimpleNamespace(_original_response=SimpleNamespace(msg=headers))
                failures["authentication"].insert(0, redirected)
            fake = FakeHTTP(failures)
            prepared = []

            def send(request, **kwargs):
                # Compare sensitive data in memory only; never print it.
                prepared.append((request.method, request.url, dict(request.headers), request.body, kwargs))
                return fake.send(request, **kwargs)

            original_url = module.HTMLRequests.SEARCH_URL
            try:
                with patch("requests.adapters.HTTPAdapter.send", side_effect=send), patch.object(module.time, "time", return_value=1800000000):
                    if instrumented:
                        transport = HLTBTransport(emit=self.output.write)
                        result = transport.search("Portal", "original", 1)
                    else:
                        result = HowLongToBeat(0.4).search("Portal", similarity_case_sensitive=False)
                self.assertEqual(result, [])
            finally:
                module.HTMLRequests.SEARCH_URL = original_url
            self.assertEqual(sum(phase == "search" for phase, _, _ in fake.calls), 1)
            calls.append(prepared)
        # Equality covers URLs, params, user agent, headers, body and Requests
        # options, plus cookies/methods across redirects when enabled.
        self.assertEqual(calls[0], calls[1])
        if redirects:
            # The cookie follows the auth redirect, but per-call sessions do
            # not carry it into the search POST in either implementation.
            self.assertIn("SECRET_COOKIE", calls[1][-2][2]["Cookie"])
            self.assertNotIn("Cookie", calls[1][-1][2])
        self.assertNotIn("SECRET", self.output.getvalue())
        return transport

    def test_numeric_authentication_matches_original_and_reaches_post(self):
        for token, key, value in (
            ("SECRET_TOKEN", 12345, 67890),
            (12345, "SECRET_KEY", 67890),
            (12345, 67890, 0),
            (12345.5, 67890.5, False),
            (True, False, 0),
            ("SECRET_TOKEN", "SECRET_KEY", ""),
            ("SECRET_TOKEN", "SECRET_KEY", {"nested": "SECRET_VALUE"}),
        ):
            with self.subTest(types=[type(field).__name__ for field in (token, key, value)]):
                transport = self.compare_original_and_instrumented({"token": token, "hpKey": key, "hpVal": value})
                fields = transport.records[-2]["authentication_diagnostics"]["fields"]
                self.assertEqual(fields["key"], {"available": True, "type": type(key).__name__})
                self.assertEqual(transport.requests, 4)

    def test_string_authentication_matches_original(self):
        self.compare_original_and_instrumented({"token": "SECRET_TOKEN", "key": "SECRET_KEY", "value": "SECRET_VALUE"})

    def test_numeric_authentication_preserves_successful_match(self):
        fake = FakeHTTP(
            failures={"authentication": [
                response(body={"token": "SECRET_TOKEN", "hpKey": 12345, "hpVal": 67890})
                for _ in range(2)
            ]},
            games={"Portal": [{
                "game_id": 20, "game_name": "Portal", "release_world": 2007,
                "comp_main": 36000, "comp_plus": 55800, "comp_100": 79200, "comp_all": 43200,
            }]},
        )
        self.http(fake)
        before = matcher._search_hltb("Portal")
        after = builder.search_hltb_global("Portal")
        self.assertEqual(after, before)
        self.assertEqual(after["hltb_id"], 20)
        self.assertEqual((after["main_story"], after["main_extra"], after["completionist"], after["all_styles"]),
                         (10.0, 15.5, 22.0, 12.0))
        self.assertEqual(sum(phase == "search" for phase, _, _ in fake.calls), 2)
        self.assertNotIn("SECRET", self.output.getvalue())

    def test_authentication_redirects_and_cookies_match_original(self):
        transport = self.compare_original_and_instrumented({"token": "SECRET_TOKEN", "hpKey": 12345, "hpVal": 67890}, redirects=True)
        self.assertEqual(transport.requests, 5)

    def test_missing_fields_and_unexpected_types_are_distinguished(self):
        valid = {"token": "SECRET_TOKEN", "hpKey": 12345, "hpVal": 67890}
        cases = [("SECRET_INVALID_JSON", "invalid_json"), ([], "unexpected_type"), (None, "unexpected_type")]
        for field in valid:
            missing = {key: value for key, value in valid.items() if key != field}
            cases.extend([(missing, "missing_fields"), ({**valid, field: None}, "missing_fields")])
        cases.extend([({**valid, "hpKey": []}, "unexpected_type"), ({**valid, "token": {}}, "unexpected_type")])
        for auth, kind in cases:
            with self.subTest(kind=kind):
                fake = FakeHTTP({"authentication": [response(body=auth)]})
                with patch("requests.adapters.HTTPAdapter.send", side_effect=fake.send):
                    transport = HLTBTransport(emit=self.output.write)
                    with self.assertRaises(HLTBFailure) as caught:
                        transport.search("Portal", "original", 1)
                self.assertEqual((caught.exception.phase, caught.exception.kind, caught.exception.status), ("authentication", kind, 200))
                self.assertTrue(caught.exception.global_candidate)
                self.assertEqual(transport.records[-1]["failure"], kind)
                self.assertFalse(any(phase == "search" for phase, _, _ in fake.calls))
        self.assertNotIn("SECRET", self.output.getvalue())

    def test_library_exceptions_keep_safe_type_and_actual_status(self):
        module = importlib.import_module("howlongtobeatpy.HTMLRequests")
        self.http(FakeHTTP())
        for exception in (ValueError, TypeError, AttributeError, KeyError, RuntimeError):
            with self.subTest(exception=exception.__name__), patch.object(module.SearchAuthToken, "extract_auth_token_from_response", side_effect=exception("SECRET_EXCEPTION_MESSAGE")):
                transport = HLTBTransport(emit=self.output.write)
                with self.assertRaises(HLTBFailure) as caught:
                    transport.search("Portal", "original", 1)
                self.assertEqual((caught.exception.kind, caught.exception.status), ("library_exception", 200))
                diagnostics = transport.records[-1]["authentication_diagnostics"]
                self.assertEqual(diagnostics["exception_type"], exception.__name__)
                self.assertNotIn("SECRET", str(caught.exception))
        self.assertNotIn("SECRET", self.output.getvalue())

    def test_unexpected_library_return_is_adapter_incompatibility(self):
        module = importlib.import_module("howlongtobeatpy.HTMLRequests")
        self.http(FakeHTTP())
        with patch.object(module.SearchAuthToken, "extract_auth_token_from_response", return_value="SECRET_UNEXPECTED_RESULT"):
            transport = HLTBTransport(emit=self.output.write)
            with self.assertRaises(HLTBFailure) as caught:
                transport.search("Portal", "original", 1)
        self.assertEqual((caught.exception.kind, caught.exception.status), ("adapter_incompatibility", 200))
        self.assertEqual(transport.records[-1]["authentication_diagnostics"]["result_type"], "str")
        self.assertNotIn("SECRET", self.output.getvalue())

    def test_internal_failure_before_http_does_not_invent_http_200(self):
        module = importlib.import_module("howlongtobeatpy.HTMLRequests")
        self.http(FakeHTTP())
        with patch.object(module.HTMLRequests, "get_auth_token_request_params", side_effect=RuntimeError("SECRET_EXCEPTION_MESSAGE")):
            transport = HLTBTransport(emit=self.output.write)
            with self.assertRaises(HLTBFailure) as caught:
                transport.search("Portal", "original", 1)
        self.assertEqual((caught.exception.phase, caught.exception.kind, caught.exception.status), ("authentication", "library_exception", None))
        self.assertEqual(transport.requests, 2)
        self.assertTrue(all(record["failure"] is None for record in transport.records))
        self.assertEqual(caught.exception.diagnostics, {"exception_type": "RuntimeError"})
        self.assertIn('"exception_type": "RuntimeError"', self.output.getvalue())
        self.assertNotIn("SECRET", self.output.getvalue())

    def test_consecutive_auth_failures_suspend_and_recovery_resets(self):
        fake = FakeHTTP({"authentication": [response(body={}) for _ in range(3)]})
        self.http(fake)
        protection = ServiceProtection()
        transport = HLTBTransport(emit=self.output.write)
        with self.assertRaises(HLTBFailure):
            builder.search_hltb_global("Portal", transport=transport, protection=protection)
        with self.assertRaises(HLTBSuspended):
            builder.search_hltb_global("Missing", transport=transport, protection=protection)
        self.assertEqual((transport.requests, transport.attempts), (9, 3))
        self.assertFalse(any(phase == "search" for phase, _, _ in fake.calls))
        self.assertIsNone(builder.search_hltb_global("Portal", transport=transport, protection=protection))
        self.assertEqual(protection.failures, 0)

    def test_exact_json_fields_are_separate_from_parsed_fields(self):
        for key_name, value_name in (("key", "value"), ("hpKey", "hpVal")):
            with self.subTest(key_name=key_name):
                transport = self.compare_original_and_instrumented({
                    "token": "SECRET_TOKEN", key_name: "SECRET_KEY", value_name: "SECRET_VALUE",
                })
                diagnostics = transport.records[-2]["authentication_diagnostics"]
                self.assertEqual(diagnostics["json_fields"][key_name], {"present": True, "type": "str"})
                self.assertEqual(diagnostics["json_fields"][value_name], {"present": True, "type": "str"})
                other_key = "hpKey" if key_name == "key" else "key"
                self.assertEqual(diagnostics["json_fields"][other_key], {"present": False, "type": None})
                self.assertTrue(diagnostics["fields"]["key"]["available"])
                self.assertEqual(diagnostics["parser_candidates"]["key"], {"count": 1, "types": ["str"], "selected_type": "str"})

    def test_absent_and_null_auth_fields_have_different_diagnostics(self):
        for auth, present in (({"token": "SECRET_TOKEN"}, False),
                              ({"token": "SECRET_TOKEN", "hpKey": None, "hpVal": None}, True)):
            with self.subTest(present=present):
                fake = FakeHTTP({"authentication": [response(body=auth)]})
                with patch("requests.adapters.HTTPAdapter.send", side_effect=fake.send):
                    transport = HLTBTransport(emit=self.output.write)
                    with self.assertRaises(HLTBFailure) as caught:
                        transport.search("Portal", "original", 1)
                self.assertEqual(caught.exception.kind, "missing_fields")
                shape = caught.exception.diagnostics
                self.assertEqual(shape["json_fields"]["hpKey"], {"present": present, "type": "NoneType" if present else None})
                self.assertEqual(shape["json_fields"]["hpVal"], {"present": present, "type": "NoneType" if present else None})
                self.assertEqual(shape["parser_candidates"]["key"]["count"], int(present))
                self.assertFalse(any(phase == "search" for phase, _, _ in fake.calls))
        self.assertNotIn("SECRET", self.output.getvalue())

    def test_invalid_json_and_root_types_fail_in_both_clients(self):
        module = importlib.import_module("howlongtobeatpy.HTMLRequests")
        for auth, original_error, kind in (("SECRET_INVALID_JSON", ValueError, "invalid_json"),
                                           ([], AttributeError, "unexpected_type"),
                                           (None, AttributeError, "unexpected_type")):
            with self.subTest(kind=kind):
                original_fake = FakeHTTP({"authentication": [response(body=auth)]})
                with patch("requests.adapters.HTTPAdapter.send", side_effect=original_fake.send):
                    with self.assertRaises(original_error):
                        module.HTMLRequests.send_website_get_auth_token("api/search/site", "fixture-agent")
                adapted_fake = FakeHTTP({"authentication": [response(body=auth)]})
                with patch("requests.adapters.HTTPAdapter.send", side_effect=adapted_fake.send):
                    with self.assertRaises(HLTBFailure) as caught:
                        HLTBTransport(emit=self.output.write).search("Portal", "original", 1)
                self.assertEqual((caught.exception.phase, caught.exception.kind), ("authentication", kind))
                self.assertFalse(any(phase == "search" for phase, _, _ in adapted_fake.calls))
        self.assertNotIn("SECRET", self.output.getvalue())

    def test_partial_formats_match_original_parser_without_inventing_fields(self):
        module = importlib.import_module("howlongtobeatpy.HTMLRequests")
        cases = [
            {"token": "SECRET_TOKEN"},
            {"token": "SECRET_TOKEN", "data": {"hpKey": "SECRET_KEY", "hpVal": "SECRET_VALUE"}},
            {"token": "SECRET_TOKEN", "auth": {"key": "SECRET_KEY", "value": "SECRET_VALUE"}},
            {"token": "SECRET_TOKEN", "k": "SECRET_KEY", "v": "SECRET_VALUE"},
            {"TOKEN": "SECRET_TOKEN", "HPKEY": "SECRET_KEY", "HPVAL": "SECRET_VALUE"},
            {"token": "SECRET_TOKEN", "hpKey": "SECRET_KEY", "hpVal": "SECRET_VALUE", "key": None, "value": None},
        ]
        for index, auth in enumerate(cases):
            with self.subTest(format=index):
                original = module.SearchAuthToken().extract_auth_token_from_json(auth)
                fake = FakeHTTP({"authentication": [response(body=auth)]})
                with patch("requests.adapters.HTTPAdapter.send", side_effect=fake.send):
                    transport = HLTBTransport(emit=self.output.write)
                    with self.assertRaises(HLTBFailure) as caught:
                        transport.search("Portal", "original", 1)
                self.assertEqual(caught.exception.kind, "missing_fields")
                expected = {
                    name: {"available": value is not None, "type": type(value).__name__}
                    for name, value in (("token", original.auth_token), ("key", original.auth_key), ("value", original.auth_value))
                }
                self.assertEqual(caught.exception.diagnostics["fields"], expected)
                self.assertFalse(any(phase == "search" for phase, _, _ in fake.calls))
        # With token only, the original library supplies unusable placeholders
        # itself. Their construction is not proof the server accepts them.
        original = module.SearchAuthToken().extract_auth_token_from_json({"token": "SECRET_TOKEN"})
        headers = module.HTMLRequests.get_search_request_headers(original, "fixture-agent")
        payload = json.loads(module.HTMLRequests.get_search_request_data("Portal", module.SearchModifiers.NONE, 1, original))
        self.assertEqual((headers["x-hp-key"], headers["x-hp-val"]), ("None", "None"))
        self.assertIn("null", payload)
        self.assertIsNone(payload["null"])
        self.assertNotIn("SECRET", self.output.getvalue())

    def test_case_and_nested_variants_are_diagnosed_without_remapping(self):
        auth = {"token": "SECRET_TOKEN", "HPKEY": None, "HPVAL": None,
                "data": {"hpKey": "SECRET_KEY", "hpVal": "SECRET_VALUE"}}
        fake = FakeHTTP({"authentication": [response(body=auth)]})
        self.http(fake)
        with self.assertRaises(HLTBFailure) as caught:
            HLTBTransport(emit=self.output.write).search("Portal", "original", 1)
        diagnostics = caught.exception.diagnostics
        self.assertEqual(diagnostics["case_variant_types"]["hpKey"], ["NoneType"])
        self.assertEqual(diagnostics["nested_fields"]["data"]["fields"]["hpKey"], {"present": True, "type": "str"})
        self.assertFalse(diagnostics["fields"]["key"]["available"])
        self.assertNotIn("SECRET", self.output.getvalue())

    def test_conflicting_formats_keep_original_order_dependent_selection(self):
        module = importlib.import_module("howlongtobeatpy.HTMLRequests")
        plain = {"key": "SECRET_PLAIN_KEY", "value": "SECRET_PLAIN_VALUE"}
        prefixed = {"hpKey": "SECRET_PREFIXED_KEY", "hpVal": "SECRET_PREFIXED_VALUE"}
        for auth, expected in (({"token": "SECRET_TOKEN", **plain, **prefixed}, prefixed),
                               ({"token": "SECRET_TOKEN", **prefixed, **plain}, plain)):
            original = module.SearchAuthToken().extract_auth_token_from_json(auth)
            self.assertEqual(original.auth_key, next(iter(expected.values())))
            self.assertEqual(original.auth_value, list(expected.values())[1])
            transport = self.compare_original_and_instrumented(auth)
            candidates = transport.records[-2]["authentication_diagnostics"]["parser_candidates"]
            self.assertEqual((candidates["key"]["count"], candidates["value"]["count"]), (2, 2))

    def test_diagnostics_never_log_arbitrary_response_names_or_values(self):
        auth = {"token": "SECRET_TOKEN", "hpKey": "SECRET_KEY", "hpVal": "SECRET_VALUE",
                "SECRET_OTHER_KEY": "SECRET_OTHER_VALUE", "SECRET_NAME": "SECRET_BODY",
                "authentication": {"hpKey": "SECRET_NESTED_KEY", "SECRET_FIELD": "SECRET_NESTED_BODY"}}
        transport = self.compare_original_and_instrumented(auth)
        shape = transport.records[-2]["authentication_diagnostics"]
        self.assertEqual(shape["parser_candidates"]["key"]["count"], 2)
        self.assertNotIn("SECRET", json.dumps(shape))
        self.assertNotIn("SECRET", self.output.getvalue())

    def test_dynamic_endpoint_is_identical_to_original(self):
        for endpoint in ("/api/finder/v2", "/api/another/search"):
            with self.subTest(endpoint=endpoint):
                self.compare_original_and_instrumented({"token": "SECRET_TOKEN", "hpKey": "SECRET_KEY", "hpVal": "SECRET_VALUE"}, endpoint=endpoint)


class HLTBProbeTests(OfflineTest):
    def run_probe(self, failures=None):
        workflow = Path(__file__).resolve().parents[1] / ".github/workflows/update-global-metadata.yml"
        scripts = re.findall(r"python -u - <<'PY'[^\n]*\n(.*?)^          PY$", workflow.read_text(), re.S | re.M)
        self.assertEqual(len(scripts), 1)
        script = compile(textwrap.dedent(scripts[0]), str(workflow) + ":hltb_probe", "exec")
        clock = FakeClock()
        fake = TimedHTTP(clock, failures=failures)
        self.http(fake)
        self.sleep.side_effect = clock.sleep
        original = HLTBTransport.search
        with patch.object(builder.time, "monotonic", side_effect=clock.monotonic), \
             patch.object(builder.time, "time", side_effect=clock.wall_time), \
             patch.object(sys, "path", list(sys.path)), \
             patch.object(builder, "search_hltb_global", side_effect=AssertionError("Builder forbidden")), \
             patch.object(matcher, "_search_hltb", side_effect=AssertionError("Matcher fallbacks forbidden")), \
             patch.object(sqlite3, "connect", side_effect=AssertionError("SQLite forbidden")), \
             patch.object(HLTBTransport, "search", autospec=True, side_effect=original) as search:
            with self.assertRaises(SystemExit) as caught:
                exec(script, {"__name__": "__main__"})
        search.assert_called_once()
        self.assertEqual(search.call_args.args[1:], ("Jump of Icarus",))
        self.assertEqual(search.call_args.kwargs, {"matching_phase": "original", "attempt": 1})
        starts = [event[0] for event in fake.dispatches]
        self.assertTrue(all(second - first >= 1 for first, second in zip(starts, starts[1:])))
        records = []
        prefixes = {
            "[hltb-http] ": {"request", "phase", "status", "failure", "authentication_diagnostics"},
            "[hltb-search] ": {"phase", "failure", "status", "failure_diagnostics"},
            "[hltb-http-summary] ": {"requests", "search_attempts", "outcomes"},
            "[hltb-probe] ": {"phase", "failure", "exception_type"},
        }
        for line in self.output.getvalue().splitlines():
            prefix = next(prefix for prefix in prefixes if line.startswith(prefix))
            record = json.loads(line[len(prefix):])
            self.assertLessEqual(record.keys(), prefixes[prefix])
            records.append((prefix, record))
        summary = next(record for prefix, record in records if prefix == "[hltb-http-summary] ")
        self.assertEqual(summary["search_attempts"], 1)
        self.assertEqual(summary["requests"], len(fake.dispatches))
        self.assertNotIn("SECRET", self.output.getvalue())
        self.assertNotIn("Jump of Icarus", self.output.getvalue())
        self.assertNotIn("Traceback", self.output.getvalue())
        return caught.exception.code, fake, records

    def test_empty_search_result_does_not_trigger_fallbacks(self):
        code, fake, _ = self.run_probe()
        self.assertEqual(code, 0)
        self.assertEqual([event[0] for event in fake.dispatches], [100, 101, 102, 103])
        self.assertEqual([phase for phase, _, _ in fake.calls], ["discovery", "script", "authentication", "search"])

    def test_missing_auth_fields_stop_before_post_without_retry(self):
        code, fake, records = self.run_probe({"authentication": [response(body={"token": "SECRET_TOKEN"})]})
        self.assertEqual(code, 1)
        self.assertEqual(len(fake.dispatches), 3)
        self.assertFalse(any(phase == "search" for phase, _, _ in fake.calls))
        search = next(record for prefix, record in records if prefix == "[hltb-search] ")
        self.assertEqual((search["phase"], search["failure"], search["status"]), ("authentication", "missing_fields", 200))
        fields = search["failure_diagnostics"]["json_fields"]
        self.assertEqual(fields["token"], {"present": True, "type": "str"})
        self.assertEqual(fields["hpKey"], {"present": False, "type": None})

    def test_http_failure_has_no_retry_or_extra_request(self):
        code, fake, records = self.run_probe({"authentication": [response(429, "SECRET_BODY", {"Retry-After": "30"})]})
        self.assertEqual(code, 1)
        self.assertEqual(len(fake.dispatches), 3)
        search = next(record for prefix, record in records if prefix == "[hltb-search] ")
        self.assertEqual((search["failure"], search["status"]), ("http", 429))
        self.assertFalse(any(phase == "search" for phase, _, _ in fake.calls))

    def test_unexpected_exception_is_sanitised_without_retry(self):
        with patch("hltb_transport.HowLongToBeat.search", side_effect=RuntimeError("SECRET_EXCEPTION_MESSAGE")):
            code, fake, records = self.run_probe()
        self.assertEqual(code, 1)
        self.assertEqual(fake.dispatches, [])
        failure = next(record for prefix, record in records if prefix == "[hltb-probe] ")
        self.assertEqual((failure["failure"], failure["exception_type"]), ("probe_exception", "RuntimeError"))


class HTTPThrottleTests(OfflineTest):
    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        self.sleep.side_effect = self.clock.sleep
        for name, callback in (("monotonic", self.clock.monotonic), ("time", self.clock.wall_time)):
            timer = patch.object(builder.time, name, side_effect=callback)
            timer.start()
            self.addCleanup(timer.stop)
        builder.configure_hltb_request_throttle(1)

    def assert_spacing(self, fake, minimum=1):
        starts = [event[0] for event in fake.dispatches]
        self.assertTrue(all(second - first >= minimum for first, second in zip(starts, starts[1:])), starts)
        return starts

    def test_all_phases_and_new_searches_have_one_second_spacing(self):
        fake = TimedHTTP(self.clock)
        self.http(fake)
        transport = HLTBTransport(emit=lambda line: None)
        builder.search_hltb_global("Portal", transport=transport)
        builder.search_hltb_global("Missing", transport=transport)
        self.assertEqual(self.assert_spacing(fake), list(range(100, 108)))
        self.assertEqual([event[1] for event in fake.dispatches], ["GET", "GET", "GET", "POST"] * 2)
        self.assertEqual(self.clock.sleeps, [1] * 7)
        self.assertEqual([record["started_monotonic"] for record in transport.records], [104, 105, 106, 107])

    def test_multiple_scripts_and_structural_fallbacks_are_spaced(self):
        fake = TimedHTTP(self.clock, failures={
            "discovery": [response(body='<script src="/_next/static/chunks/a.js"></script><script src="/_next/static/chunks/b.js"></script>')],
            "script": [response(body="unrelated code")],
        })
        self.http(fake)
        transport = HLTBTransport(emit=lambda line: None)
        self.assertIsNone(builder.search_hltb_global("Game: Complete Edition", transport=transport))
        self.assertEqual(self.assert_spacing(fake), list(range(100, 117)))
        self.assertEqual(transport.attempts, 4)
        self.assertEqual(transport.requests, 17)

    def test_separate_transport_instances_share_the_global_limit(self):
        fake = TimedHTTP(self.clock)
        self.http(fake)
        builder.search_hltb_global("Portal")
        builder.search_hltb_global("Missing")
        self.assertEqual(self.assert_spacing(fake), list(range(100, 108)))

    def test_search_calls_without_http_have_no_old_throttle(self):
        class FixtureTransport:
            def search(self, query, phase, attempt):
                return []
        builder.search_hltb_global("Portal", transport=FixtureTransport())
        builder.search_hltb_global("Missing", transport=FixtureTransport())
        self.sleep.assert_not_called()

    def test_configurable_interval_and_zero_interval(self):
        fake = TimedHTTP(self.clock)
        self.http(fake)
        builder.configure_hltb_request_throttle(.25)
        builder.search_hltb_global("Portal")
        self.assertEqual(self.assert_spacing(fake, .25), [100, 100.25, 100.5, 100.75])
        builder.configure_hltb_request_throttle(0)
        builder.search_hltb_global("Missing")
        self.assertEqual([event[0] for event in fake.dispatches[4:]], [100.75] * 4)
        self.assertEqual(self.clock.sleeps, [.25] * 3)

    def test_network_time_counts_towards_the_interval(self):
        fake = TimedHTTP(self.clock, latency=.25)
        self.http(fake)
        builder.search_hltb_global("Portal")
        self.assertEqual(self.assert_spacing(fake), [100, 101, 102, 103])
        self.assertEqual(self.clock.sleeps, [.75] * 3)

    def test_slow_requests_need_no_extra_sleep(self):
        fake = TimedHTTP(self.clock, latency=2)
        self.http(fake)
        builder.search_hltb_global("Portal")
        self.assertEqual(self.assert_spacing(fake), [100, 102, 104, 106])
        self.sleep.assert_not_called()

    def test_wall_clock_changes_do_not_change_spacing(self):
        fake = TimedHTTP(self.clock)
        self.http(fake)
        with patch.object(builder.time, "time", side_effect=lambda: 20_000 - self.clock.now):
            builder.search_hltb_global("Portal")
        self.assertEqual(self.assert_spacing(fake), [100, 101, 102, 103])

    def test_sleep_returning_early_rechecks_the_monotonic_clock(self):
        fake = TimedHTTP(self.clock)
        self.http(fake)
        def early_sleep(seconds):
            self.clock.sleeps.append(seconds)
            self.clock.now += min(seconds, .5)
        self.sleep.side_effect = early_sleep
        builder.search_hltb_global("Portal")
        self.assertEqual(self.assert_spacing(fake), [100, 101, 102, 103])
        self.assertEqual(self.clock.sleeps, [1, .5] * 3)

    def test_relative_redirect_chain_is_counted_and_spaced(self):
        fake = TimedHTTP(self.clock, failures={"discovery": [
            response(302, "", {"Location": "/other"}),
            response(301, "", {"Location": "/"}),
        ]})
        self.http(fake)
        transport = HLTBTransport(emit=lambda line: None)
        builder.search_hltb_global("Portal", transport=transport)
        self.assertEqual(self.assert_spacing(fake), list(range(100, 106)))
        self.assertEqual([record["status"] for record in transport.records], [302, 301, 200, 200, 200, 200])
        self.assertEqual(transport.requests, 6)
        self.assertEqual(sum(transport.counts.values()), 6)

    def test_post_307_redirect_keeps_method_and_body(self):
        fake = TimedHTTP(self.clock, failures={"search": [response(307, "", {"Location": "/api/search/site/redirected"})]})
        self.http(fake)
        builder.search_hltb_global("Portal")
        self.assertEqual(self.assert_spacing(fake), list(range(100, 105)))
        self.assertEqual([event[1] for event in fake.dispatches[-2:]], ["POST", "POST"])
        self.assertEqual(fake.dispatches[-2][3], fake.dispatches[-1][3])

    def test_post_303_redirect_keeps_requests_method_conversion(self):
        def route(request):
            if urlsplit(request.url).path == "/search-result":
                return response(body={"data": []})
        fake = TimedHTTP(self.clock, route=route, failures={"search": [response(303, "", {"Location": "/search-result"})]})
        self.http(fake)
        transport = HLTBTransport(emit=lambda line: None)
        builder.search_hltb_global("Portal", transport=transport)
        self.assertEqual(self.assert_spacing(fake), list(range(100, 105)))
        self.assertEqual([event[1] for event in fake.dispatches[-2:]], ["POST", "GET"])
        self.assertIsNone(fake.dispatches[-1][3])
        self.assertEqual([record["phase"] for record in transport.records[-2:]], ["search", "search"])

    def test_redirect_limit_stops_a_loop_with_every_hop_throttled(self):
        fake = TimedHTTP(self.clock, failures={"discovery": [response(302, "", {"Location": "/"}) for _ in range(31)]})
        self.http(fake)
        transport = HLTBTransport(emit=lambda line: None)
        with self.assertRaises(HLTBFailure) as caught:
            transport.search("Portal", "original", 1)
        self.assertEqual(caught.exception.kind, "network")
        self.assertEqual(self.assert_spacing(fake), list(range(100, 131)))
        self.assertEqual(transport.requests, 31)

    def test_redirect_retry_after_is_respected_before_following(self):
        fake = TimedHTTP(self.clock, failures={"discovery": [response(302, "", {"Location": "/", "Retry-After": "7"})]})
        self.http(fake)
        builder.search_hltb_global("Portal")
        self.assertEqual(self.assert_spacing(fake), [100, 107, 108, 109, 110])
        self.assertEqual(self.clock.sleeps, [7, 1, 1, 1])

    def test_long_redirect_retry_after_suspends_before_next_hop(self):
        fake = TimedHTTP(self.clock, failures={"discovery": [response(302, "", {"Location": "/", "Retry-After": "3600"})]})
        self.http(fake)
        with self.assertRaises(HLTBSuspended) as caught:
            builder.search_hltb_global("Portal")
        self.assertEqual(caught.exception.delay, 3600)
        self.assertEqual(len(fake.dispatches), 1)
        self.sleep.assert_not_called()

    def test_retries_use_the_same_limit_without_an_extra_throttle_delay(self):
        for result in (response(503, "error"), requests.Timeout("timeout")):
            with self.subTest(result=type(result).__name__):
                self.clock.now = 100
                self.clock.sleeps.clear()
                builder.HLTB_HTTP_THROTTLE.last_started = None
                fake = TimedHTTP(self.clock, failures={"search": [result]})
                with patch("requests.adapters.HTTPAdapter.send", side_effect=fake.send):
                    builder.search_hltb_global("Portal")
                self.assertEqual(self.assert_spacing(fake), [100, 101, 102, 103, 108, 109, 110, 111])
                self.assertEqual(self.clock.sleeps, [1, 1, 1, 5, 1, 1, 1])

    def test_only_remaining_interval_is_waited_after_shorter_backoff(self):
        fake = TimedHTTP(self.clock, failures={"search": [response(503, "error")]})
        self.http(fake)
        builder.configure_hltb_request_throttle(10)
        builder.search_hltb_global("Portal")
        self.assertEqual(self.assert_spacing(fake, 10), list(range(100, 180, 10)))
        self.assertEqual(self.clock.sleeps, [10, 10, 10, 5, 5, 10, 10, 10])

    def test_retry_after_and_http_interval_are_not_added_together(self):
        fake = TimedHTTP(self.clock, failures={"search": [response(503, "error", {"Retry-After": "20"})]})
        self.http(fake)
        builder.search_hltb_global("Portal")
        self.assertEqual(self.assert_spacing(fake), [100, 101, 102, 103, 123, 124, 125, 126])
        self.assertEqual(self.clock.sleeps, [1, 1, 1, 20, 1, 1, 1])

    def test_steam_dispatch_is_unthrottled_and_does_not_reset_hltb_interval(self):
        fake = TimedHTTP(self.clock)
        self.http(fake)
        builder.search_hltb_global("Portal")
        requests.get("https://steam.invalid/")
        builder.search_hltb_global("Missing")
        self.assertEqual([event[0] for event in fake.dispatches], [100, 101, 102, 103, 103, 104, 105, 106, 107])
        self.assertEqual(self.clock.sleeps, [1] * 7)

    def test_matcher_result_is_identical_with_throttle_enabled(self):
        fake = TimedHTTP(self.clock, games={"The Example": [
            {"game_id": 20, "game_name": "The Example", "release_world": 2020,
             "comp_main": 36000, "comp_plus": 55800, "comp_100": 79200, "comp_all": 43200},
        ]})
        self.http(fake)
        before = matcher._search_hltb("The Example Complete Edition")
        fake.dispatches.clear()
        after = builder.search_hltb_global("The Example Complete Edition")
        self.assertEqual(after, before)
        self.assertEqual(after["hltb_id"], 20)
        self.assertEqual(self.assert_spacing(fake), list(range(100, 108)))


class ProtectionTests(OfflineTest):
    def test_retry_after_delta_date_and_invalid_values(self):
        now = datetime(2026, 10, 11, tzinfo=timezone.utc).timestamp()
        self.assertEqual(retry_after_seconds("20", now), 20)
        self.assertEqual(retry_after_seconds("Sun, 11 Oct 2026 00:00:30 GMT", now), 30)
        self.assertEqual(retry_after_seconds("Sat, 10 Oct 2026 00:00:00 GMT", now), 0)
        for value in (None, "bad", "-1", "NaN", "inf", "1.5", "", "9" * 1000):
            self.assertIsNone(retry_after_seconds(value, now))

    def test_recovery_after_503_timeout_and_invalid_json(self):
        for result in (response(503, "bad"), requests.Timeout("timeout"), response(body="not JSON")):
            with self.subTest(result=type(result).__name__):
                fake = FakeHTTP({"search": [result]})
                with patch("requests.adapters.HTTPAdapter.send", side_effect=fake.send):
                    protection = ServiceProtection()
                    self.assertIsNone(builder.search_hltb_global("Portal", protection=protection))
                    self.assertEqual(protection.failures, 0)
                    self.assertEqual(sum(p == "search" for p, _, _ in fake.calls), 2)
        self.assertEqual(self.sleep.call_args_list, [((5.0,),)] * 3)

    def test_short_retry_after_respected(self):
        self.http(FakeHTTP({"search": [response(503, "bad", {"Retry-After": "20"})]}))
        self.assertIsNone(builder.search_hltb_global("Portal"))
        self.sleep.assert_called_once_with(20)

    def test_invalid_retry_after_uses_normal_backoff(self):
        self.http(FakeHTTP({"search": [response(503, "bad", {"Retry-After": "invalid"})]}))
        self.assertIsNone(builder.search_hltb_global("Portal"))
        self.sleep.assert_called_once_with(5)

    def test_retry_after_http_date_respected(self):
        self.http(FakeHTTP({"search": [response(503, "bad", {"Retry-After": "Sun, 11 Oct 2026 00:00:30 GMT"})]}))
        with patch.object(builder.time, "time", return_value=datetime(2026, 10, 11, tzinfo=timezone.utc).timestamp()):
            self.assertIsNone(builder.search_hltb_global("Portal"))
        self.sleep.assert_called_once_with(30)

    def test_long_retry_after_suspends_without_early_retry(self):
        fake = FakeHTTP({"search": [response(503, "bad", {"Retry-After": "3600"})]})
        self.http(fake)
        with self.assertRaises(HLTBSuspended) as caught:
            builder.search_hltb_global("Portal")
        self.assertEqual(caught.exception.delay, 3600)
        self.assertEqual(sum(p == "search" for p, _, _ in fake.calls), 1)
        self.sleep.assert_not_called()

    def test_429_and_403_suspend_immediately(self):
        for status in (429, 403):
            with self.subTest(status=status):
                fake = FakeHTTP({"discovery": [response(status, "bad", {"Retry-After": "10"})]})
                with patch("requests.adapters.HTTPAdapter.send", side_effect=fake.send):
                    with self.assertRaises(HLTBSuspended) as caught:
                        builder.search_hltb_global("Portal")
                    self.assertEqual(caught.exception.delay, 300)
                    self.assertEqual(len(fake.calls), 1)
        self.sleep.assert_not_called()

    def test_progressive_backoff_and_failure_limit(self):
        protection = ServiceProtection()
        error = HLTBFailure("search", "timeout")
        self.assertEqual(protection.failure(error), 5)
        self.assertEqual(protection.failure(error), 10)
        with self.assertRaises(HLTBSuspended):
            protection.failure(error)
        protection.success()
        self.assertEqual(protection.failure(error), 5)


class SQLiteProgressTests(OfflineTest):
    def setUp(self):
        super().setUp()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "global.db"
        # Existing DB helpers use transaction contexts, which do not close
        # sqlite connections. Keep and close test connections explicitly.
        connections = []
        connect = db.connect
        def tracked_connect(path):
            conn = connect(path)
            connections.append(conn)
            return conn
        tracked = patch.object(db, "connect", side_effect=tracked_connect)
        tracked.start()
        self.addCleanup(tracked.stop)
        self.addCleanup(lambda: [conn.close() for conn in connections])
        db.init_database(self.path)
        db.upsert_catalog(self.path, [{"appid": i, "name": name} for i, name in enumerate(("Portal", "Missing", "Later"), 1)])

    def rows(self):
        with closing(sqlite3.connect(self.path)) as conn:
            return conn.execute("SELECT appid,hltb_dirty,hltb_error_count,hltb_retry_after FROM steam_catalog ORDER BY appid").fetchall()

    def test_saved_progress_survives_and_unprocessed_games_remain_eligible(self):
        fake = FakeHTTP({"search": [
            response(body={"data": [{"game_id": 1, "game_name": "Portal", "comp_main": 3600}]}),
            response(503, "bad"), response(503, "bad"), response(503, "bad"),
        ]})
        self.http(fake)
        builder.update_hltb(self.path, limit=3, pause_seconds=0)
        self.assertEqual(self.rows(), [(1, 0, 0, None), (2, 1, 0, None), (3, 1, 0, None)])
        self.assertEqual([r["appid"] for r in db.hltb_candidates(self.path, 3)], [2, 3])
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("SELECT appid,state,main_story FROM hltb_metadata").fetchall(), [(1, "matched", 1.0)])
        self.assertGreater(float(db.get_state(self.path, builder.HLTB_SERVICE_RETRY_STATE)), builder.time.time())
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [5, 10])
        call_count = len(fake.calls)
        builder.update_hltb(self.path, limit=3, pause_seconds=0)
        self.assertEqual(len(fake.calls), call_count)
        # After the global pause expires, the same unresolved games can run.
        deadline = float(db.get_state(self.path, builder.HLTB_SERVICE_RETRY_STATE))
        with patch.object(builder.time, "time", return_value=deadline + 1):
            builder.update_hltb(self.path, limit=3, pause_seconds=0)
        self.assertEqual(self.rows(), [(1, 0, 0, None), (2, 0, 0, None), (3, 0, 0, None)])

    def test_rate_limit_persists_pause_without_touching_appids(self):
        self.http(FakeHTTP({"authentication": [response(429, "bad", {"Retry-After": "3600"})]}))
        with patch.object(builder.time, "time", return_value=1000):
            builder.update_hltb(self.path, limit=3, pause_seconds=0)
            self.assertEqual(float(db.get_state(self.path, builder.HLTB_SERVICE_RETRY_STATE)), 4600)
        self.assertEqual(self.rows(), [(1, 1, 0, None), (2, 1, 0, None), (3, 1, 0, None)])
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM hltb_metadata").fetchone()[0], 0)

    def test_persistent_rate_limit_with_http_spacing(self):
        clock = FakeClock()
        self.sleep.side_effect = clock.sleep
        fake = TimedHTTP(clock, failures={"authentication": [response(429, "error", {"Retry-After": "3600"})]})
        self.http(fake)
        with patch.object(builder.time, "monotonic", side_effect=clock.monotonic), patch.object(builder.time, "time", side_effect=clock.wall_time):
            builder.update_hltb(self.path, limit=3, pause_seconds=1)
            self.assertEqual([event[0] for event in fake.dispatches], [100, 101, 102])
            self.assertEqual(float(db.get_state(self.path, builder.HLTB_SERVICE_RETRY_STATE)), clock.wall_time() + 3600)
            self.assertEqual(self.rows(), [(1, 1, 0, None), (2, 1, 0, None), (3, 1, 0, None)])
            builder.update_hltb(self.path, limit=3, pause_seconds=1)
            self.assertEqual(len(fake.dispatches), 3)
            clock.now += 3601
            builder.update_hltb(self.path, limit=3, pause_seconds=1)
            starts = [event[0] for event in fake.dispatches]
            self.assertTrue(all(b - a >= 1 for a, b in zip(starts, starts[1:])))
            self.assertEqual(self.rows(), [(1, 0, 0, None), (2, 0, 0, None), (3, 0, 0, None)])

    def test_global_failures_keep_progress_with_http_throttle(self):
        clock = FakeClock()
        self.sleep.side_effect = clock.sleep
        fake = TimedHTTP(clock, failures={"search": [
            response(body={"data": [{"game_id": 1, "game_name": "Portal", "comp_main": 3600}]}),
            response(503, "bad"), response(503, "bad"), response(503, "bad"),
        ]})
        self.http(fake)
        with patch.object(builder.time, "monotonic", side_effect=clock.monotonic), patch.object(builder.time, "time", side_effect=clock.wall_time):
            builder.update_hltb(self.path, limit=3, pause_seconds=1)
        self.assertEqual([event[0] for event in fake.dispatches],
                         [100, 101, 102, 103, 104, 105, 106, 107, 112, 113, 114, 115, 125, 126, 127, 128])
        self.assertEqual(self.rows(), [(1, 0, 0, None), (2, 1, 0, None), (3, 1, 0, None)])
        self.assertEqual([game["appid"] for game in db.hltb_candidates(self.path, 3)], [2, 3])
        self.assertGreater(float(db.get_state(self.path, builder.HLTB_SERVICE_RETRY_STATE)), clock.wall_time())
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("SELECT appid,state,main_story FROM hltb_metadata").fetchall(), [(1, "matched", 1.0)])

    def test_game_specific_parsing_error_retains_existing_cooldown(self):
        self.http(FakeHTTP(games={"Portal": [{"game_id": 1, "game_name": "Portal", "comp_main": None}]}))
        builder.update_hltb(self.path, limit=3, pause_seconds=0)
        rows = self.rows()
        self.assertEqual(rows[0][:3], (1, 1, 1))
        self.assertIsNotNone(rows[0][3])
        self.assertEqual(rows[1:], [(2, 0, 0, None), (3, 0, 0, None)])
        self.assertIsNone(db.get_state(self.path, builder.HLTB_SERVICE_RETRY_STATE))

    def test_repeated_item_parsing_failure_suspends_further_cooldowns(self):
        db.upsert_catalog(self.path, [{"appid": 4, "name": "Unprocessed"}])
        self.http(FakeHTTP(games={name: [{"game_id": i, "game_name": name, "comp_main": None}]
                                 for i, name in enumerate(("Portal", "Missing", "Later"), 1)}))
        builder.update_hltb(self.path, limit=4, pause_seconds=0)
        rows = self.rows()
        self.assertEqual([row[2] for row in rows], [1, 1, 0, 0])
        self.assertEqual([row[1] for row in rows], [1, 1, 1, 1])
        self.assertGreater(float(db.get_state(self.path, builder.HLTB_SERVICE_RETRY_STATE)), builder.time.time())
        self.assertEqual([r["appid"] for r in db.hltb_candidates(self.path, 4)], [3, 4])
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM hltb_metadata").fetchone()[0], 0)

    def test_later_variant_failure_never_saves_partial_match_or_no_match(self):
        db.upsert_catalog(self.path, [{"appid": 1, "name": "The Example: Game"}])
        fake = FakeHTTP({"search": [
            response(body={"data": [{"game_id": 1, "game_name": "The Example: Game", "comp_main": 3600}]}),
            response(503, "bad"), response(503, "bad"), response(503, "bad"),
        ]})
        self.http(fake)
        builder.update_hltb(self.path, limit=3, pause_seconds=0)
        self.assertEqual(self.rows(), [(1, 1, 0, None), (2, 1, 0, None), (3, 1, 0, None)])
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM hltb_metadata").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
