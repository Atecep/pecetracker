from __future__ import annotations

import json
import sqlite3
from sqlite_support import ClosingConnection
from datetime import datetime, timedelta, timezone
from pathlib import Path

from constants import (
    HLTB_CACHE_MAX_AGE_DAYS,
    HLTB_MATCHER_VERSION,
)


SCHEMA_VERSION = 4
STEAM_UNAVAILABLE_RETRY_DAYS = 30

# Global-only Steam refresh policy.
#
# Store details are primarily refreshed when the official catalogue reports
# that the app changed. A long safety refresh catches anything the catalogue
# signal may miss. Reviews use their own shorter TTL and can be refreshed
# without calling /api/appdetails again.
GLOBAL_STEAM_DETAILS_MAX_AGE_DAYS = 365
GLOBAL_STEAM_REVIEWS_MAX_AGE_DAYS = 90

# Global-only HLTB no-match refresh policy. The desktop app retains its
# separate 30-day HLTB_NO_MATCH_CACHE_DAYS setting from constants.py.
GLOBAL_HLTB_NO_MATCH_CACHE_DAYS = 90


def _now():
    return datetime.now(timezone.utc)


def _now_iso():
    return _now().isoformat(timespec="seconds")


def _cutoff_iso(days):
    return (
        _now() - timedelta(days=int(days))
    ).isoformat(timespec="seconds")


def connect(db_path):
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path, factory=ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def _ensure_column(conn, table_name, column_name, definition):
    columns = {
        row["name"]
        for row in conn.execute(
            f"PRAGMA table_info({table_name})"
        ).fetchall()
    }

    if column_name not in columns:
        conn.execute(
            f"ALTER TABLE {table_name} "
            f"ADD COLUMN {column_name} {definition}"
        )


