from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time

import requests
from howlongtobeatpy import HowLongToBeat


REPO_ROOT = Path(__file__).resolve().parents[1]

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from constants import (  # noqa: E402
    HLTB_ERROR_BACKOFF_SECONDS,
    HLTB_REPEATED_ERROR_BACKOFF_SECONDS,
)
import hltb_service  # noqa: E402
from steam_reviews import (  # noqa: E402
    ReviewClient, ReviewResponseError, is_service_failure, is_transient_failure, retry_delay,
)
from steam_fetcher import (  # noqa: E402
    get_store_details,
)

from global_database import (  # noqa: E402
    GLOBAL_STEAM_REVIEWS_ERROR_COOLDOWN_SECONDS,
    get_state,
    hltb_candidates,
    init_database,
    save_hltb_error,
    save_hltb_match,
    save_hltb_no_match,
    save_steam_ready,
    save_steam_reviews,
    save_steam_unavailable,
    set_state,
    stats,
    steam_details_candidates,
    steam_review_candidates,
    steam_review_selection_counts,
    save_steam_error,
    upsert_catalog,
)


STEAM_APP_LIST_URL = (
    "https://api.steampowered.com/"
    "IStoreService/GetAppList/v1/"
)

DEFAULT_DB = (
    Path(__file__).resolve().parent
    / "data"
    / "global_metadata.db"
)

DEFAULT_CATALOG_PAGE_SIZE = 50_000
DEFAULT_STEAM_LIMIT = 100
DEFAULT_HLTB_LIMIT = 500

DEFAULT_STEAM_PAUSE = 2.0
DEFAULT_HLTB_PAUSE = 2.0

# HLTB request throttle.
#
# The matcher can issue multiple search requests for a single Steam title
# (normalised title, punctuation variant, edition fallback, structural
# fallback, etc.). Throttling per game therefore does not reliably limit the
# pressure placed on HLTB. The global builder instead enforces a minimum gap
# between howlongtobeatpy search() calls, including retries and fallbacks.
_hltb_request_lock = threading.Lock()
_hltb_last_request_started = 0.0
_hltb_request_interval = DEFAULT_HLTB_PAUSE
_hltb_retry_delay = float(HLTB_ERROR_BACKOFF_SECONDS)


def configure_hltb_request_throttle(interval_seconds):
    global _hltb_request_interval
    _hltb_request_interval = max(0.0, float(interval_seconds))


def _run_hltb_request(query):
    global _hltb_last_request_started

    with _hltb_request_lock:
        now = time.monotonic()
        wait_seconds = (
            _hltb_request_interval
            - (now - _hltb_last_request_started)
        )

        if wait_seconds > 0:
            time.sleep(wait_seconds)

        _hltb_last_request_started = time.monotonic()

        return HowLongToBeat(0.4).search(
            query,
            similarity_case_sensitive=False,
        )


def _throttled_hltb_search_once(query):
    results = _run_hltb_request(query)

    if results is not None:
        return _validate_hltb_results(results)

    # One retry for a temporary HLTB/request failure. The existing builder
    # backoff still applies if the retry also fails.
    time.sleep(max(0.0, _hltb_retry_delay))

    results = _run_hltb_request(query)

    if results is None:
        raise RuntimeError(
            f"HowLongToBeat search failed for query {query!r}"
        )

    return _validate_hltb_results(results)


def _validate_hltb_results(results):
    # Only a valid empty result means no match. Malformed responses must leave
    # the game pending, even if the matcher would otherwise skip bad entries.
    if not isinstance(results, list):
        raise ValueError("HowLongToBeat returned an invalid search result")
    for entry in results:
        try:
            game_id = int(getattr(entry, "game_id", None))
        except (TypeError, ValueError) as error:
            raise ValueError("HowLongToBeat returned an invalid game ID") from error
        name = getattr(entry, "game_name", None)
        if game_id <= 0 or not isinstance(name, str) or not name.strip():
            raise ValueError("HowLongToBeat returned an invalid game entry")
    return results


