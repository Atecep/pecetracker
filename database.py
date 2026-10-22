import sqlite3
import json

from datetime import datetime
from app_paths import DATABASE_PATH, ensure_data_dir
from sqlite_support import ClosingConnection

SCHEMA_VERSION = 2
CONNECTION_TIMEOUT = 5.0
_UNSET = object()


class GameDataConflict(RuntimeError):
    """A full edit was based on personal data that has since changed."""


def get_connection():
    ensure_data_dir()

    return sqlite3.connect(
        DATABASE_PATH, timeout=CONNECTION_TIMEOUT, factory=ClosingConnection
    )


def init_database():

    with get_connection() as conn:

        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError("This database requires a newer SLT version.")
        if version == SCHEMA_VERSION:
            return
        # Serialize writers BEFORE inspecting the old schema. DDL and the
        # version marker commit together; a failed migration rolls back.
        conn.execute("BEGIN IMMEDIATE")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError("This database requires a newer SLT version.")
        if version == SCHEMA_VERSION:
            return

        # User data
        conn.execute("""
            CREATE TABLE IF NOT EXISTS game_data (
                appid INTEGER PRIMARY KEY,
                status TEXT,
                notes TEXT DEFAULT ''
            )
        """)

        # Metadata retrieved from Steam
        conn.execute("""
            CREATE TABLE IF NOT EXISTS game_metadata (
                appid INTEGER PRIMARY KEY,

                genres TEXT,
                categories TEXT,

                developers TEXT,
                publishers TEXT,

                review_score INTEGER,
                review_description TEXT,
                positive_percentage REAL,
                total_reviews INTEGER,

                fetched_at TEXT
            )
        """)

        # Persistent status for metadata fetch failures/retries.
        # Successful games do not need a row here.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS metadata_fetch_status (
                appid INTEGER PRIMARY KEY,
                state TEXT NOT NULL,
                failure_count INTEGER NOT NULL DEFAULT 0,
                last_failure_at TEXT,
                next_retry_at TEXT,
                last_error TEXT
            )
        """)

        # HowLongToBeat data is cached independently from Steam metadata.
        # We also cache a no_match state so opening the same modal does not
        # repeatedly query HLTB for titles that cannot be matched safely.
        conn.execute("""
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
                fetched_at TEXT NOT NULL
            )
        """)

        # Persistent application-level state.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS app_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        # Add newer HLTB matcher fields to databases created by older app versions.
        hltb_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(hltb_metadata)").fetchall()
        }

        if "match_confidence" not in hltb_columns:
            conn.execute(
                "ALTER TABLE hltb_metadata ADD COLUMN match_confidence TEXT"
            )

        if "matcher_version" not in hltb_columns:
            conn.execute(
                "ALTER TABLE hltb_metadata ADD COLUMN matcher_version INTEGER"
            )

        metadata_columns = {row[1] for row in conn.execute("PRAGMA table_info(game_metadata)")}
        if "reviews_fetched_at" not in metadata_columns:
            conn.execute("ALTER TABLE game_metadata ADD COLUMN reviews_fetched_at TEXT")
            conn.execute("""UPDATE game_metadata SET reviews_fetched_at = fetched_at
                WHERE review_score IS NOT NULL OR review_description IS NOT NULL
                   OR positive_percentage IS NOT NULL OR total_reviews IS NOT NULL""")

        # Migrate the old experimental label to the more accurate state name.
        conn.execute("""
            UPDATE metadata_fetch_status
            SET state = 'metadata_unavailable'
            WHERE state = 'possibly_delisted'
               OR (last_error = 'store_unavailable'
                   AND state <> 'metadata_unavailable')
        """)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

def get_all_game_data():

    with get_connection() as conn:

        cursor = conn.execute("""
            SELECT
                appid,
                status,
                notes
            FROM game_data
        """)

        rows = cursor.fetchall()

    return {
        appid: {
            "status": status,
            "notes": notes or ""
        }

        for appid, status, notes in rows
    }


def save_game_data(
    appid,
    status,
    notes="",
    expected=_UNSET,
):

    with get_connection() as conn:

        if expected is not _UNSET:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT status, notes FROM game_data WHERE appid = ?", (int(appid),)
            ).fetchone()
            baseline = None if expected is None else (
                expected["status"], expected.get("notes") or ""
            )
            if current is not None:
                current = (current[0], current[1] or "")
            if current != baseline:
                raise GameDataConflict("This game was changed in another window.")

        conn.execute("""
            INSERT INTO game_data (
                appid,
                status,
                notes
            )

            VALUES (?, ?, ?)

            ON CONFLICT(appid)

            DO UPDATE SET
                status = excluded.status,
                notes = excluded.notes
        """, (
            appid,
            status,
            notes
        ))


def save_game_status(appid, status):
    """A gallery action changes only status, never a captured copy of notes."""
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO game_data(appid, status, notes) VALUES (?, ?, '')
            ON CONFLICT(appid) DO UPDATE SET status = excluded.status
        """, (int(appid), status))