def init_database(db_path):
    with connect(db_path) as conn:
        # WAL allows the Steam and HLTB workers to use separate SQLite
        # connections concurrently while keeping write transactions short.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")

        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_info (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS steam_catalog (
                appid INTEGER PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                steam_last_modified INTEGER,
                price_change_number INTEGER,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                steam_dirty INTEGER NOT NULL DEFAULT 1,
                hltb_dirty INTEGER NOT NULL DEFAULT 1,

                hltb_error_count INTEGER NOT NULL DEFAULT 0,
                hltb_retry_after TEXT,
                hltb_last_error TEXT,
                hltb_last_error_at TEXT
            );

            CREATE TABLE IF NOT EXISTS steam_metadata (
                appid INTEGER PRIMARY KEY,
                state TEXT NOT NULL,

                genres TEXT,
                categories TEXT,
                developers TEXT,
                publishers TEXT,
                release_date TEXT,
                is_free INTEGER,

                review_score INTEGER,
                review_description TEXT,
                positive_percentage REAL,
                total_positive INTEGER,
                total_negative INTEGER,
                total_reviews INTEGER,

                fetched_at TEXT NOT NULL,
                reviews_fetched_at TEXT,

                FOREIGN KEY(appid)
                    REFERENCES steam_catalog(appid)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS hltb_metadata (
                appid INTEGER PRIMARY KEY,
                searched_name TEXT NOT NULL,
                state TEXT NOT NULL,

                hltb_id INTEGER,
                matched_name TEXT,
                similarity REAL,
                match_confidence TEXT,
                matcher_version INTEGER,

                main_story REAL,
                main_extra REAL,
                completionist REAL,
                all_styles REAL,
                web_link TEXT,

                fetched_at TEXT NOT NULL,

                FOREIGN KEY(appid)
                    REFERENCES steam_catalog(appid)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS updater_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS steam_fetch_retries (
                appid INTEGER NOT NULL REFERENCES steam_catalog(appid) ON DELETE CASCADE,
                service TEXT NOT NULL CHECK(service IN ('details', 'reviews')),
                retry_after REAL NOT NULL,
                last_error TEXT NOT NULL,
                PRIMARY KEY(appid, service)
            );

            CREATE INDEX IF NOT EXISTS idx_catalog_steam_dirty
                ON steam_catalog(steam_dirty, appid);

            CREATE INDEX IF NOT EXISTS idx_catalog_hltb_dirty
                ON steam_catalog(hltb_dirty, appid);

            CREATE INDEX IF NOT EXISTS idx_steam_fetched
                ON steam_metadata(fetched_at);

            CREATE INDEX IF NOT EXISTS idx_steam_reviews_due
                ON steam_metadata(state, COALESCE(reviews_fetched_at, ''), appid);

            CREATE INDEX IF NOT EXISTS idx_hltb_fetched
                ON hltb_metadata(fetched_at);

            DROP VIEW IF EXISTS game_metadata_export;

            CREATE VIEW game_metadata_export AS
            SELECT
                c.appid,
                c.name AS steam_name,

                s.state AS steam_state,
                s.genres,
                s.categories,
                s.developers,
                s.publishers,
                s.release_date,
                s.is_free,
                s.review_score,
                s.review_description,
                s.positive_percentage,
                s.total_positive,
                s.total_negative,
                s.total_reviews,
                s.fetched_at AS steam_fetched_at,

                h.state AS hltb_state,
                h.hltb_id,
                h.matched_name AS hltb_name,
                h.similarity AS hltb_similarity,
                h.match_confidence AS hltb_match_confidence,
                h.matcher_version AS hltb_matcher_version,
                h.main_story,
                h.main_extra,
                h.completionist,
                h.all_styles,
                h.web_link AS hltb_web_link,
                h.fetched_at AS hltb_fetched_at

            FROM steam_catalog AS c
            LEFT JOIN steam_metadata AS s
                ON s.appid = c.appid
            LEFT JOIN hltb_metadata AS h
                ON h.appid = c.appid;
            """
        )

        # Schema v2: persistent HLTB error cooldown. These ALTERs are
        # intentionally migration-safe so an existing v1 global DB can be
        # upgraded in place without deleting any metadata.
        _ensure_column(
            conn,
            "steam_catalog",
            "hltb_error_count",
            "INTEGER NOT NULL DEFAULT 0",
        )
        _ensure_column(
            conn,
            "steam_catalog",
            "hltb_retry_after",
            "TEXT",
        )
        _ensure_column(
            conn,
            "steam_catalog",
            "hltb_last_error",
            "TEXT",
        )
        _ensure_column(
            conn,
            "steam_catalog",
            "hltb_last_error_at",
            "TEXT",
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_catalog_hltb_retry_after
            ON steam_catalog(hltb_retry_after)
            """
        )

        # Schema v3: Steam review refresh is tracked independently from
        # Steam Store details. Existing rows inherit fetched_at only when
        # review data had actually been stored.
        _ensure_column(
            conn,
            "steam_metadata",
            "reviews_fetched_at",
            "TEXT",
        )

        conn.execute(
            """
            UPDATE steam_metadata
            SET reviews_fetched_at = fetched_at
            WHERE
                reviews_fetched_at IS NULL
                AND (
                    review_score IS NOT NULL
                    OR review_description IS NOT NULL
                    OR total_positive IS NOT NULL
                    OR total_negative IS NOT NULL
                    OR total_reviews IS NOT NULL
                )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_steam_reviews_fetched
            ON steam_metadata(reviews_fetched_at)
            """
        )

        conn.execute(
            """
            INSERT INTO schema_info(key, value)
            VALUES ('schema_version', ?)
            ON CONFLICT(key)
            DO UPDATE SET value = excluded.value
            """,
            (str(SCHEMA_VERSION),),
        )


def get_state(db_path, key, default=None):
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT value FROM updater_state WHERE key = ?",
            (key,),
        ).fetchone()

    return default if row is None else row["value"]


def set_state(db_path, key, value):
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO updater_state(key, value)
            VALUES (?, ?)
            ON CONFLICT(key)
            DO UPDATE SET value = excluded.value
            """,
            (key, str(value)),
        )


def upsert_catalog(db_path, apps):
    now = _now_iso()
    rows = []

    for app in apps:
        appid = int(app["appid"])
        name = str(app.get("name") or "").strip()

        last_modified = app.get("last_modified")
        price_change_number = app.get("price_change_number")

        rows.append(
            (
                appid,
                name,
                int(last_modified) if last_modified is not None else None,
                (
                    int(price_change_number)
                    if price_change_number is not None
                    else None
                ),
                now,
                now,
            )
        )

    if not rows:
        return 0

    with connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO steam_catalog (
                appid,
                name,
                steam_last_modified,
                price_change_number,
                first_seen_at,
                last_seen_at,
                steam_dirty,
                hltb_dirty
            )
            VALUES (?, ?, ?, ?, ?, ?, 1, 1)

            ON CONFLICT(appid)
            DO UPDATE SET
                name = excluded.name,
                steam_last_modified = excluded.steam_last_modified,
                price_change_number = excluded.price_change_number,
                last_seen_at = excluded.last_seen_at,

                steam_dirty = CASE
                    WHEN steam_catalog.steam_last_modified
                         IS NOT excluded.steam_last_modified
                    THEN 1
                    ELSE steam_catalog.steam_dirty
                END,

                hltb_dirty = CASE
                    WHEN steam_catalog.name <> excluded.name
                    THEN 1
                    ELSE steam_catalog.hltb_dirty
                END,

                hltb_error_count = CASE
                    WHEN steam_catalog.name <> excluded.name
                    THEN 0
                    ELSE steam_catalog.hltb_error_count
                END,

                hltb_retry_after = CASE
                    WHEN steam_catalog.name <> excluded.name
                    THEN NULL
                    ELSE steam_catalog.hltb_retry_after
                END,

                hltb_last_error = CASE
                    WHEN steam_catalog.name <> excluded.name
                    THEN NULL
                    ELSE steam_catalog.hltb_last_error
                END,

                hltb_last_error_at = CASE
                    WHEN steam_catalog.name <> excluded.name
                    THEN NULL
                    ELSE steam_catalog.hltb_last_error_at
                END
            """,
            rows,
        )

    return len(rows)


