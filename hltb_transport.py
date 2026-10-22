"""Per-attempt HLTB transport without patching requests or other services.

The pinned wrapper exposes no synchronous session injection. Bind copies of
its four HTTP entry points to a private transport; its parser stays untouched.
"""
from contextlib import contextmanager
import importlib
import threading
import time
import types

import requests
from howlongtobeatpy import HowLongToBeat

_local = threading.local()
_request_lock = threading.Lock()
_last_started = 0.0
REQUEST_INTERVAL = .5
ATTEMPT_SECONDS = 30
MAX_REQUESTS = 40


class BudgetExceeded(RuntimeError):
    pass


class Transport:
    def __init__(self):
        self.deadline = time.monotonic() + ATTEMPT_SECONDS
        self.requests = 0

    def _request(self, method, *args, **kwargs):
        global _last_started
        with _request_lock:
            remaining = self.deadline - time.monotonic()
            pause = max(0, REQUEST_INTERVAL - (time.monotonic() - _last_started))
            if self.requests >= MAX_REQUESTS or remaining <= pause:
                raise BudgetExceeded("HLTB attempt budget exhausted; retry later")
            if pause:
                time.sleep(pause)
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise BudgetExceeded("HLTB attempt deadline reached")
            self.requests += 1
            _last_started = time.monotonic()
            kwargs["timeout"] = min(10, remaining)
            response = getattr(requests, method)(*args, **kwargs)
            if time.monotonic() > self.deadline:
                raise BudgetExceeded("HLTB attempt deadline reached")
            return response

    def get(self, *args, **kwargs):
        return self._request("get", *args, **kwargs)

    def post(self, *args, **kwargs):
        return self._request("post", *args, **kwargs)


@contextmanager
def search_budget():
    previous = getattr(_local, "transport", None)
    _local.transport = Transport()
    try:
        yield
    finally:
        _local.transport = previous


def client(similarity):
    transport = getattr(_local, "transport", None)
    instance = HowLongToBeat(similarity)
    if transport is None:
        return instance
    original = importlib.import_module("howlongtobeatpy.HTMLRequests").HTMLRequests
    bound = type("BudgetHTMLRequests", (original,), {})
    for name in ("send_web_request", "send_website_request_getcode", "send_website_get_auth_token", "get_game_title"):
        function = getattr(original, name)
        namespace = {**function.__globals__, "requests": transport, "HTMLRequests": bound}
        setattr(bound, name, staticmethod(types.FunctionType(
            function.__code__, namespace, name, function.__defaults__, function.__closure__
        )))
    for name in ("search", "search_from_id"):
        function = getattr(HowLongToBeat, name)
        namespace = {**function.__globals__, "HTMLRequests": bound}
        setattr(instance, name, types.MethodType(types.FunctionType(
            function.__code__, namespace, name, function.__defaults__, function.__closure__
        ), instance))
    return instance