def search_hltb_global(game_name):
    """Run the app's exact matcher with global-builder request throttling."""
    original_search_once = hltb_service._search_once
    hltb_service._search_once = _throttled_hltb_search_once

    try:
        return hltb_service._search_hltb(game_name)
    finally:
        hltb_service._search_once = original_search_once


def get_catalog_page(
    api_key,
    *,
    last_appid=0,
    if_modified_since=None,
    max_results=DEFAULT_CATALOG_PAGE_SIZE,
):
    request_data = {
        "include_games": True,
        "include_dlc": False,
        "include_software": False,
        "include_videos": False,
        "include_hardware": False,
        "last_appid": int(last_appid),
        "max_results": int(max_results),
    }

    if if_modified_since is not None:
        request_data["if_modified_since"] = int(if_modified_since)

    response = requests.get(
        STEAM_APP_LIST_URL,
        headers={
            "x-webapi-key": api_key,
        },
        params={
            "input_json": json.dumps(request_data),
        },
        timeout=60,
    )

    if not response.ok:
        raise RuntimeError(
            "Steam catalogue request failed with "
            f"HTTP {response.status_code}."
        )

    payload = response.json().get("response") or {}
    apps = payload.get("apps") or []

    have_more = bool(
        payload.get("have_more_results")
    )

    returned_last_appid = payload.get("last_appid")

    if returned_last_appid is None and apps:
        returned_last_appid = apps[-1].get("appid")

    return (
        apps,
        have_more,
        int(returned_last_appid or 0),
    )


def sync_catalog(
    db_path,
    api_key,
    *,
    page_size,
    max_pages=None,
):
    previous_sync = get_state(
        db_path,
        "steam_catalog_complete_sync_unix",
    )

    if_modified_since = (
        int(previous_sync)
        if previous_sync is not None
        else None
    )

    sync_started_at = int(time.time())

    mode = (
        "incremental"
        if if_modified_since is not None
        else "initial"
    )

    print(
        f"[catalog] Starting {mode} Steam catalogue sync."
    )

    total_received = 0
    page_number = 0
    last_appid = 0
    completed = False

    while True:
        apps, have_more, next_last_appid = get_catalog_page(
            api_key,
            last_appid=last_appid,
            if_modified_since=if_modified_since,
            max_results=page_size,
        )

        page_number += 1
        total_received += upsert_catalog(
            db_path,
            apps,
        )

        print(
            f"[catalog] Page {page_number}: "
            f"{len(apps)} rows "
            f"({total_received} total received)"
        )

        if not have_more or not apps:
            completed = True
            break

        if (
            max_pages is not None
            and page_number >= max_pages
        ):
            print(
                "[catalog] Test page limit reached; "
                "sync is intentionally NOT marked complete."
            )
            break

        if next_last_appid <= last_appid:
            raise RuntimeError(
                "Steam catalogue pagination did not advance."
            )

        last_appid = next_last_appid

    if completed:
        set_state(
            db_path,
            "steam_catalog_complete_sync_unix",
            sync_started_at,
        )
        print(
            "[catalog] Complete sync timestamp saved."
        )

    print(
        f"[catalog] Finished: {total_received} rows received."
    )


DEFAULT_REVIEWS_PAUSE = 2.0
DEFAULT_STEAM_BUDGET_SECONDS = 150 * 60
DEFAULT_STEAM_FAILURE_THRESHOLD = 3


def _lane_report(limit):
    return dict(limit=limit, selected=0, attempted=0, updated=0, unavailable=0,
                skipped=0, errors=0, deferred=False, budget_exhausted=False,
                fatal_error=None, error_samples=[], consecutive_failures=0, stop_reason=None)


def _finish_lane_report(report, started):
    report["completed"] = report["updated"] + report["unavailable"]
    report["pending"] = report["selected"] - report["completed"]
    report["not_attempted"] = report["selected"] - report["attempted"]
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)


