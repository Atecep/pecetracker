from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
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
from steam_fetcher import (  # noqa: E402
    get_review_summary,
    get_store_details,
)

from global_database import (  # noqa: E402
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
    steam_candidates,
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
        return results

    # One retry for a temporary HLTB/request failure. The existing builder
    # backoff still applies if the retry also fails.
    time.sleep(max(0.0, _hltb_retry_delay))

    results = _run_hltb_request(query)

    if results is None:
        raise RuntimeError(
            f"HowLongToBeat search failed for query {query!r}"
        )

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


def update_steam(
    db_path,
    *,
    limit,
    pause_seconds,
):
    games = steam_candidates(
        db_path,
        limit,
    )

    if not games:
        print("[steam] Nothing to process.")
        return

    print(
        f"[steam] Processing {len(games)} games."
    )

    details_ready = 0
    reviews_refreshed = 0
    unavailable = 0
    errors = 0

    for index, game in enumerate(games, start=1):
        appid = int(game["appid"])
        name = game["name"]
        refresh_kind = game.get(
            "refresh_kind",
            "full",
        )

        if refresh_kind == "reviews":
            try:
                reviews = get_review_summary(
                    appid
                )

                if reviews is None:
                    errors += 1
                    print(
                        f"[steam] Review summary unavailable "
                        f"{appid} {name!r}"
                    )
                else:
                    save_steam_reviews(
                        db_path,
                        appid,
                        reviews,
                    )
                    reviews_refreshed += 1

            except Exception as error:
                errors += 1
                print(
                    f"[steam] Review error {appid} "
                    f"{name!r}: {error}"
                )

        else:
            try:
                details = get_store_details(
                    appid
                )

                if details is None:
                    save_steam_unavailable(
                        db_path,
                        appid,
                    )
                    unavailable += 1
                else:
                    save_steam_ready(
                        db_path,
                        details,
                    )
                    details_ready += 1

                    # New/changed games also get a review refresh, but a
                    # review failure must not discard valid Store details.
                    try:
                        reviews = get_review_summary(
                            appid
                        )

                        if reviews is not None:
                            save_steam_reviews(
                                db_path,
                                appid,
                                reviews,
                            )
                            reviews_refreshed += 1
                        else:
                            errors += 1
                            print(
                                f"[steam] Review summary unavailable "
                                f"{appid} {name!r}"
                            )

                    except Exception as review_error:
                        errors += 1
                        print(
                            f"[steam] Review error {appid} "
                            f"{name!r}: {review_error}"
                        )

            except Exception as error:
                # Temporary Store failures stay dirty and are retried later.
                errors += 1
                print(
                    f"[steam] Error {appid} {name!r}: {error}"
                )

        if index % 10 == 0 or index == len(games):
            print(
                f"[steam] {index}/{len(games)} | "
                f"{details_ready} details, "
                f"{reviews_refreshed} reviews, "
                f"{unavailable} unavailable, "
                f"{errors} errors"
            )

        if index < len(games):
            time.sleep(max(0.0, pause_seconds))


def update_hltb(
    db_path,
    *,
    limit,
    pause_seconds,
):
    games = hltb_candidates(
        db_path,
        limit,
    )

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

        try:
            # Reuse the exact v1.3.0 matcher without changing hltb_service.py.
            result = search_hltb_global(name)

            if result is None:
                save_hltb_no_match(
                    db_path,
                    appid,
                    name,
                )
                no_match += 1
            else:
                save_hltb_match(
                    db_path,
                    appid,
                    name,
                    result,
                )
                matched += 1

            consecutive_errors = 0

        except Exception as error:
            # Never persist a temporary/network error as no_match.
            errors += 1
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
            "Build/update the Steam Library Tracker "
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

    return parser.parse_args()


def main():
    args = parse_args()
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

    run_steam = args.steam_limit > 0
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
            )
            hltb_future = executor.submit(
                update_hltb,
                db_path,
                limit=args.hltb_limit,
                pause_seconds=args.hltb_pause,
            )

            # Propagate worker failures instead of silently publishing a
            # partially failed update.
            steam_future.result()
            hltb_future.result()

    elif run_steam:
        update_steam(
            db_path,
            limit=args.steam_limit,
            pause_seconds=args.steam_pause,
        )

    elif run_hltb:
        update_hltb(
            db_path,
            limit=args.hltb_limit,
            pause_seconds=args.hltb_pause,
        )

    print_stats(db_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