def steam_candidates(db_path, limit):
    details_cutoff = _cutoff_iso(
        GLOBAL_STEAM_DETAILS_MAX_AGE_DAYS
    )
    reviews_cutoff = _cutoff_iso(
        GLOBAL_STEAM_REVIEWS_MAX_AGE_DAYS
    )
    unavailable_cutoff = _cutoff_iso(
        STEAM_UNAVAILABLE_RETRY_DAYS
    )

    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT
                c.appid,
                c.name,
                CASE
                    WHEN
                        c.steam_dirty = 1
                        OR s.appid IS NULL
                        OR (
                            s.state = 'unavailable'
                            AND s.fetched_at < ?
                        )
                        OR (
                            s.state = 'ready'
                            AND s.fetched_at < ?
                        )
                    THEN 'full'
                    ELSE 'reviews'
                END AS refresh_kind
            FROM steam_catalog AS c
            LEFT JOIN steam_metadata AS s
                ON s.appid = c.appid
            WHERE
                c.steam_dirty = 1
                OR s.appid IS NULL
                OR (
                    s.state = 'unavailable'
                    AND s.fetched_at < ?
                )
                OR (
                    s.state = 'ready'
                    AND s.fetched_at < ?
                )
                OR (
                    s.state = 'ready'
                    AND (
                        s.reviews_fetched_at IS NULL
                        OR s.reviews_fetched_at < ?
                    )
                )
            ORDER BY
                CASE
                    WHEN
                        c.steam_dirty = 1
                        OR s.appid IS NULL
                        OR (
                            s.state = 'unavailable'
                            AND s.fetched_at < ?
                        )
                        OR (
                            s.state = 'ready'
                            AND s.fetched_at < ?
                        )
                    THEN 0
                    ELSE 1
                END,
                c.steam_dirty DESC,
                c.appid ASC
            LIMIT ?
            """,
            (
                unavailable_cutoff,
                details_cutoff,
                unavailable_cutoff,
                details_cutoff,
                reviews_cutoff,
                unavailable_cutoff,
                details_cutoff,
                int(limit),
            ),
        ).fetchall()

    return [dict(row) for row in rows]


def steam_details_candidates(db_path, limit):
    """Details have their own budget; review-only work cannot consume it."""
    with connect(db_path) as conn:
        rows = conn.execute("""
            SELECT c.appid, c.name FROM steam_catalog c
            LEFT JOIN steam_metadata s ON s.appid = c.appid
            WHERE (c.steam_dirty = 1 OR s.appid IS NULL
                OR (s.state = 'unavailable' AND s.fetched_at < ?)
                OR (s.state = 'ready' AND s.fetched_at < ?))
              AND NOT EXISTS (SELECT 1 FROM steam_fetch_retries r
                WHERE r.appid = c.appid AND r.service = 'details' AND r.retry_after > ?)
            ORDER BY c.steam_dirty DESC, COALESCE(s.fetched_at, ''), c.appid
            LIMIT ?
        """, (_cutoff_iso(STEAM_UNAVAILABLE_RETRY_DAYS),
              _cutoff_iso(GLOBAL_STEAM_DETAILS_MAX_AGE_DAYS), _now().timestamp(), int(limit))).fetchall()
    return [dict(row) for row in rows]


def steam_review_candidates(db_path, limit):
    """Oldest/missing summaries first, independently of catalogue dirtiness."""
    with connect(db_path) as conn:
        rows = conn.execute("""
            SELECT c.appid, c.name FROM steam_catalog c
            JOIN steam_metadata s ON s.appid = c.appid
            WHERE s.state = 'ready'
              AND COALESCE(s.reviews_fetched_at, '') < ?
              AND NOT EXISTS (SELECT 1 FROM steam_fetch_retries r
                WHERE r.appid = c.appid AND r.service = 'reviews' AND r.retry_after > ?)
            ORDER BY COALESCE(s.reviews_fetched_at, ''), c.appid
            LIMIT ?
        """, (_cutoff_iso(GLOBAL_STEAM_REVIEWS_MAX_AGE_DAYS), _now().timestamp(), int(limit))).fetchall()
    return [dict(row) for row in rows]


def save_steam_error(db_path, appid, service, error, seconds):
    with connect(db_path) as conn:
        conn.execute("""INSERT INTO steam_fetch_retries VALUES (?, ?, ?, ?)
            ON CONFLICT(appid, service) DO UPDATE SET
              retry_after = excluded.retry_after, last_error = excluded.last_error
        """, (int(appid), service, _now().timestamp() + seconds,
              f"{type(error).__name__}: {error}"[:1000]))


def save_steam_ready(db_path, metadata):
    """Save Steam Store details without touching cached review data."""
    appid = int(metadata["appid"])

    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO steam_metadata (
                appid,
                state,
                genres,
                categories,
                developers,
                publishers,
                release_date,
                is_free,
                fetched_at
            )
            VALUES (?, 'ready', ?, ?, ?, ?, ?, ?, ?)

            ON CONFLICT(appid)
            DO UPDATE SET
                state = 'ready',
                genres = excluded.genres,
                categories = excluded.categories,
                developers = excluded.developers,
                publishers = excluded.publishers,
                release_date = excluded.release_date,
                is_free = excluded.is_free,
                fetched_at = excluded.fetched_at
            """,
            (
                appid,
                json.dumps(metadata.get("genres") or []),
                json.dumps(metadata.get("categories") or []),
                json.dumps(metadata.get("developers") or []),
                json.dumps(metadata.get("publishers") or []),
                metadata.get("release_date"),
                1 if metadata.get("is_free") else 0,
                _now_iso(),
            ),
        )

        conn.execute(
            """
            UPDATE steam_catalog
            SET steam_dirty = 0
            WHERE appid = ?
            """,
            (appid,),
        )
        conn.execute("DELETE FROM steam_fetch_retries WHERE appid = ? AND service = 'details'", (appid,))