def _record_steam_error(db_path, service, appid, error, report, failure_threshold):
    report["errors"] += 1
    if len(report["error_samples"]) < 10:
        report["error_samples"].append(dict(appid=appid, type=type(error).__name__, message=str(error)))
    print(f"[steam-{service}] Error {appid}: {error}", flush=True)
    # Leave both cached data and successful fetch dates untouched. A per-row
    # delay prevents a permanently bad app from monopolising the next batch.
    service_failure = is_service_failure(error)
    transient_failure = is_transient_failure(error)
    delay = retry_delay(error)
    if service == "reviews" and not service_failure and not transient_failure:
        delay = max(delay, GLOBAL_STEAM_REVIEWS_ERROR_COOLDOWN_SECONDS)
        report["individual_errors"] += 1
    save_steam_error(db_path, appid, service, error, delay)
    report["consecutive_failures"] = (report["consecutive_failures"] + 1
                                      if transient_failure else 0)
    if service_failure or report["consecutive_failures"] >= failure_threshold:
        key = f"steam_{service}_retry_after"
        until = time.time() + retry_delay(error)
        previous = float(get_state(db_path, key, "0"))
        set_state(db_path, key, max(previous, until))
        report["deferred"] = True
        report["stop_reason"] = ("service_cooldown" if service_failure
                                 else "consecutive_transient_failures")
        print(f"[steam-{service}] Service cooldown; other workers continue.", flush=True)
        return True
    return False


def _lane_cooling_down(db_path, service, report):
    until = float(get_state(db_path, f"steam_{service}_retry_after", "0"))
    # The migration's shared cooldown originated predominantly from reviews.
    # Honour it there without carrying its restriction into Store details.
    if service == "reviews":
        until = max(until, float(get_state(db_path, "steam_service_retry_after", "0")))
    if time.time() < until:
        report["deferred"] = True
        report["stop_reason"] = "persisted_cooldown"
        return True
    return False


def _update_details(db_path, report, pause_seconds, deadline, done,
                    failure_threshold=DEFAULT_STEAM_FAILURE_THRESHOLD):
    started = time.monotonic()
    try:
        games = steam_details_candidates(db_path, report["limit"])
        report["selected"] = len(games)
        if not games or _lane_cooling_down(db_path, "details", report):
            return
        for index, game in enumerate(games):
            if index:
                remaining = deadline - time.monotonic()
                if remaining <= pause_seconds:
                    report["budget_exhausted"] = True
                    break
                time.sleep(max(0.0, pause_seconds))
            if time.monotonic() >= deadline:
                report["budget_exhausted"] = True
                break
            appid = int(game["appid"])
            report["attempted"] += 1
            try:
                details = get_store_details(appid)
            except (requests.RequestException, ValueError) as error:
                if _record_steam_error(db_path, "details", appid, error, report, failure_threshold):
                    break
                continue
            # DB/programming failures propagate; they are not acquisition errors.
            report["consecutive_failures"] = 0
            if details is None:
                save_steam_unavailable(db_path, appid)
                report["unavailable"] += 1
            else:
                save_steam_ready(db_path, details)
                report["updated"] += 1
            if report["attempted"] % 100 == 0:
                print(f"[steam-details] {report['attempted']}/{len(games)} | {report['updated']} updated", flush=True)
    finally:
        _finish_lane_report(report, started)
        done.set()


