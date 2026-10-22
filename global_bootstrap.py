from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
from pathlib import Path
import shutil
import tempfile
import threading
import time
import sqlite3
from app_logging import get_logger
from sqlite_support import is_busy
from urllib.request import Request, urlopen

from database import (
    get_app_state,
    import_global_metadata,
    set_app_state,
)


_log = get_logger(__name__)
_worker_lock = threading.Lock()
_worker_thread = None
_next_check = 0.0
_last_result = None

GLOBAL_BOOTSTRAP_VERSION = 1
GLOBAL_BOOTSTRAP_STATE_KEY = (
    f"global_metadata_bootstrap_v{GLOBAL_BOOTSTRAP_VERSION}"
)
GLOBAL_BOOTSTRAP_LAST_ATTEMPT_KEY = (
    f"{GLOBAL_BOOTSTRAP_STATE_KEY}_last_attempt"
)
GLOBAL_BOOTSTRAP_RETRY_HOURS = 6

GLOBAL_RELEASE_BASE_URL = (
    "https://github.com/Atecep/steam-library-tracker/"
    "releases/download/global-db"
)

GLOBAL_DB_ZST_NAME = "global_metadata.db.zst"
GLOBAL_DB_ZST_SHA256_NAME = "global_metadata.db.zst.sha256"

GLOBAL_DB_ZST_URL = (
    f"{GLOBAL_RELEASE_BASE_URL}/{GLOBAL_DB_ZST_NAME}"
)
GLOBAL_DB_ZST_SHA256_URL = (
    f"{GLOBAL_RELEASE_BASE_URL}/{GLOBAL_DB_ZST_SHA256_NAME}"
)


def _now():
    return datetime.now()


def _now_iso():
    return _now().isoformat(
        timespec="seconds"
    )


def global_bootstrap_completed():
    return (
        get_app_state(
            GLOBAL_BOOTSTRAP_STATE_KEY
        )
        == "completed"
    )


def should_attempt_global_bootstrap():
    if global_bootstrap_completed():
        return False

    last_attempt = get_app_state(
        GLOBAL_BOOTSTRAP_LAST_ATTEMPT_KEY
    )

    if not last_attempt:
        return True

    try:
        last_attempt_time = datetime.fromisoformat(
            last_attempt
        )
    except ValueError:
        return True

    retry_after = (
        last_attempt_time
        + timedelta(
            hours=GLOBAL_BOOTSTRAP_RETRY_HOURS
        )
    )

    return _now() >= retry_after


def _download(url, destination):
    destination = Path(destination)

    request = Request(
        url,
        headers={
            "User-Agent": "SteamLibraryTracker/global-metadata-bootstrap",
            "Accept": "application/octet-stream",
        },
    )

    with urlopen(
        request,
        timeout=120,
    ) as response:
        with destination.open("wb") as output:
            shutil.copyfileobj(
                response,
                output,
                length=1024 * 1024,
            )


def _read_expected_sha256(checksum_path):
    checksum_text = Path(
        checksum_path
    ).read_text(
        encoding="utf-8"
    ).strip()

    if not checksum_text:
        raise RuntimeError(
            "The global metadata checksum is empty."
        )

    expected = checksum_text.split()[0].strip()

    if (
        len(expected) != 64
        or any(
            character not in "0123456789abcdefABCDEF"
            for character in expected
        )
    ):
        raise RuntimeError(
            "The global metadata checksum is invalid."
        )

    return expected.lower()


def _sha256(path):
    digest = hashlib.sha256()

    with Path(path).open("rb") as source:
        while True:
            chunk = source.read(
                1024 * 1024
            )

            if not chunk:
                break

            digest.update(chunk)

    return digest.hexdigest()


def _verify_download(compressed_path, checksum_path):
    expected = _read_expected_sha256(
        checksum_path
    )
    actual = _sha256(
        compressed_path
    )

    if actual != expected:
        raise RuntimeError(
            "Global metadata checksum verification failed."
        )