def import_game_data(games):

    rows = []

    for game in games:

        rows.append((
            int(game["appid"]),
            game["status"],
            game.get(
                "notes",
                ""
            )
        ))

    with get_connection() as conn:

        conn.executemany("""
            INSERT INTO game_data (
                appid,
                status,
                notes
            )

            VALUES (?, ?, ?)

            ON CONFLICT(appid)

            DO UPDATE SET
                status = excluded.status,
                notes = excluded.notes
        """, rows)

    return len(rows)


def get_game_metadata(appid):

    with get_connection() as conn:

        cursor = conn.execute("""
            SELECT
                genres,
                categories,
                developers,
                publishers,
                review_score,
                review_description,
                positive_percentage,
                total_reviews,
                fetched_at
                , reviews_fetched_at

            FROM game_metadata

            WHERE appid = ?
        """, (
            appid,
        ))

        row = cursor.fetchone()

    if row is None:
        return None

    return {
        "appid": appid,

        "genres": json.loads(
            row[0] or "[]"
        ),

        "categories": json.loads(
            row[1] or "[]"
        ),

        "developers": json.loads(
            row[2] or "[]"
        ),

        "publishers": json.loads(
            row[3] or "[]"
        ),

        "review_score": row[4],

        "review_description": row[5],

        "positive_percentage": row[6],

        "total_reviews": row[7],

        "fetched_at": row[8],
        "reviews_fetched_at": row[9],
    }


def get_all_game_metadata():
    """
    Return all Steam metadata currently stored in the local cache.

    The result is keyed by AppID so callers such as Smart Pick can
    match an entire library against cached metadata without making
    one database query per game (and, importantly, without triggering
    any Steam requests).
    """

    with get_connection() as conn:

        cursor = conn.execute("""
            SELECT
                appid,
                genres,
                categories,
                developers,
                publishers,
                review_score,
                review_description,
                positive_percentage,
                total_reviews,
                fetched_at
                , reviews_fetched_at

            FROM game_metadata
        """)

        rows = cursor.fetchall()

    return {
        int(row[0]): {
            "appid": int(row[0]),
            "genres": json.loads(row[1] or "[]"),
            "categories": json.loads(row[2] or "[]"),
            "developers": json.loads(row[3] or "[]"),
            "publishers": json.loads(row[4] or "[]"),
            "review_score": row[5],
            "review_description": row[6],
            "positive_percentage": row[7],
            "total_reviews": row[8],
            "fetched_at": row[9],
            "reviews_fetched_at": row[10],
        }
        for row in rows
    }