def _update_reviews(db_path, report, interval, deadline, details_done,
                    failure_threshold=DEFAULT_STEAM_FAILURE_THRESHOLD):
    started = time.monotonic()
    # The lane owns the consecutive-failure breaker; the client still enforces
    # request spacing and unconditional rate-limit/Retry-After backoff.
    client = ReviewClient(interval=interval, circuit_on_invalid=False, circuit_on_transient=False)
    attempted_appids = set()
    report.update(eligible=0, cooldown_excluded=0, min_age_excluded=0,
                  individual_errors=0, unused_capacity=report["limit"])
    try:
        if report["limit"] <= 0:
            return
        report.update(steam_review_selection_counts(db_path))
        while report["attempted"] < report["limit"]:
            # Read done BEFORE querying, so the last detail cannot be missed.
            complete = details_done.is_set()
            games = steam_review_candidates(db_path, report["limit"] - report["attempted"],
                                            exclude_appids=attempted_appids)
            report["selected"] += len(games)
            if _lane_cooling_down(db_path, "reviews", report):
                return
            if time.monotonic() + interval >= deadline:
                report["budget_exhausted"] = True
                return
            if not games:
                if complete:
                    break
                details_done.wait(min(0.25, max(0.0, deadline - time.monotonic())))
                continue
            for game in games:
                if time.monotonic() + interval >= deadline:
                    report["budget_exhausted"] = True
                    return
                appid = int(game["appid"])
                attempted_appids.add(appid)
                report["attempted"] += 1
                try:
                    summary = client.get_summary(appid)
                    if summary is None:
                        raise ReviewResponseError("Steam reviews summary is unavailable")
                except (requests.RequestException, ValueError) as error:
                    if _record_steam_error(db_path, "reviews", appid, error, report, failure_threshold):
                        return
                    continue
                report["consecutive_failures"] = 0
                if save_steam_reviews(db_path, appid, summary):
                    report["updated"] += 1
                else:
                    report["skipped"] += 1
                if report["attempted"] % 100 == 0:
                    print(f"[steam-reviews] {report['attempted']}/{report['limit']} | {report['updated']} updated", flush=True)
    finally:
        try:
            if report["limit"] > 0:
                remaining = steam_review_selection_counts(db_path, exclude_appids=attempted_appids)
                # Initial/final snapshots account for concurrent Store arrivals
                # without scanning the full catalogue on each idle poll.
                report["eligible"] = max(report["eligible"], report["attempted"] + remaining["eligible"])
                for key in ("cooldown_excluded", "min_age_excluded"):
                    report[key] = max(report[key], remaining[key])
        finally:
            report["unused_capacity"] = max(0, report["limit"] - report["attempted"])
            _finish_lane_report(report, started)


def update_steam(db_path, *, limit, pause_seconds, reviews_limit=None,
                 reviews_pause_seconds=DEFAULT_REVIEWS_PAUSE,
                 budget_seconds=DEFAULT_STEAM_BUDGET_SECONDS,
                 failure_threshold=DEFAULT_STEAM_FAILURE_THRESHOLD):
    """Run independent Store and reviews lanes, each with its own budget."""
    if type(failure_threshold) is not int or failure_threshold < 1:
        raise ValueError("Steam failure threshold must be a positive integer")
    details = _lane_report(limit)
    reviews = _lane_report(limit if reviews_limit is None else reviews_limit)
    done = threading.Event()
    deadline = time.monotonic() + budget_seconds
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="steam") as executor:
        futures = (
            (details, executor.submit(_update_details, db_path, details, pause_seconds, deadline, done, failure_threshold)),
            (reviews, executor.submit(_update_reviews, db_path, reviews, reviews_pause_seconds, deadline, done, failure_threshold)),
        )
        for lane, future in futures:
            try:
                future.result()
            except Exception as error:
                lane["fatal_error"] = f"{type(error).__name__}: {error}"
                print(f"[steam] FATAL: {lane['fatal_error']}", flush=True)
    report = dict(details=details, reviews=reviews)
    print("[steam] Run summary: " + json.dumps(report, sort_keys=True), flush=True)
    return report


def steam_run_failed(report):
    return bool(report and any(report[lane]["fatal_error"] for lane in ("details", "reviews")))


def steam_run_partial(report):
    return bool(report and any(report[lane]["errors"] or report[lane]["deferred"]
                              or report[lane]["budget_exhausted"] for lane in ("details", "reviews")))


def update_hltb(
    db_path,
    *,
    limit,
    pause_seconds,
):
    report = _lane_report(limit)
    report.update(matched=0, no_match=0)
    started = time.monotonic()
    try:
        _update_hltb(db_path, report, limit=limit, pause_seconds=pause_seconds)
    except Exception as error:
        report["fatal_error"] = f"{type(error).__name__}: {error}"
        print(f"[hltb] FATAL: {report['fatal_error']}", flush=True)
    finally:
        _finish_lane_report(report, started)
    print("[hltb] Run summary: " + json.dumps(report, sort_keys=True), flush=True)
    return report


