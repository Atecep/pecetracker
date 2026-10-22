"""Gentle background population/refresh of HowLongToBeat metadata.

The worker never calls Streamlit APIs. It processes one game at a time in a
daemon thread, sleeps between requests, and reports progress in groups of ten.
"""

from __future__ import annotations

import threading
import sqlite3
from app_logging import get_logger
from sqlite_support import is_busy
import time

from constants import (
    HLTB_BACKGROUND_PAUSE_SECONDS,
    HLTB_BACKGROUND_PROGRESS_INTERVAL,
    HLTB_ERROR_BACKOFF_SECONDS,
    HLTB_REPEATED_ERROR_BACKOFF_SECONDS,
    STATUS_BACKLOG,
    STATUS_FINISHED,
    STATUS_PLAYING,
    STATUS_UNPLAYED,
)
from database import get_all_hltb_metadata
from hltb_service import get_hltb_metadata, is_hltb_cache_fresh


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


def _normalise_library_games(games):
    """Keep only fields required by the HLTB worker."""
    normalised = []
    seen = set()

    for game in games:
        appid = game.get("AppID", game.get("appid"))
        name = game.get("Game", game.get("name"))

        try:
            appid = int(appid)
        except (TypeError, ValueError):
            continue

        name = str(name or "").strip()

        if not name or appid in seen:
            continue

        seen.add(appid)
        normalised.append(
            {
                "appid": appid,
                "name": name,
                "status": game.get("Status", game.get("status")),
            }
        )

    # Populate the games the user is most likely to care about first.
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


def _candidate_games(games, deferred_appids=None):
    """Return games whose HLTB cache is missing, expired, or invalidated."""
    cached = get_all_hltb_metadata()
    deferred_appids = deferred_appids or set()
    candidates = []

    for game in games:
        if game["appid"] in deferred_appids:
            continue

        metadata = cached.get(game["appid"])

        if metadata is None or not is_hltb_cache_fresh(metadata):
            candidates.append(game)

    return candidates


def _wait(seconds):
    """Interruptible sleep for responsive shutdown."""
    return _stop_event.wait(seconds)


def _print_progress(batch_results, session_processed, pending):
    matched = sum(1 for item in batch_results if item["result"] == "matched")
    no_match = sum(1 for item in batch_results if item["result"] == "no_match")
    errors = sum(1 for item in batch_results if item["result"] == "error")

    elapsed_values = [
        item["elapsed"]
        for item in batch_results
        if item["elapsed"] is not None
    ]
    avg = (
        sum(elapsed_values) / len(elapsed_values)
        if elapsed_values
        else 0.0
    )

    _log.info(
        "[hltb] "
        f"{session_processed} processed: "
        f"{matched} matched, {no_match} no match, {errors} error"
        f"{'s' if errors != 1 else ''} in last {len(batch_results)} "
        f"| {pending} pending | avg {avg:.2f}s"
    )

    no_match_names = [
        item["name"]
        for item in batch_results
        if item["result"] == "no_match"
    ]
    error_names = [
        item["name"]
        for item in batch_results
        if item["result"] == "error"
    ]

    if no_match_names:
        _log.info("[hltb]   no match: " + " | ".join(no_match_names))

    if error_names:
        _log.info("[hltb]   errors: " + " | ".join(error_names))


def _run_worker():

    _log.info(
        "[hltb] Background population started "
        f"(1 game + {HLTB_BACKGROUND_PAUSE_SECONDS}s pause)."
    )

    session_processed = 0
    consecutive_errors = 0
    progress_batch = []
    error_attempts = {}
    deferred_appids = set()
    deferred_until = {}

    while not _stop_event.is_set():
        for appid, deadline in list(deferred_until.items()):
            if time.monotonic() >= deadline:
                deferred_appids.discard(appid)
                deferred_until.pop(appid)
                error_attempts.pop(appid, None)
        games = _current_library_games()

        if not games:
            break

        candidates = _candidate_games(
            games,
            deferred_appids=deferred_appids,
        )

        if not candidates:
            if progress_batch:
                _print_progress(
                    progress_batch,
                    session_processed,
                    0,
                )
                progress_batch = []

            if deferred_appids:
                _log.info(
                    "[hltb] "
                    f"{len(deferred_appids)} game(s) deferred after repeated "
                    "request errors; they will be retried after a cooldown."
                )
            else:
                _log.debug("HLTB cache is up to date")
            _wake_event.wait(IDLE_POLL_SECONDS)
            _wake_event.clear()
            continue

        # Work from the current snapshot. We rescan after each progress
        # group so cache changes caused by modal opens are picked up too.
        group = candidates[:HLTB_BACKGROUND_PROGRESS_INTERVAL]

        for game in group:
            if _stop_event.is_set():
                break

            appid = game["appid"]
            name = game["name"]
            started = time.perf_counter()

            try:
                metadata = get_hltb_metadata(
                    appid=appid,
                    game_name=name,
                )
                elapsed = time.perf_counter() - started

                if metadata and metadata.get("state") == "matched":
                    result = "matched"
                else:
                    result = "no_match"

                consecutive_errors = 0
                wait_seconds = HLTB_BACKGROUND_PAUSE_SECONDS

            except RuntimeError:
                # HLTB/network failures are deliberately not converted into
                # no-match cache entries by hltb_service.
                elapsed = time.perf_counter() - started
                result = "error"
                consecutive_errors += 1
                error_attempts[appid] = error_attempts.get(appid, 0) + 1

                # Give one later retry during this worker run. If the same
                # game fails twice, defer it for a future app run so one bad
                # title/service hiccup cannot keep the worker looping forever.
                if error_attempts[appid] >= 2:
                    deferred_appids.add(appid)
                    deferred_until[appid] = time.monotonic() + 600

                wait_seconds = (
                    HLTB_REPEATED_ERROR_BACKOFF_SECONDS
                    if consecutive_errors >= 2
                    else HLTB_ERROR_BACKOFF_SECONDS
                )

            except Exception as error:
                elapsed = time.perf_counter() - started
                result = "error"
                consecutive_errors += 1
                error_attempts[appid] = error_attempts.get(appid, 0) + 1

                if error_attempts[appid] >= 2:
                    deferred_appids.add(appid)
                    deferred_until[appid] = time.monotonic() + 600

                wait_seconds = (
                    HLTB_REPEATED_ERROR_BACKOFF_SECONDS
                    if consecutive_errors >= 2
                    else HLTB_ERROR_BACKOFF_SECONDS
                )
                _log.info(
                    "[hltb] Unexpected background error for "
                    f"{name}: {type(error).__name__}"
                )

            session_processed += 1
            progress_batch.append(
                {
                    "name": name,
                    "result": result,
                    "elapsed": elapsed,
                }
            )

            # Smooth cadence: every attempted game is followed by a pause.
            # Temporary errors back off more aggressively.
            if _wait(wait_seconds):
                break

        if _stop_event.is_set():
            break

        remaining = len(
            _candidate_games(
                _current_library_games(),
                deferred_appids=deferred_appids,
            )
        )

        if progress_batch:
            _print_progress(
                progress_batch,
                session_processed,
                remaining,
            )
            progress_batch = []

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
    _worker_thread = threading.Thread(target=_worker_loop, name="hltb-metadata-refresh", daemon=True)
    _worker_thread.start()


def start_hltb_background_refresh(games):
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


def stop_hltb_background_refresh():
    """Request stopped; a subsequent start is remembered until cleanup."""
    global _restart_requested
    with _state_lock:
        _restart_requested = False
        _stop_event.set()
        _wake_event.set()