def save_game_metadata(metadata):
    """Details and reviews have independent freshness; a failed review is absent."""
    reviews = metadata.get("reviews")
    now = datetime.now().isoformat(timespec="seconds")
    values = {"appid": int(metadata["appid"]), "fetched_at": now,
              "reviews_fetched_at": now if reviews is not None else None}
    for field in ("genres", "categories", "developers", "publishers"):
        values[field] = json.dumps(metadata.get(field) or [])
    for field in ("review_score", "review_description", "positive_percentage", "total_reviews"):
        values[field] = (reviews or {}).get(field)
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO game_metadata(appid,genres,categories,developers,publishers,
                review_score,review_description,positive_percentage,total_reviews,
                fetched_at,reviews_fetched_at)
            VALUES(:appid,:genres,:categories,:developers,:publishers,
                :review_score,:review_description,:positive_percentage,:total_reviews,
                :fetched_at,:reviews_fetched_at)
            ON CONFLICT(appid) DO UPDATE SET
                genres=excluded.genres, categories=excluded.categories,
                developers=excluded.developers, publishers=excluded.publishers,
                fetched_at=excluded.fetched_at,
                review_score=CASE WHEN excluded.reviews_fetched_at IS NOT NULL THEN excluded.review_score ELSE game_metadata.review_score END,
                review_description=CASE WHEN excluded.reviews_fetched_at IS NOT NULL THEN excluded.review_description ELSE game_metadata.review_description END,
                positive_percentage=CASE WHEN excluded.reviews_fetched_at IS NOT NULL THEN excluded.positive_percentage ELSE game_metadata.positive_percentage END,
                total_reviews=CASE WHEN excluded.reviews_fetched_at IS NOT NULL THEN excluded.total_reviews ELSE game_metadata.total_reviews END,
                reviews_fetched_at=COALESCE(excluded.reviews_fetched_at,game_metadata.reviews_fetched_at)
        """, values)


def get_metadata_fetch_status(appid):
    """Return the persistent metadata fetch state for one AppID."""

    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
                state,
                failure_count,
                last_failure_at,
                next_retry_at,
                last_error
            FROM metadata_fetch_status
            WHERE appid = ?
            """,
            (int(appid),),
        ).fetchone()

    if row is None:
        return None

    return {
        "appid": int(appid),
        "state": row[0],
        "failure_count": int(row[1] or 0),
        "last_failure_at": row[2],
        "next_retry_at": row[3],
        "last_error": row[4],
    }


def get_all_metadata_fetch_statuses():
    """Return all persistent metadata fetch states, keyed by AppID."""

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                appid,
                state,
                failure_count,
                last_failure_at,
                next_retry_at,
                last_error
            FROM metadata_fetch_status
            """
        ).fetchall()

    return {
        int(row[0]): {
            "appid": int(row[0]),
            "state": row[1],
            "failure_count": int(row[2] or 0),
            "last_failure_at": row[3],
            "next_retry_at": row[4],
            "last_error": row[5],
        }
        for row in rows
    }


def save_metadata_fetch_status(
    appid,
    state,
    failure_count=0,
    next_retry_at=None,
    last_error=None,
):
    """Persist a metadata fetch/retry state."""

    now = datetime.now().isoformat(timespec="seconds")

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO metadata_fetch_status (
                appid,
                state,
                failure_count,
                last_failure_at,
                next_retry_at,
                last_error
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(appid)
            DO UPDATE SET
                state = excluded.state,
                failure_count = excluded.failure_count,
                last_failure_at = excluded.last_failure_at,
                next_retry_at = excluded.next_retry_at,
                last_error = excluded.last_error
            """,
            (
                int(appid),
                state,
                int(failure_count),
                now,
                next_retry_at,
                last_error,
            ),
        )


def clear_metadata_fetch_status(appid):
    """Clear failure state after a successful metadata fetch."""

    with get_connection() as conn:
        conn.execute(
            "DELETE FROM metadata_fetch_status WHERE appid = ?",
            (int(appid),),
        )


