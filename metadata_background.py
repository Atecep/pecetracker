"""Gentle background population/refresh of Steam Store metadata.

The worker intentionally does not use Streamlit APIs. It runs as a daemon
thread in the Streamlit server process, updates SQLite one game at a time and
stops automatically when there is no due work left.
"""

import threading
import sqlite3
from app_logging import get_logger
from sqlite_support import is_busy

import requests
from steam_reviews import retry_delay

from constants import (
    METADATA_BACKGROUND_BATCH_SIZE,
    METADATA_BACKGROUND_PAUSE_SECONDS,
    METADATA_NETWORK_BACKOFF_SECONDS,
    METADATA_RATE_LIMIT_BACKOFF_SECONDS,
    STATUS_BACKLOG,
    STATUS_FINISHED,
    STATUS_PLAYING,
    STATUS_UNPLAYED,
)
from database import (
    get_all_game_metadata,
    get_all_metadata_fetch_statuses,
)
from metadata_service import (
    get_game_metadata,
    is_cache_fresh,
    is_metadata_retry_due,
)
from steam_store import SteamStoreMetadataUnavailable


_STATUS_PRIORITY = {
    STATUS_BACKLOG: 0,
    STATUS_UNPLAYED: 1,
    STATUS_PLAYING: 2,
    STATUS_FINISHED: 3,
}

_log = get_logger(__name__)
_restart_requested = False
_wake_event = threading.Event()
IDLE_POLL_SECONDS = 30
_state_lock = threading.Lock()
_worker_thread = None
_stop_event = threading.Event()
_library_games = []
_priority_appids = []


def _normalise_library_games(games):
    """Keep only the AppID/status fields needed by the worker."""
    normalised = []
    seen = set()

    for game in games:
        appid = game.get("AppID", game.get("appid"))

        if appid is None:
            continue

        try:
            appid = int(appid)
        except (TypeError, ValueError):
            continue

        if appid in seen:
            continue

        seen.add(appid)
        normalised.append({
            "appid": appid,
            "status": game.get("Status", game.get("status")),
        })

    normalised.sort(
        key=lambda item: (
            _STATUS_PRIORITY.get(item["status"], 99),
            item["appid"],
        )
    )

    return normalised


def _current_library_games():
    with _state_lock:
        return list(_library_games)


def _take_priority_batch(limit):
    """Pop a small batch of user-requested metadata rechecks."""
    global _priority_appids

    with _state_lock:
        batch = _priority_appids[:limit]
        _priority_appids = _priority_appids[limit:]

    return batch


def _queue_priority_appids(appids, valid_appids):
    """Queue manual rechecks once, keeping the user's library order-safe."""
    global _priority_appids

    requested = []
    seen = set()

    for appid in appids:
        try:
            appid = int(appid)
        except (TypeError, ValueError):
            continue

        if appid not in valid_appids or appid in seen:
            continue

        seen.add(appid)
        requested.append(appid)

    with _state_lock:
        already_queued = set(_priority_appids)
        new_appids = [
            appid
            for appid in requested
            if appid not in already_queued
        ]
        _priority_appids.extend(new_appids)

    return len(new_appids)


def _candidate_appids(games):
    """Return due missing metadata first, then due stale metadata."""
    cached = get_all_game_metadata()
    fetch_statuses = get_all_metadata_fetch_statuses()
    missing = []
    stale = []

    for game in games:
        appid = game["appid"]
        fetch_status = fetch_statuses.get(appid)

        if (
            fetch_status is not None
            and not is_metadata_retry_due(fetch_status)
        ):
            continue

        metadata = cached.get(appid)

        if metadata is None:
            missing.append(appid)
        elif fetch_status is not None or not is_cache_fresh(metadata):
            stale.append(appid)

    return missing + stale, len(missing), len(stale)


def _wait(seconds):
    """Interruptible sleep so shutdown/update is responsive."""
    return _stop_event.wait(seconds)


