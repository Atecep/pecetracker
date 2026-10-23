"""Anonymous IUserReviewsService client shared by desktop and catalogue jobs.

Contract: https://partner.steamgames.com/doc/webapi/IUserReviewsService
Valve publishes no numeric anonymous quota. Space requests conservatively and
stop on errors rather than retrying a batch against the lower shared IP limit.
The personal Steam Web API key is deliberately never passed to this service.
"""
import json
import math
import threading
import time
from email.utils import parsedate_to_datetime

import requests

from version import APP_VERSION

REVIEWS_URL = "https://api.steampowered.com/IUserReviewsService/GetAppReviews/v1/"
ANONYMOUS_INTERVAL_SECONDS = 10.0


def retry_delay(error):
    """Honour numeric/HTTP-date Retry-After without shortening server delays."""
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    seconds = max(900 if status == 429 else 300,
                  getattr(error, "retry_after_seconds", 0))
    value = response.headers.get("Retry-After") if response is not None else None
    if value:
        try:
            delay = float(value)
        except (TypeError, ValueError):
            try:
                delay = parsedate_to_datetime(value).timestamp() - time.time()
            except (TypeError, ValueError, OverflowError):
                delay = 0
        if math.isfinite(delay):
            seconds = max(seconds, delay)
    return seconds


class ReviewResponseError(requests.RequestException):
    """An HTTP-successful response did not contain a valid first-page summary."""


class ReviewBackoff(requests.RequestException):
    def __init__(self, seconds):
        super().__init__("Steam reviews are cooling down; no request was sent.")
        self.retry_after_seconds = seconds


def parse_summary(payload):
    body = payload.get("response") if isinstance(payload, dict) else None
    summary = body.get("query_summary") if isinstance(body, dict) else None
    if not isinstance(summary, dict):
        raise ReviewResponseError("Steam reviews response has no query_summary.")

    counts = {}
    for field in ("total_positive", "total_negative", "total_reviews"):
        value = summary.get(field)
        if type(value) is not int or value < 0:
            raise ReviewResponseError(f"Steam reviews summary has invalid {field}.")
        counts[field] = value
    if counts["total_positive"] + counts["total_negative"] != counts["total_reviews"]:
        raise ReviewResponseError("Steam reviews counts are inconsistent.")
    score = summary.get("review_score")
    description = summary.get("review_score_desc")
    if type(score) is not int or not 0 <= score <= 9 or not isinstance(description, str):
        raise ReviewResponseError("Steam reviews score/description is missing or invalid.")
    return {**counts, "review_score": score, "review_description": description,
            "positive_percentage": round(counts["total_positive"] / counts["total_reviews"] * 100, 1)
            if counts["total_reviews"] else None}


class ReviewClient:
    def __init__(self, interval=ANONYMOUS_INTERVAL_SECONDS):
        self.interval = max(ANONYMOUS_INTERVAL_SECONDS, interval)
        self._lock = threading.Lock()
        self._last_request = None
        self._retry_at = 0.0

    def get_summary(self, appid):
        appid = int(appid)
        if not 0 < appid <= 0xffffffff:
            raise ValueError("AppID must be a positive uint32.")
        # First page + review_type=All is required for aggregate score fields.
        query = {"appid": appid, "filter": 0, "languages": ["all"],
                 "review_type": 0, "purchase_type": 1, "num_per_page": 1,
                 "cursor": "*", "display_language": "english",
                 "filter_offtopic_activity": True}
        with self._lock:
            now = time.monotonic()
            if now < self._retry_at:
                raise ReviewBackoff(self._retry_at - now)
            if self._last_request is not None:
                wait = self.interval - (now - self._last_request)
                if wait > 0:
                    time.sleep(wait)
            self._last_request = time.monotonic()
            try:
                response = requests.get(
                    REVIEWS_URL, params={"input_json": json.dumps(query)},
                    headers={"User-Agent": f"SteamLibraryTracker/{APP_VERSION}"},
                    timeout=15,
                )
                response.raise_for_status()
                if response.headers.get("x-eresult", "1") != "1":
                    raise ReviewResponseError("Steam reviews returned an API error.", response=response)
                try:
                    payload = response.json()
                except ValueError as error:
                    raise ReviewResponseError("Steam reviews returned invalid JSON.", response=response) from error
                return parse_summary(payload)
            except requests.RequestException as error:
                self._retry_at = time.monotonic() + retry_delay(error)
                raise


_client = ReviewClient()


def get_review_summary(appid):
    return _client.get_summary(appid)
