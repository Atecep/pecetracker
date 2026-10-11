"""Offline-testable diagnostics and service protection for the global builder.

The integration is deliberately scoped to howlongtobeatpy 1.0.23's synchronous
search. No authentication, endpoint or connection caching is introduced here.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
import importlib
import json
import math
import threading
import time
from types import SimpleNamespace

import requests
from howlongtobeatpy import HowLongToBeat


def retry_after_seconds(value, now=None):
    """Accept delta-seconds or an HTTP date; reject malformed header values."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value.isascii() and value.isdecimal():
        try:
            seconds = float(value)
        except (ValueError, OverflowError):
            return None
    else:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                return None
            seconds = max(0.0, date.timestamp() - (time.time() if now is None else now))
        except (ValueError, TypeError, OverflowError):
            return None
    return seconds if math.isfinite(seconds) else None


class HLTBFailure(RuntimeError):
    def __init__(self, phase, kind, status=None, retry_after=None, global_candidate=True, diagnostics=None):
        self.phase = phase
        self.kind = kind
        self.status = status
        self.retry_after = retry_after
        self.global_candidate = global_candidate
        self.diagnostics = diagnostics or {}
        # Do not embed underlying exception messages, URLs or response bodies.
        super().__init__(f"HLTB {phase}: {kind}" + (f" (HTTP {status})" if status is not None else ""))


class HLTBSuspended(RuntimeError):
    def __init__(self, failure, delay):
        self.failure = failure
        self.delay = delay
        super().__init__(f"{failure}; global processing suspended for at least {delay:.0f}s")


class ServiceProtection:
    """One retry per query, with a circuit across consecutive failed attempts."""
    BASE_DELAY = 5.0
    MAX_DELAY = 60.0
    FAILURE_LIMIT = 3
    SUSPEND_SECONDS = 300.0

    def __init__(self):
        self.failures = 0
        self.delay = 0.0
        self.parsing_failures = 0

    def success(self):
        self.failures = 0
        self.delay = 0.0
        self.parsing_failures = 0

    def failure(self, error):
        if not error.global_candidate:
            # An isolated bad item retains the existing AppID cooldown. The
            # same parser failing on consecutive games can indicate a wider
            # schema change; stop before assigning further local cooldowns.
            if error.kind == "parsing":
                self.parsing_failures += 1
                if self.parsing_failures >= self.FAILURE_LIMIT:
                    raise HLTBSuspended(error, self.SUSPEND_SECONDS)
            else:
                self.parsing_failures = 0
            raise error
        self.parsing_failures = 0
        self.failures += 1
        delay = min(self.MAX_DELAY, self.BASE_DELAY * 2 ** min(self.failures - 1, 8))
        requested = error.retry_after or 0.0
        if (
            error.status in (403, 429)
            or self.failures >= self.FAILURE_LIMIT
            or requested > self.MAX_DELAY
        ):
            raise HLTBSuspended(error, max(self.SUSPEND_SECONDS, requested))
        self.delay = max(delay, requested)
        return self.delay


class HTTPThrottle:
    """Shared start-to-start interval, including redirects and failed sends."""
    def __init__(self, interval=1.0):
        self.lock = threading.Lock()
        self.interval = 0.0
        self.last_started = None
        self.not_before = 0.0
        self.configure(interval)

    def configure(self, interval):
        interval = float(interval)
        if not math.isfinite(interval):
            raise ValueError("HLTB HTTP interval must be finite")
        with self.lock:
            self.interval = max(0.0, interval)

    def wait(self):
        with self.lock:
            entered = time.monotonic()
            deadline = self.not_before
            if self.last_started is not None:
                deadline = max(deadline, self.last_started + self.interval)
            now = entered
            # Recheck after sleep in case it returned early.
            while now < deadline:
                time.sleep(deadline - now)
                now = time.monotonic()
            self.last_started = now
            return now, now - entered

    def defer(self, seconds):
        with self.lock:
            self.not_before = max(self.not_before, time.monotonic() + seconds)


HLTB_HTTP_THROTTLE = HTTPThrottle()


class _HLTBHTTPAdapter(requests.adapters.HTTPAdapter):
    """Intercept each dispatch, while Requests retains redirect semantics."""
    def __init__(self, transport):
        super().__init__(max_retries=0)
        self.transport = transport

    def send(self, request, **kwargs):
        send = super().send
        return self.transport._send(lambda: send(request, **kwargs))