def _run_worker():

    _log.info(
        "[metadata] Background refresh started "
        f"(1 game every {METADATA_BACKGROUND_PAUSE_SECONDS}s)."
    )

    total_updated = 0
    total_deferred = 0

    while not _stop_event.is_set():
        games = _current_library_games()

        if not games:
            break

        priority_batch = _take_priority_batch(
            METADATA_BACKGROUND_BATCH_SIZE
        )

        if priority_batch:
            batch = [
                (appid, True)
                for appid in priority_batch
            ]
        else:
            candidates, _, _ = _candidate_appids(games)

            if not candidates:
                # Completion changes as retry deadlines and TTLs expire.
                _wake_event.wait(IDLE_POLL_SECONDS)
                _wake_event.clear()
                continue

            # Build a small queue to avoid rescanning the whole SQLite
            # cache after every single game. Requests are still strictly
            # sequential with a pause after each one; there is no burst.
            batch = [
                (appid, False)
                for appid in candidates[:METADATA_BACKGROUND_BATCH_SIZE]
            ]

        network_backoff = None

        for index, (appid, force_refresh) in enumerate(batch):
            if _stop_event.is_set():
                break

            try:
                metadata = get_game_metadata(
                    appid,
                    force_refresh=force_refresh,
                    allow_stale_on_error=False,
                )

                if metadata is None or not is_cache_fresh(metadata):
                    total_deferred += 1
                else:
                    total_updated += 1

            except SteamStoreMetadataUnavailable:
                # metadata_service has already persisted the retry state
                # for this AppID.
                total_deferred += 1

            except requests.HTTPError as error:
                total_deferred += 1
                status_code = getattr(
                    getattr(error, "response", None),
                    "status_code",
                    None,
                )

                if status_code == 429:
                    network_backoff = max(METADATA_RATE_LIMIT_BACKOFF_SECONDS, retry_delay(error))
                    _log.info(
                        "[metadata] Steam rate limit detected; "
                        "background refresh is backing off."
                    )
                    break

                if status_code is not None and status_code >= 500:
                    network_backoff = max(METADATA_NETWORK_BACKOFF_SECONDS, retry_delay(error))
                    break

            except requests.RequestException as error:
                total_deferred += 1
                network_backoff = max(METADATA_NETWORK_BACKOFF_SECONDS, retry_delay(error))
                _log.info(
                    "[metadata] Network problem; background refresh "
                    "is backing off."
                )
                break

            except Exception:
                _log.exception("Steam metadata request failed for %s", appid)
                total_deferred += 1

            processed = total_updated + total_deferred

            if processed % 10 == 0:
                _, remaining_missing, remaining_stale = _candidate_appids(
                    _current_library_games()
                )
                _log.info(
                    "[metadata] Progress: "
                    f"{total_updated} updated, {total_deferred} deferred this session; "
                    f"{remaining_missing} missing + {remaining_stale} stale pending."
                )

            # Smooth request rate: one game, then pause, including between
            # items in the internal queue.
            if index < len(batch) - 1:
                if _wait(METADATA_BACKGROUND_PAUSE_SECONDS):
                    break

        if _stop_event.is_set():
            break

        if network_backoff is not None:
            if _wait(network_backoff):
                break
        else:
            # Also pause between the last game in this queue and the first
            # game in the next queue so the request cadence stays uniform.
            if _wait(METADATA_BACKGROUND_PAUSE_SECONDS):
                break

def _worker_loop():
    global _worker_thread, _restart_requested
    try:
        while not _stop_event.is_set():
            try:
                _run_worker()
                break
            except sqlite3.Error as error:
                _log.exception("Background database scan failed")
                if not is_busy(error) or _wait(5):
                    break
            except OSError:
                _log.exception("Background filesystem operation failed")
                if _wait(30):
                    break
            except Exception:
                # Programming/schema failures remain visible in the persistent
                # log, rather than spinning forever behind a broad retry.
                _log.exception("Background worker failed")
                break
    finally:
        with _state_lock:
            _worker_thread = None
            if _restart_requested:
                _restart_requested = False
                _launch_worker_locked()


def _launch_worker_locked():
    global _worker_thread
    _stop_event.clear()
    _worker_thread = threading.Thread(target=_worker_loop, name="steam-metadata-refresh", daemon=True)
    _worker_thread.start()


def start_metadata_background_refresh(games):
    """Update the snapshot and request running, including during shutdown."""
    global _library_games, _restart_requested
    normalised = _normalise_library_games(games)
    with _state_lock:
        _library_games = normalised
        _wake_event.set()
        if _worker_thread is not None and _worker_thread.is_alive():
            if _stop_event.is_set():
                _restart_requested = True
            return False
        _restart_requested = False
        _launch_worker_locked()
    return True


def request_metadata_recheck(games, appids):
    """Prioritise a manual recheck of selected games.

    The check still uses the normal gentle request cadence and respects any
    active network/rate-limit backoff. Retry windows for these AppIDs are
    bypassed only for this user-requested pass.
    """
    global _library_games

    normalised = _normalise_library_games(games)
    valid_appids = {item["appid"] for item in normalised}

    with _state_lock:
        _library_games = normalised

    queued = _queue_priority_appids(appids, valid_appids)

    start_metadata_background_refresh(normalised)

    return queued


def stop_metadata_background_refresh():
    """Request stopped; a subsequent start is remembered until cleanup."""
    global _restart_requested
    with _state_lock:
        _restart_requested = False
        _stop_event.set()
        _wake_event.set()