def _decompress_zstandard(source_path, destination_path):
    try:
        import zstandard as zstd
    except ImportError as error:
        raise RuntimeError(
            "The zstandard package is required for "
            "the global metadata bootstrap."
        ) from error

    decompressor = zstd.ZstdDecompressor()

    with Path(source_path).open("rb") as source:
        with Path(destination_path).open("wb") as destination:
            decompressor.copy_stream(
                source,
                destination,
            )


def bootstrap_global_metadata(library_records):
    """Run the one-time global metadata bootstrap.

    Failure is non-fatal. The existing local Steam and HLTB workers remain
    the fallback, and another bootstrap attempt is allowed after the cooldown.
    """

    try:
        if global_bootstrap_completed():
            return {
                "attempted": False,
                "completed": True,
                "steam_imported": 0,
                "hltb_imported": 0,
                "error": None,
            }

        if not should_attempt_global_bootstrap():
            return {
                "attempted": False,
                "completed": False,
                "steam_imported": 0,
                "hltb_imported": 0,
                "error": None,
            }

        set_app_state(
            GLOBAL_BOOTSTRAP_LAST_ATTEMPT_KEY,
            _now_iso(),
        )

        with tempfile.TemporaryDirectory(
            prefix="slt-global-metadata-"
        ) as temp_dir:
            temp_dir = Path(temp_dir)

            compressed_path = (
                temp_dir
                / GLOBAL_DB_ZST_NAME
            )
            checksum_path = (
                temp_dir
                / GLOBAL_DB_ZST_SHA256_NAME
            )
            database_path = (
                temp_dir
                / "global_metadata.db"
            )

            _download(
                GLOBAL_DB_ZST_URL,
                compressed_path,
            )
            _download(
                GLOBAL_DB_ZST_SHA256_URL,
                checksum_path,
            )

            _verify_download(
                compressed_path,
                checksum_path,
            )

            _decompress_zstandard(
                compressed_path,
                database_path,
            )

            imported = import_global_metadata(
                database_path,
                library_records,
            )

        set_app_state(
            GLOBAL_BOOTSTRAP_STATE_KEY,
            "completed",
        )

        return {
            "attempted": True,
            "completed": True,
            "steam_imported": int(
                imported.get(
                    "steam_imported",
                    0,
                )
            ),
            "hltb_imported": int(
                imported.get(
                    "hltb_imported",
                    0,
                )
            ),
            "error": None,
        }

    except Exception as error:
        _log.exception("Global metadata bootstrap failed")
        if is_busy(error):
            try:
                set_app_state(GLOBAL_BOOTSTRAP_LAST_ATTEMPT_KEY, "")
            except sqlite3.Error:
                _log.warning("Could not clear bootstrap attempt after contention")

        return {
            "attempted": True,
            "completed": False,
            "steam_imported": 0,
            "hltb_imported": 0,
            "error": str(error),
            "retry_soon": is_busy(error),
        }


def start_global_bootstrap(library_records):
    """Run bootstrap off the Streamlit thread; callers only inspect snapshots."""
    global _worker_thread, _next_check, _last_result
    with _worker_lock:
        if _worker_thread is not None or time.monotonic() < _next_check:
            return
        snapshot = [{"AppID": game["AppID"]} for game in library_records]
        def run():
            global _worker_thread, _next_check, _last_result
            try:
                result = bootstrap_global_metadata(snapshot)
            except Exception:
                _log.exception("Unexpected bootstrap worker failure")
                result = {"completed": False, "attempted": True, "error": "Bootstrap failed", "retry_soon": True}
            finally:
                with _worker_lock:
                    _last_result = result
                    _next_check = time.monotonic() + (30 if result.get("retry_soon") else GLOBAL_BOOTSTRAP_RETRY_HOURS * 3600)
                    _worker_thread = None
        _worker_thread = threading.Thread(target=run, name="global-bootstrap", daemon=True)
        _worker_thread.start()


def get_bootstrap_result():
    with _worker_lock:
        return dict(_last_result) if _last_result is not None else None