class HLTBTransport:
    def __init__(self, emit=print, throttle=None):
        self.emit = emit
        self.throttle = throttle if throttle is not None else HLTB_HTTP_THROTTLE
        self.requests = 0
        self.attempts = 0
        self.counts = Counter()
        self.records = []
        self.query = ""
        self.matching_phase = "original"
        self.attempt = 0
        self.phase = "discovery"

    def _log(self, prefix, record):
        self.emit(prefix + json.dumps(record, ensure_ascii=True, sort_keys=True))

    def log_summary(self):
        self._log("[hltb-http-summary] ", {
            "requests": self.requests,
            "search_attempts": self.attempts,
            "outcomes": [
                {"phase": phase, "status": status, "failure": failure, "count": count}
                for (phase, status, failure), count in sorted(
                    self.counts.items(), key=lambda item: tuple(str(value) for value in item[0]),
                )
            ],
        })

    def _phase(self, method, url):
        if method == "POST":
            return "search"
        if "/_next/static/chunks/" in url:
            return "script"
        if url.split("?", 1)[0].rstrip("/").endswith("/init"):
            return "authentication"
        return "discovery"

    def _send(self, send):
        phase = self.phase
        started, waited = self.throttle.wait()
        record = {
            "request": self.requests + 1,
            "query": self.query,
            "matching_phase": self.matching_phase,
            "attempt": self.attempt,
            "phase": phase,
            "started_at": time.time(),
            "started_monotonic": started,
            "throttle_wait_seconds": round(waited, 4),
            "status": None,
            "failure": None,
            "retry_after_seconds": None,
        }
        self.requests += 1
        self.records.append(record)
        try:
            response = send()
            record["status"] = response.status_code
            # Requests follows redirects after adapter.send returns. Respect
            # Retry-After before the next hop, without replacing its handling
            # of methods, relative URLs, cookies or the redirect limit.
            if response.is_redirect:
                requested = retry_after_seconds(response.headers.get("Retry-After"))
                record["retry_after_seconds"] = requested
                if requested is not None:
                    if requested > ServiceProtection.MAX_DELAY:
                        response.close()
                        raise HLTBFailure(phase, "http", response.status_code, requested)
                    self.throttle.defer(requested)
            return response
        except requests.Timeout:
            record["failure"] = "timeout"
            raise HLTBFailure(phase, "timeout") from None
        except requests.RequestException:
            record["failure"] = "network"
            raise HLTBFailure(phase, "network") from None
        finally:
            record["duration_seconds"] = round(time.monotonic() - started, 4)

    def _request(self, method, url, **kwargs):
        self.phase = self._phase(method, url)
        phase = self.phase
        try:
            # Match requests.get/post's per-call session lifetime. Only these
            # private sessions mount the adapter; Steam's sessions are untouched.
            with requests.Session() as session:
                adapter = _HLTBHTTPAdapter(self)
                session.mount("https://", adapter)
                session.mount("http://", adapter)
                response = session.request(method, url, **kwargs)
            record = self.records[-1]
            if response.status_code != 200:
                record["failure"] = "http"
                requested = retry_after_seconds(response.headers.get("Retry-After"))
                record["retry_after_seconds"] = requested
                if (
                    phase != "script" or response.status_code in (403, 429)
                    or response.status_code >= 500 or requested is not None
                ):
                    raise HLTBFailure(
                        phase, "http", response.status_code,
                        requested,
                        global_candidate=phase in ("discovery", "authentication")
                        or response.status_code in (401, 403, 404, 408, 429)
                        or response.status_code >= 500 or requested is not None,
                    )
            if phase == "authentication":
                try:
                    data = response.json()
                except ValueError:
                    record["authentication_diagnostics"] = {"json_valid": False}
                    raise HLTBFailure(phase, "invalid_json", response.status_code) from None
                record["authentication_diagnostics"] = {
                    "json_valid": True, "json_type": type(data).__name__,
                }
                if not isinstance(data, dict):
                    raise HLTBFailure(phase, "unexpected_type", response.status_code)
            if phase == "search":
                try:
                    data = json.loads(response.text)
                except (ValueError, TypeError):
                    raise HLTBFailure(phase, "invalid_json", response.status_code) from None
                if not isinstance(data, dict) or not isinstance(data.get("data"), list) or any(
                    not isinstance(entry, dict) for entry in data["data"]
                ):
                    raise HLTBFailure(phase, "invalid_response", response.status_code)
            return response
        # Exceptions while following redirects or consuming the body happen
        # above the adapter, and must retain Phase 1's classification too.
        except requests.Timeout:
            raise HLTBFailure(phase, "timeout") from None
        except requests.RequestException:
            raise HLTBFailure(phase, "network") from None

    @contextmanager
    def _instrument(self):
        module = importlib.import_module("howlongtobeatpy.HTMLRequests")
        cls = module.HTMLRequests
        original_requests = module.requests
        discovery = cls.__dict__["send_website_request_getcode"]
        authentication = cls.__dict__["send_website_get_auth_token"]
        original_url = cls.SEARCH_URL

        def discover(user_agent):
            result = discovery.__func__(user_agent)
            if result is None or not result.search_url:
                raise HLTBFailure("discovery", "endpoint_not_found")
            return result

        def authenticate(url, user_agent):
            def fail(kind, **diagnostics):
                record = next((r for r in reversed(self.records) if r["phase"] == "authentication"), None)
                if record is not None:
                    record.setdefault("authentication_diagnostics", {}).update(diagnostics)
                return HLTBFailure(
                    "authentication", kind, record["status"] if record else None,
                    diagnostics=record["authentication_diagnostics"] if record else diagnostics,
                )

            try:
                result = authentication.__func__(url, user_agent)
            except HLTBFailure:
                raise
            except Exception as error:
                # The response has already passed JSON/HTTP validation. An
                # exception here originates in the library's extraction code.
                raise fail("library_exception", exception_type=type(error).__name__) from None
            if result is None:
                raise fail("missing_fields", result_type="NoneType")
            if not isinstance(result, module.SearchAuthToken):
                raise fail("adapter_incompatibility", result_type=type(result).__name__)
            fields = {
                "token": result.auth_token, "key": result.auth_key, "value": result.auth_value,
            }
            diagnostics = {"fields": {
                name: {"available": value is not None, "type": type(value).__name__}
                for name, value in fields.items()
            }}
            # Record only logical field names, availability and types. Never
            # include response field names, authentication values or headers.
            record = next((r for r in reversed(self.records) if r["phase"] == "authentication"), None)
            if record is not None:
                record.setdefault("authentication_diagnostics", {}).update(diagnostics)
            if any(value is None for value in fields.values()) or any(
                isinstance(fields[name], str) and not fields[name] for name in ("token", "key")
            ):
                raise fail("missing_fields")
            # 1.0.23 stringifies header values and JSON-serialises the key in
            # the payload. Numeric keys/tokens therefore work in the original
            # library and must not be rejected by this adapter.
            if any(not isinstance(fields[name], (str, int, float, bool)) for name in ("token", "key")):
                raise fail("unexpected_type")
            return result

        module.requests = SimpleNamespace(
            get=lambda url, **kwargs: self._request("GET", url, **kwargs),
            post=lambda url, **kwargs: self._request("POST", url, **kwargs),
        )
        cls.send_website_request_getcode = staticmethod(discover)
        cls.send_website_get_auth_token = staticmethod(authenticate)
        try:
            yield
        finally:
            module.requests = original_requests
            cls.send_website_request_getcode = discovery
            cls.send_website_get_auth_token = authentication
            cls.SEARCH_URL = original_url

    def search(self, query, matching_phase, attempt):
        self.query, self.matching_phase, self.attempt = query, matching_phase, attempt
        self.phase = "discovery"
        self.records = []
        self.attempts += 1
        started = time.monotonic()
        failure = None
        results = None
        try:
            if not query or not query.strip():
                raise HLTBFailure("search", "invalid_query", global_candidate=False)
            with self._instrument():
                results = HowLongToBeat(0.4).search(query, similarity_case_sensitive=False)
            if results is None:
                raise HLTBFailure(self.phase, "empty_response")
            return results
        except HLTBFailure as error:
            failure = error
            raise
        except json.JSONDecodeError:
            failure = HLTBFailure(self.phase, "invalid_json")
            raise failure from None
        except (KeyError, TypeError, ValueError, AttributeError):
            failure = HLTBFailure(self.phase, "parsing", global_candidate=self.phase != "search")
            raise failure from None
        finally:
            if failure is not None and self.records and not (
                failure.phase == "authentication" and self.records[-1]["phase"] != "authentication"
            ):
                self.records[-1]["failure"] = failure.kind
            for record in self.records:
                self.counts[(record["phase"], record["status"], record["failure"])] += 1
                self._log("[hltb-http] ", record)
            self._log("[hltb-search] ", {
                "query": query, "matching_phase": matching_phase, "attempt": attempt,
                "duration_seconds": round(time.monotonic() - started, 4),
                "phase": failure.phase if failure else "search",
                "failure": failure.kind if failure else None,
                "failure_diagnostics": failure.diagnostics if failure else None,
                "status": failure.status if failure else None,
                "retry_after_seconds": failure.retry_after if failure else None,
                "results": len(results) if results is not None else None,
            })
