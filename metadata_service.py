from datetime import datetime, timedelta
import threading

import requests
from cache_time import is_fresh, parse_timestamp
from datetime import timezone

from constants import (
    METADATA_CACHE_MAX_AGE_DAYS,
    METADATA_UNAVAILABLE_FIRST_RETRY_SECONDS,
    METADATA_UNAVAILABLE_RECHECK_SECONDS,
    METADATA_UNAVAILABLE_SECOND_RETRY_SECONDS,
    METADATA_FAILED_RETRY_SECONDS,
    METADATA_STATE_DEFERRED,
    METADATA_STATE_METADATA_UNAVAILABLE,
)
from database import (
    clear_metadata_fetch_status,
    get_game_metadata as get_cached_metadata,
    get_metadata_fetch_status,
    save_game_metadata,
    save_metadata_fetch_status,
)
from steam_store import (
    SteamStoreMetadataUnavailable,
    get_game_metadata as fetch_steam_metadata,
)
from steam_reviews import ReviewResponseError, retry_delay


_fetch_locks_guard = threading.Lock()
_fetch_locks = {}


def _get_fetch_lock(appid):
    """Return a per-game lock so duplicate Steam fetches do not overlap."""
    with _fetch_locks_guard:
        return _fetch_locks.setdefault(int(appid), threading.Lock())


def is_cache_fresh(metadata):
    return is_fresh(metadata.get("fetched_at"), METADATA_CACHE_MAX_AGE_DAYS) and is_fresh(
        metadata.get("reviews_fetched_at"), METADATA_CACHE_MAX_AGE_DAYS
    )


def is_metadata_retry_due(fetch_status):
    """Return True when a persistent deferred entry may be tried again."""
    if not fetch_status:
        return True

    next_retry_at = fetch_status.get("next_retry_at")

    if not next_retry_at:
        return True

    try:
        retry_time = parse_timestamp(next_retry_at)
    except (TypeError, ValueError):
        return True

    return datetime.now(timezone.utc) >= retry_time


def _retry_time_after(seconds):
    return (
        datetime.now() + timedelta(seconds=seconds)
    ).isoformat(timespec="seconds")


def _record_store_unavailable(appid):
    """Record a valid Steam Store success=false response.

    The game is marked Metadata unavailable immediately. Repeated valid
    Store responses only change how often it is retried. Network errors,
    HTTP 429 responses and timeouts never create this visible state.
    """
    current = get_metadata_fetch_status(appid) or {}
    failure_count = int(current.get("failure_count") or 0) + 1

    state = METADATA_STATE_METADATA_UNAVAILABLE

    if failure_count >= 3:
        retry_seconds = METADATA_UNAVAILABLE_RECHECK_SECONDS
    elif failure_count == 2:
        retry_seconds = METADATA_UNAVAILABLE_SECOND_RETRY_SECONDS
    else:
        retry_seconds = METADATA_UNAVAILABLE_FIRST_RETRY_SECONDS

    save_metadata_fetch_status(
        appid=appid,
        state=state,
        failure_count=failure_count,
        next_retry_at=_retry_time_after(retry_seconds),
        last_error="store_unavailable",
    )

    return state


def _record_temporary_failure(appid, error_type, retry_seconds=METADATA_FAILED_RETRY_SECONDS):
    """Persist a temporary retry without marking Store metadata unavailable."""
    current = get_metadata_fetch_status(appid) or {}
    current_state = current.get("state")

    # Once Store metadata is known to be unavailable, an unrelated network
    # failure during a periodic recheck must not hide that visible state.
    state = (
        METADATA_STATE_METADATA_UNAVAILABLE
        if current_state == METADATA_STATE_METADATA_UNAVAILABLE
        else METADATA_STATE_DEFERRED
    )

    save_metadata_fetch_status(
        appid=appid,
        state=state,
        failure_count=int(current.get("failure_count") or 0),
        next_retry_at=_retry_time_after(max(METADATA_FAILED_RETRY_SECONDS, retry_seconds)),
        last_error=error_type,
    )


def get_game_metadata(
    appid,
    force_refresh=False,
    allow_stale_on_error=True,
    allow_fetch=True,
):
    appid = int(appid)

    cached = get_cached_metadata(appid)
    if not allow_fetch:
        return cached
    fetch_status = get_metadata_fetch_status(appid)

    # Respect persistent retry windows. This prevents a restart or opening a
    # modal from immediately hammering the same failing AppID again.
    if (
        not force_refresh
        and fetch_status is not None
        and not is_metadata_retry_due(fetch_status)
    ):
        return cached

    # Cache is still valid.
    if (
        cached is not None
        and not force_refresh
        and fetch_status is None
        and is_cache_fresh(cached)
    ):
        return cached

    # A modal and the background worker can ask for the same game at nearly
    # the same time. Serialise only that AppID, then re-check both cache and
    # retry state because another caller may already have handled it.
    with _get_fetch_lock(appid):
        cached = get_cached_metadata(appid)
        fetch_status = get_metadata_fetch_status(appid)

        if (
            not force_refresh
            and fetch_status is not None
            and not is_metadata_retry_due(fetch_status)
        ):
            return cached

        if (
            cached is not None
            and not force_refresh
            and fetch_status is None
            and is_cache_fresh(cached)
        ):
            return cached

        try:
            metadata = fetch_steam_metadata(appid)

            if metadata is not None:
                save_game_metadata(metadata)
                if metadata.get("reviews") is None:
                    _record_temporary_failure(appid, "reviews_unavailable")
                else:
                    clear_metadata_fetch_status(appid)
                return get_cached_metadata(appid)

            # Defensive fallback: current Steam Store code raises a specific
            # exception for success=false, so a plain None is only an unknown
            # temporary failure and is not Store-unavailable evidence.
            _record_temporary_failure(appid, "empty_response")

        except SteamStoreMetadataUnavailable:
            _record_store_unavailable(appid)

            if allow_stale_on_error:
                return cached

            raise

        except requests.HTTPError as error:
            status_code = getattr(
                getattr(error, "response", None),
                "status_code",
                None,
            )
            error_type = (
                "rate_limit"
                if status_code == 429
                else "http_error"
            )
            _record_temporary_failure(appid, error_type, retry_delay(error))

            if cached is not None and allow_stale_on_error:
                return cached

            raise

        except requests.RequestException as error:
            _record_temporary_failure(
                appid, "reviews_unavailable" if isinstance(error, ReviewResponseError) else "network_error",
                retry_delay(error),
            )

            if cached is not None and allow_stale_on_error:
                return cached

            raise

        except Exception:
            _record_temporary_failure(appid, "unexpected_error")

            if cached is not None and allow_stale_on_error:
                return cached

            raise

        if cached is not None:
            return cached

        return None