def _update_hltb(db_path, report, *, limit, pause_seconds):
    games = hltb_candidates(
        db_path,
        limit,
    )
    report["selected"] = len(games)

    if not games:
        print("[hltb] Nothing to process.")
        return

    configure_hltb_request_throttle(
        pause_seconds
    )

    print(
        f"[hltb] Processing {len(games)} games."
    )
    print(
        "[hltb] Request throttle: "
        f"{max(0.0, pause_seconds):.1f}s minimum between search() calls; "
        f"one retry after {_hltb_retry_delay:.1f}s on request failure."
    )

    matched = 0
    no_match = 0
    errors = 0
    consecutive_errors = 0

    for index, game in enumerate(games, start=1):
        appid = int(game["appid"])
        name = str(game["name"] or "").strip()
        report["attempted"] += 1

        try:
            # Reuse the exact v1.3.0 matcher without changing hltb_service.py.
            result = search_hltb_global(name)
        except sqlite3.DatabaseError:
            raise
        except Exception as error:
            # Never persist a temporary/network error as no_match.
            errors += 1
            report["errors"] += 1
            if len(report["error_samples"]) < 10:
                report["error_samples"].append(dict(appid=appid, type=type(error).__name__, message=str(error)))
            consecutive_errors += 1

            cooldown = save_hltb_error(
                db_path,
                appid,
                error,
            )

            print(
                f"[hltb] Error {appid} {name!r}: {error} | "
                f"retry #{cooldown['error_count']} "
                f"in {cooldown['cooldown_hours']}h"
            )

            backoff = (
                HLTB_REPEATED_ERROR_BACKOFF_SECONDS
                if consecutive_errors > 1
                else HLTB_ERROR_BACKOFF_SECONDS
            )

            time.sleep(max(0.0, backoff))
        else:
            # Persistence/integrity failures must never become acquisition errors.
            if result is None:
                save_hltb_no_match(db_path, appid, name)
                no_match += 1
                report["no_match"] += 1
            else:
                save_hltb_match(db_path, appid, name, result)
                matched += 1
                report["matched"] += 1
            report["updated"] += 1
            consecutive_errors = 0

        if index % 10 == 0 or index == len(games):
            print(
                f"[hltb] {index}/{len(games)} | "
                f"{matched} matched, "
                f"{no_match} no match, "
                f"{errors} errors"
            )

        # No per-game sleep here. Each search() call, including matcher
        # fallbacks, already respects the minimum interval.


def print_stats(db_path):
    values = stats(db_path)

    print("")
    print("[global-db] Current totals")
    print(f"  Catalogue games:    {values['catalog']}")
    print(f"  Steam ready:        {values['steam_ready']}")
    print(f"  Steam unavailable:  {values['steam_unavailable']}")
    print(f"  Steam reviews:      {values['steam_reviews_cached']}")
    print(f"  HLTB matched:       {values['hltb_matched']}")
    print(f"  HLTB no match:      {values['hltb_no_match']}")
    print(f"  HLTB cooldown:      {values['hltb_cooldown']}")


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Build/update the Pecetracker "
            "global Steam + HLTB metadata database."
        )
    )

    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB),
    )

    parser.add_argument(
        "--steam-limit",
        type=int,
        default=DEFAULT_STEAM_LIMIT,
    )

    parser.add_argument(
        "--hltb-limit",
        type=int,
        default=DEFAULT_HLTB_LIMIT,
    )

    parser.add_argument(
        "--steam-pause",
        type=float,
        default=DEFAULT_STEAM_PAUSE,
    )

    parser.add_argument(
        "--hltb-pause",
        type=float,
        default=DEFAULT_HLTB_PAUSE,
        help=(
            "Minimum seconds between howlongtobeatpy search() calls, "
            "including retries and matcher fallbacks."
        ),
    )

    parser.add_argument(
        "--catalog-page-size",
        type=int,
        default=DEFAULT_CATALOG_PAGE_SIZE,
    )

    parser.add_argument(
        "--catalog-max-pages",
        type=int,
        default=None,
        help=(
            "Testing only. Stop after N catalogue pages. "
            "An incomplete catalogue sync is never marked complete."
        ),
    )

    parser.add_argument(
        "--skip-catalog",
        action="store_true",
    )

    parser.add_argument(
        "--stats-only",
        action="store_true",
    )

    parser.add_argument("--steam-reviews-limit", type=int, default=None,
                        help="Independent reviews budget; defaults to --steam-limit")
    parser.add_argument("--steam-reviews-pause", type=float, default=DEFAULT_REVIEWS_PAUSE,
                        help="Minimum seconds between anonymous review request starts")
    parser.add_argument("--steam-budget-seconds", type=float, default=DEFAULT_STEAM_BUDGET_SECONDS,
                        help="Time budget per Steam lane; reserves time for publication")
    parser.add_argument("--steam-failure-threshold", type=int, default=DEFAULT_STEAM_FAILURE_THRESHOLD,
                        help="Consecutive transport/5xx failures before stopping a Steam lane")
    parser.add_argument("--report", help="Write acquisition counters/status as JSON for CI diagnostics")

    args = parser.parse_args()
    limits = (args.steam_limit, args.hltb_limit, args.steam_reviews_limit)
    if any(n is not None and n < 0 for n in limits):
        parser.error("Batch limits must be non-negative")
    if args.steam_failure_threshold < 1:
        parser.error("Steam failure threshold must be a positive integer")
    if any(not math.isfinite(n) or n < 0 for n in
           (args.steam_pause, args.steam_reviews_pause, args.steam_budget_seconds)):
        parser.error("Steam intervals and time budgets must be finite and non-negative")
    return args


