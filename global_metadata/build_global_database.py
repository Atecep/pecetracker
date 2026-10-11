from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
from pathlib import Path
import re
import sys
import threading
import time

import requests


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
from hltb_transport import (  # noqa: E402
    HLTB_HTTP_THROTTLE,
    HLTBFailure,
    HLTBSuspended,
    HLTBTransport,
    ServiceProtection,
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
DEFAULT_HLTB_LIMIT = 100

DEFAULT_STEAM_PAUSE = 2.0
DEFAULT_HLTB_PAUSE = 1.0

# HLTB request throttle.
#
# The matcher can issue multiple search requests for a single Steam title
# (normalised title, punctuation variant, edition fallback, structural
# fallback, etc.). The shared transport throttle gates every HTTP dispatch.
# This lock only protects the scoped howlongtobeatpy instrumentation.
_hltb_request_lock = threading.Lock()
HLTB_SERVICE_RETRY_STATE = "hltb_service_retry_after"


def configure_hltb_request_throttle(interval_seconds):
    HLTB_HTTP_THROTTLE.configure(interval_seconds)


def _run_hltb_request(query, *, transport, matching_phase, attempt):
    with _hltb_request_lock:
        return transport.search(query, matching_phase, attempt)


def _throttled_hltb_search_once(query, *, transport, protection, matching_phase):
    for attempt in (1, 2):
        try:
            results = _run_hltb_request(
                query, transport=transport, matching_phase=matching_phase, attempt=attempt,
            )
        except HLTBFailure as error:
            delay = protection.failure(error)
            if attempt == 2:
                raise
            print(f"[hltb] Retry {query!r} after {delay:.1f}s: {error}")
            time.sleep(delay)
        else:
            protection.success()
            return results


def search_hltb_global(game_name, *, transport=None, protection=None):
    """Run the app's exact matcher with global-builder request throttling."""
    original_search_once = hltb_service._search_once
    original_search_queries = hltb_service._search_queries
    transport = transport if transport is not None else HLTBTransport()
    protection = protection if protection is not None else ServiceProtection()
    phases = {}
    primary = True

    def search_queries(queries):
        nonlocal primary
        if primary:
            clean = hltb_service._strip_query_noise(game_name)
            clean = hltb_service._YEAR_SUFFIX_RE.sub("", clean).strip()
            edition = hltb_service._strip_safe_edition_suffix(clean)
            edition_queries = {edition, re.sub(r"[^\w]+", " ", edition).strip()}
            edition_queries = {re.sub(r"\s+", " ", query) for query in edition_queries}
            for query in queries:
                phases[query] = (
                    "original" if query == game_name else
                    "edition_fallback" if edition != clean and query in edition_queries else
                    "title_variant"
                )
            primary = False
        else:
            phases.update({query: "structural_fallback" for query in queries})
        return original_search_queries(queries)

    hltb_service._search_queries = search_queries
    hltb_service._search_once = lambda query: _throttled_hltb_search_once(
        query, transport=transport, protection=protection, matching_phase=phases[query],
    )

    try:
        return hltb_service._search_hltb(game_name)
    finally:
        hltb_service._search_once = original_search_once
        hltb_service._search_queries = original_search_queries


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
    retry_after = float(get_state(db_path, HLTB_SERVICE_RETRY_STATE, "0"))
    if math.isfinite(retry_after) and retry_after > time.time():
        print(f"[hltb] Global pause active; retry in {retry_after - time.time():.0f}s.")
        return
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
        f"{max(0.0, pause_seconds):.1f}s minimum between HTTP requests (including redirects); "
        "one retry with progressive backoff on temporary failures."
    )

    matched = 0
    no_match = 0
    errors = 0
    consecutive_errors = 0
    transport = HLTBTransport()
    protection = ServiceProtection()

    for index, game in enumerate(games, start=1):
        appid = int(game["appid"])
        name = str(game["name"] or "").strip()

        try:
            # Reuse the exact v1.3.0 matcher without changing hltb_service.py.
            result = search_hltb_global(name, transport=transport, protection=protection)

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

        except HLTBSuspended as error:
            errors += 1
            set_state(db_path, HLTB_SERVICE_RETRY_STATE, time.time() + error.delay)
            print(f"[hltb] {error}. Current and remaining games stay eligible.")
            break

        except HLTBFailure as error:
            errors += 1
            if error.global_candidate:
                # Leave unresolved games eligible; a service failure is not an
                # AppID failure. The next failed attempt can open the circuit.
                print(f"[hltb] Deferred {appid} {name!r}: {error}; no individual cooldown.")
                time.sleep(protection.delay)
            else:
                consecutive_errors += 1
                cooldown = save_hltb_error(db_path, appid, error)
                print(
                    f"[hltb] Error {appid} {name!r}: {error} | "
                    f"retry #{cooldown['error_count']} in {cooldown['cooldown_hours']}h"
                )
                time.sleep(
                    HLTB_REPEATED_ERROR_BACKOFF_SECONDS
                    if consecutive_errors > 1 else HLTB_ERROR_BACKOFF_SECONDS
                )

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

        # Every HTTP dispatch is throttled by the shared transport adapter.

    print(
        f"[hltb] Summary: {matched} matched, {no_match} no match, {errors} errors; "
        f"{transport.attempts} search attempts, {transport.requests} HTTP requests."
    )
    transport.log_summary()


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
            "Minimum seconds between HLTB HTTP requests, including redirects."
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