def save_steam_reviews(db_path, appid, reviews):
    """Refresh only Steam review summary fields."""
    appid = int(appid)

    if reviews is None:
        return

    with connect(db_path) as conn:
        cursor = conn.execute(
            """
            UPDATE steam_metadata
            SET
                review_score = ?,
                review_description = ?,
                positive_percentage = ?,
                total_positive = ?,
                total_negative = ?,
                total_reviews = ?,
                reviews_fetched_at = ?
            WHERE
                appid = ?
                AND state = 'ready'
            """,
            (
                reviews.get("review_score"),
                reviews.get("review_description"),
                reviews.get("positive_percentage"),
                reviews.get("total_positive"),
                reviews.get("total_negative"),
                reviews.get("total_reviews"),
                _now_iso(),
                appid,
            ),
        )
        conn.execute("DELETE FROM steam_fetch_retries WHERE appid = ? AND service = 'reviews'", (appid,))
        return cursor.rowcount == 1


def save_steam_unavailable(db_path, appid):
    appid = int(appid)

    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO steam_metadata(appid, state, fetched_at)
            VALUES (?, 'unavailable', ?)

            ON CONFLICT(appid)
            DO UPDATE SET
                state = 'unavailable',
                fetched_at = excluded.fetched_at
            """,
            (appid, _now_iso()),
        )

        conn.execute(
            """
            UPDATE steam_catalog
            SET steam_dirty = 0
            WHERE appid = ?
            """,
            (appid,),
        )
        conn.execute("DELETE FROM steam_fetch_retries WHERE appid = ? AND service = 'details'", (appid,))


def hltb_candidates(db_path, limit):
    from hltb_service import has_sequel_conflict
    matched_cutoff = _cutoff_iso(HLTB_CACHE_MAX_AGE_DAYS)
    no_match_cutoff = _cutoff_iso(GLOBAL_HLTB_NO_MATCH_CACHE_DAYS)
    now = _now_iso()

    with connect(db_path) as conn:
        conn.create_function("sequel_conflict", 2, has_sequel_conflict)
        rows = conn.execute(
            """
            SELECT c.appid, c.name
            FROM steam_catalog AS c
            LEFT JOIN hltb_metadata AS h
                ON h.appid = c.appid
            WHERE
                c.name <> ''
                AND (
                    c.hltb_retry_after IS NULL
                    OR c.hltb_retry_after <= ?
                )
                AND (
                    h.appid IS NULL
                    OR c.hltb_dirty = 1
                    OR (h.state = 'matched'
                        AND COALESCE(h.match_confidence, '') <> 'manual'
                        AND sequel_conflict(h.searched_name, h.matched_name))
                    OR (
                        h.state = 'matched'
                        AND h.match_confidence <> 'manual'
                        AND h.fetched_at < ?
                    )
                    OR (
                        h.state = 'no_match'
                        AND (
                            h.matcher_version IS NULL
                            OR h.matcher_version <> ?
                            OR h.fetched_at < ?
                        )
                    )
                )
            ORDER BY
                c.hltb_dirty DESC,
                c.appid ASC
            LIMIT ?
            """,
            (
                now,
                matched_cutoff,
                int(HLTB_MATCHER_VERSION),
                no_match_cutoff,
                int(limit),
            ),
        ).fetchall()

    return [dict(row) for row in rows]


def save_hltb_match(db_path, appid, searched_name, result):
    appid = int(appid)

    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO hltb_metadata (
                appid,
                searched_name,
                state,
                hltb_id,
                matched_name,
                similarity,
                match_confidence,
                matcher_version,
                main_story,
                main_extra,
                completionist,
                all_styles,
                web_link,
                fetched_at
            )
            VALUES (
                ?, ?, 'matched', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )

            ON CONFLICT(appid)
            DO UPDATE SET
                searched_name = excluded.searched_name,
                state = 'matched',
                hltb_id = excluded.hltb_id,
                matched_name = excluded.matched_name,
                similarity = excluded.similarity,
                match_confidence = excluded.match_confidence,
                matcher_version = excluded.matcher_version,
                main_story = excluded.main_story,
                main_extra = excluded.main_extra,
                completionist = excluded.completionist,
                all_styles = excluded.all_styles,
                web_link = excluded.web_link,
                fetched_at = excluded.fetched_at
            """,
            (
                appid,
                searched_name,
                result.get("hltb_id"),
                result.get("matched_name"),
                result.get("similarity"),
                result.get("match_confidence"),
                result.get("matcher_version"),
                result.get("main_story"),
                result.get("main_extra"),
                result.get("completionist"),
                result.get("all_styles"),
                result.get("web_link"),
                _now_iso(),
            ),
        )

        conn.execute(
            """
            UPDATE steam_catalog
            SET
                hltb_dirty = 0,
                hltb_error_count = 0,
                hltb_retry_after = NULL,
                hltb_last_error = NULL,
                hltb_last_error_at = NULL
            WHERE appid = ?
            """,
            (appid,),
        )