def main():
    args = parse_args()
    steam_report = None
    hltb_report = None
    db_path = Path(args.db)

    init_database(db_path)

    if args.stats_only:
        print_stats(db_path)
        return 0

    if not args.skip_catalog:
        api_key = os.environ.get("STEAM_WEB_API_KEY")

        if not api_key:
            raise SystemExit(
                "STEAM_WEB_API_KEY is not set."
            )

        sync_catalog(
            db_path,
            api_key,
            page_size=args.catalog_page_size,
            max_pages=args.catalog_max_pages,
        )

    run_steam = args.steam_limit > 0 or (args.steam_reviews_limit or 0) > 0
    run_hltb = args.hltb_limit > 0

    if run_steam and run_hltb:
        print(
            "[workers] Running Steam and HLTB concurrently."
        )

        with ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="global-metadata",
        ) as executor:
            steam_future = executor.submit(
                update_steam,
                db_path,
                limit=args.steam_limit,
                pause_seconds=args.steam_pause,
                reviews_limit=args.steam_reviews_limit,
                reviews_pause_seconds=args.steam_reviews_pause,
                budget_seconds=args.steam_budget_seconds,
                failure_threshold=args.steam_failure_threshold,
            )
            hltb_future = executor.submit(
                update_hltb,
                db_path,
                limit=args.hltb_limit,
                pause_seconds=args.hltb_pause,
            )

            # Acquisition failures are reported as partial progress. Only unsafe
            # database/worker failures block publication.
            steam_report = steam_future.result()
            hltb_report = hltb_future.result()

    elif run_steam:
        steam_report = update_steam(
            db_path,
            limit=args.steam_limit,
            pause_seconds=args.steam_pause,
            reviews_limit=args.steam_reviews_limit,
            reviews_pause_seconds=args.steam_reviews_pause,
            budget_seconds=args.steam_budget_seconds,
            failure_threshold=args.steam_failure_threshold,
        )

    elif run_hltb:
        hltb_report = update_hltb(
            db_path,
            limit=args.hltb_limit,
            pause_seconds=args.hltb_pause,
        )

    print_stats(db_path)
    fatal = steam_run_failed(steam_report) or bool(hltb_report and hltb_report["fatal_error"])
    partial = steam_run_partial(steam_report) or bool(hltb_report and hltb_report["errors"])
    report = {"steam": steam_report, "hltb": hltb_report,
              "status": "fatal" if fatal else "partial" if partial else "ok",
              "publishable": not fatal}
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if fatal:
        print("[global-db] FATAL: publication blocked; inspect acquisition report.")
        return 1
    if partial:
        print("[global-db] PARTIAL: valid transactions may be published; failed requests remain due.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