def get_metadata_unavailable_appids():
    """Return AppIDs whose Steam Store metadata is currently unavailable."""

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT appid
            FROM metadata_fetch_status
            WHERE state = 'metadata_unavailable'
            """
        ).fetchall()

    return {int(row[0]) for row in rows}



def get_hltb_metadata(appid):
    """Return cached HowLongToBeat data for one Steam AppID."""

    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
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
            FROM hltb_metadata
            WHERE appid = ?
            """,
            (int(appid),),
        ).fetchone()

    if row is None:
        return None

    return {
        "appid": int(appid),
        "searched_name": row[0],
        "state": row[1],
        "hltb_id": row[2],
        "matched_name": row[3],
        "similarity": row[4],
        "match_confidence": row[5],
        "matcher_version": row[6],
        "main_story": row[7],
        "main_extra": row[8],
        "completionist": row[9],
        "all_styles": row[10],
        "web_link": row[11],
        "fetched_at": row[12],
    }


def get_all_hltb_metadata():
    """Return all cached HowLongToBeat metadata keyed by Steam AppID.

    This is intentionally a single bulk SQLite read so library filters can use
    cached HLTB durations without issuing one database query per game and,
    importantly, without triggering any external HLTB requests.
    """

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
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
            FROM hltb_metadata
            """
        ).fetchall()

    return {
        int(row[0]): {
            "appid": int(row[0]),
            "searched_name": row[1],
            "state": row[2],
            "hltb_id": row[3],
            "matched_name": row[4],
            "similarity": row[5],
            "match_confidence": row[6],
            "matcher_version": row[7],
            "main_story": row[8],
            "main_extra": row[9],
            "completionist": row[10],
            "all_styles": row[11],
            "web_link": row[12],
            "fetched_at": row[13],
        }
        for row in rows
    }


def save_hltb_metadata(
    appid,
    searched_name,
    state,
    hltb_id=None,
    matched_name=None,
    similarity=None,
    match_confidence=None,
    matcher_version=None,
    main_story=None,
    main_extra=None,
    completionist=None,
    all_styles=None,
    web_link=None,
):
    """Persist a successful HLTB match or a cached no-match result."""

    fetched_at = datetime.now().isoformat(timespec="seconds")

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO hltb_metadata (
                appid, searched_name, state, hltb_id, matched_name,
                similarity, match_confidence, matcher_version, main_story,
                main_extra, completionist, all_styles, web_link, fetched_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(appid)
            DO UPDATE SET
                searched_name = excluded.searched_name,
                state = excluded.state,
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
                int(appid), searched_name, state, hltb_id, matched_name,
                similarity, match_confidence, matcher_version, main_story,
                main_extra, completionist, all_styles, web_link, fetched_at,
            ),
        )


def get_app_state(key, default=None):
    """Return one persistent application state value."""

    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT value
            FROM app_state
            WHERE key = ?
            """,
            (str(key),),
        ).fetchone()

    if row is None:
        return default

    return row[0]