def save_hltb_no_match(db_path, appid, searched_name):
    appid = int(appid)

    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO hltb_metadata (
                appid,
                searched_name,
                state,
                matcher_version,
                fetched_at
            )
            VALUES (?, ?, 'no_match', ?, ?)

            ON CONFLICT(appid)
            DO UPDATE SET
                searched_name = excluded.searched_name,
                state = 'no_match',
                hltb_id = NULL,
                matched_name = NULL,
                similarity = NULL,
                match_confidence = NULL,
                matcher_version = excluded.matcher_version,
                main_story = NULL,
                main_extra = NULL,
                completionist = NULL,
                all_styles = NULL,
                web_link = NULL,
                fetched_at = excluded.fetched_at
            """,
            (
                appid,
                searched_name,
                int(HLTB_MATCHER_VERSION),
                _now_iso(),
            ),
        )

        conn.execute(
            """
            UPDATE steam_catalog
            SET
                hltb_dirty = 0,
                hltb_error_count = 0,
                hltb_retry_after = NULL,
                hltb_last_error = NULL,
                hltb_last_error_at = NULL
            WHERE appid = ?
            """,
            (appid,),
        )


def save_hltb_error(db_path, appid, error):
    """Persist a temporary HLTB failure and return its cooldown information."""
    appid = int(appid)
    now = _now()

    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT hltb_error_count
            FROM steam_catalog
            WHERE appid = ?
            """,
            (appid,),
        ).fetchone()

        previous_count = (
            int(row["hltb_error_count"] or 0)
            if row is not None
            else 0
        )
        error_count = previous_count + 1

        if error_count == 1:
            cooldown_hours = 1
        elif error_count == 2:
            cooldown_hours = 6
        else:
            cooldown_hours = 24

        retry_after = now + timedelta(
            hours=cooldown_hours
        )
        error_text = str(error or "").strip()[:500]

        conn.execute(
            """
            UPDATE steam_catalog
            SET
                hltb_dirty = 1,
                hltb_error_count = ?,
                hltb_retry_after = ?,
                hltb_last_error = ?,
                hltb_last_error_at = ?
            WHERE appid = ?
            """,
            (
                error_count,
                retry_after.isoformat(timespec="seconds"),
                error_text,
                now.isoformat(timespec="seconds"),
                appid,
            ),
        )

    return {
        "error_count": error_count,
        "cooldown_hours": cooldown_hours,
        "retry_after": retry_after.isoformat(timespec="seconds"),
    }


def stats(db_path):
    with connect(db_path) as conn:
        result = {
            "catalog": conn.execute(
                "SELECT COUNT(*) FROM steam_catalog"
            ).fetchone()[0],

            "steam_ready": conn.execute(
                """
                SELECT COUNT(*)
                FROM steam_metadata
                WHERE state = 'ready'
                """
            ).fetchone()[0],

            "steam_unavailable": conn.execute(
                """
                SELECT COUNT(*)
                FROM steam_metadata
                WHERE state = 'unavailable'
                """
            ).fetchone()[0],

            "steam_reviews_cached": conn.execute(
                """
                SELECT COUNT(*)
                FROM steam_metadata
                WHERE
                    state = 'ready'
                    AND reviews_fetched_at IS NOT NULL
                """
            ).fetchone()[0],

            "hltb_matched": conn.execute(
                """
                SELECT COUNT(*)
                FROM hltb_metadata
                WHERE state = 'matched'
                """
            ).fetchone()[0],

            "hltb_no_match": conn.execute(
                """
                SELECT COUNT(*)
                FROM hltb_metadata
                WHERE state = 'no_match'
                """
            ).fetchone()[0],

            "hltb_cooldown": conn.execute(
                """
                SELECT COUNT(*)
                FROM steam_catalog
                WHERE
                    hltb_retry_after IS NOT NULL
                    AND hltb_retry_after > ?
                """,
                (_now_iso(),),
            ).fetchone()[0],
        }

    return {key: int(value) for key, value in result.items()}