def set_app_state(key, value):
    """Persist one application state value."""

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO app_state (key, value)
            VALUES (?, ?)
            ON CONFLICT(key)
            DO UPDATE SET value = excluded.value
            """,
            (str(key), str(value)),
        )


def save_library_appids(steam_id, games):
    """Store ownership separately from the subset with personal edits."""
    appids = sorted({int(game["appid"]) for game in games})
    set_app_state(f"owned_appids:{steam_id}", json.dumps(appids))


def get_library_appids(steam_id):
    value = get_app_state(f"owned_appids:{steam_id}")
    return None if value is None else [int(appid) for appid in json.loads(value)]


def import_global_metadata(global_db_path, library_records):
    """Import missing Steam/HLTB metadata for the current user library.

    Existing local metadata is never overwritten. Manual HLTB overrides
    therefore always remain authoritative.
    """

    appids = sorted(
        {
            int(game["AppID"])
            for game in library_records
            if game.get("AppID") is not None
        }
    )

    if not appids:
        return {
            "steam_imported": 0,
            "hltb_imported": 0,
        }

    with get_connection() as conn:
        conn.execute(
            "ATTACH DATABASE ? AS global_metadata_db",
            (str(global_db_path),),
        )

        try:
            source_columns = {row[1] for row in conn.execute("PRAGMA global_metadata_db.table_info(steam_metadata)")}
            reviews_time = "s.reviews_fetched_at" if "reviews_fetched_at" in source_columns else "CASE WHEN s.total_reviews IS NOT NULL THEN s.fetched_at END"
            # Validate the columns this app version depends on.
            conn.execute(
                """
                SELECT
                    appid,
                    state,
                    genres,
                    categories,
                    developers,
                    publishers,
                    review_score,
                    review_description,
                    positive_percentage,
                    total_reviews
                FROM global_metadata_db.steam_metadata
                LIMIT 0
                """
            )

            conn.execute(
                """
                SELECT
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
                    web_link
                FROM global_metadata_db.hltb_metadata
                LIMIT 0
                """
            )

            conn.execute(
                """
                CREATE TEMP TABLE bootstrap_library_appids (
                    appid INTEGER PRIMARY KEY
                )
                """
            )

            conn.executemany(
                """
                INSERT INTO bootstrap_library_appids(appid)
                VALUES (?)
                """,
                ((appid,) for appid in appids),
            )

            conn.execute(
                f"""
                INSERT INTO game_metadata (
                    appid,
                    genres,
                    categories,
                    developers,
                    publishers,
                    review_score,
                    review_description,
                    positive_percentage,
                    total_reviews,
                    fetched_at, reviews_fetched_at
                )
                SELECT
                    s.appid,
                    s.genres,
                    s.categories,
                    s.developers,
                    s.publishers,
                    s.review_score,
                    s.review_description,
                    s.positive_percentage,
                    s.total_reviews,
                    s.fetched_at, {reviews_time}
                FROM global_metadata_db.steam_metadata AS s
                INNER JOIN bootstrap_library_appids AS l
                    ON l.appid = s.appid
                LEFT JOIN game_metadata AS local
                    ON local.appid = s.appid
                WHERE
                    s.state = 'ready'
                    AND local.appid IS NULL
                """,
            )

            steam_imported = int(
                conn.execute(
                    "SELECT changes()"
                ).fetchone()[0]
            )

            # A centrally resolved Steam row supersedes any old local
            # unavailable/retry marker for the same AppID.
            conn.execute(
                """
                DELETE FROM metadata_fetch_status
                WHERE appid IN (
                    SELECT s.appid
                    FROM global_metadata_db.steam_metadata AS s
                    INNER JOIN bootstrap_library_appids AS l
                        ON l.appid = s.appid
                    WHERE s.state = 'ready'
                )
                """
            )

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
                SELECT
                    h.appid,
                    h.searched_name,
                    h.state,
                    h.hltb_id,
                    h.matched_name,
                    h.similarity,
                    h.match_confidence,
                    h.matcher_version,
                    h.main_story,
                    h.main_extra,
                    h.completionist,
                    h.all_styles,
                    h.web_link,
                    h.fetched_at
                FROM global_metadata_db.hltb_metadata AS h
                INNER JOIN bootstrap_library_appids AS l
                    ON l.appid = h.appid
                LEFT JOIN hltb_metadata AS local
                    ON local.appid = h.appid
                WHERE
                    h.state IN ('matched', 'no_match')
                    AND local.appid IS NULL
                """,
            )

            hltb_imported = int(
                conn.execute(
                    "SELECT changes()"
                ).fetchone()[0]
            )

            conn.execute(
                "DROP TABLE bootstrap_library_appids"
            )
            conn.commit()

        finally:
            try:
                conn.execute(
                    "DETACH DATABASE global_metadata_db"
                )
            except Exception:
                pass

    return {
        "steam_imported": steam_imported,
        "hltb_imported": hltb_imported,
    }

