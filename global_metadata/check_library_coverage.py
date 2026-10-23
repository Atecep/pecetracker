from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path


def _project_root() -> Path:
    # Intended location: global_metadata/check_library_coverage.py
    # Also works if temporarily placed in the repository root.
    here = Path(__file__).resolve().parent

    if (here / "database.py").exists():
        return here

    parent = here.parent
    if (parent / "database.py").exists():
        return parent

    raise SystemExit(
        "Could not find the Pecetracker project root. "
        "Place this script in the repository root or in global_metadata/."
    )


PROJECT_ROOT = _project_root()
sys.path.insert(0, str(PROJECT_ROOT))

from database import get_library_appids  # noqa: E402
from settings import load_settings, SettingsError  # noqa: E402
from sqlite_support import ClosingConnection  # noqa: E402


def _percentage(value: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return (value / total) * 100.0


def _print_metric(label: str, value: int, total: int) -> None:
    print(
        f"  {label:<24} {value:>5} / {total:<5} "
        f"({_percentage(value, total):6.2f}%)"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Measure how much of the local Steam library is already covered "
            "by the Pecetracker global metadata database."
        )
    )
    parser.add_argument(
        "global_db",
        type=Path,
        help="Path to the downloaded global_metadata.db",
    )
    parser.add_argument("--steam-id", help="Account whose last successful ownership snapshot should be measured")
    args = parser.parse_args()

    global_db = args.global_db.expanduser().resolve()

    if not global_db.is_file():
        raise SystemExit(f"Global database not found: {global_db}")

    try:
        steam_id = args.steam_id or load_settings()["steam_id"]
    except SettingsError as error:
        raise SystemExit(str(error)) from error
    appids = get_library_appids(steam_id)

    if appids is None:
        raise SystemExit("No ownership snapshot is available for this account. Refresh the library in Pecetracker first.")

    if not appids:
        raise SystemExit(
            "The local library database contains no games. "
            "Open Pecetracker and sync the Steam library first."
        )

    with sqlite3.connect(global_db.as_uri() + "?mode=ro", uri=True, factory=ClosingConnection) as conn:
        conn.row_factory = sqlite3.Row

        # A temporary table avoids SQLite's host-parameter limit for
        # libraries containing thousands of AppIDs.
        conn.execute(
            """
            CREATE TEMP TABLE library_appids (
                appid INTEGER PRIMARY KEY
            )
            """
        )
        conn.executemany(
            "INSERT INTO library_appids(appid) VALUES (?)",
            ((appid,) for appid in appids),
        )

        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total,

                SUM(CASE
                    WHEN c.appid IS NOT NULL THEN 1 ELSE 0
                END) AS in_catalog,

                SUM(CASE
                    WHEN s.state = 'ready' THEN 1 ELSE 0
                END) AS steam_ready,

                SUM(CASE
                    WHEN s.state = 'unavailable' THEN 1 ELSE 0
                END) AS steam_unavailable,

                SUM(CASE
                    WHEN
                        s.state = 'ready'
                        AND s.reviews_fetched_at IS NOT NULL
                    THEN 1 ELSE 0
                END) AS steam_reviews,

                SUM(CASE
                    WHEN h.state = 'matched' THEN 1 ELSE 0
                END) AS hltb_matched,

                SUM(CASE
                    WHEN h.state = 'no_match' THEN 1 ELSE 0
                END) AS hltb_no_match

            FROM library_appids AS l
            LEFT JOIN steam_catalog AS c
                ON c.appid = l.appid
            LEFT JOIN steam_metadata AS s
                ON s.appid = l.appid
            LEFT JOIN hltb_metadata AS h
                ON h.appid = l.appid
            """
        ).fetchone()

        total = int(row["total"] or 0)
        in_catalog = int(row["in_catalog"] or 0)
        steam_ready = int(row["steam_ready"] or 0)
        steam_unavailable = int(row["steam_unavailable"] or 0)
        steam_reviews = int(row["steam_reviews"] or 0)
        hltb_matched = int(row["hltb_matched"] or 0)
        hltb_no_match = int(row["hltb_no_match"] or 0)

        steam_resolved = steam_ready + steam_unavailable
        steam_pending = total - steam_resolved
        hltb_resolved = hltb_matched + hltb_no_match
        hltb_pending = total - hltb_resolved
        missing_catalog = total - in_catalog

        print()
        print("[library-coverage] Local Steam library")
        print(f"  Total games:             {total}")
        print()

        print("[library-coverage] Global Steam catalogue")
        _print_metric("Present in catalogue:", in_catalog, total)
        _print_metric("Missing from catalogue:", missing_catalog, total)
        print()

        print("[library-coverage] Steam metadata")
        _print_metric("Ready:", steam_ready, total)
        _print_metric("Unavailable:", steam_unavailable, total)
        _print_metric("Resolved total:", steam_resolved, total)
        _print_metric("Still pending:", steam_pending, total)
        _print_metric("Reviews cached:", steam_reviews, total)
        print()

        print("[library-coverage] HowLongToBeat")
        _print_metric("Matched:", hltb_matched, total)
        _print_metric("No reliable match:", hltb_no_match, total)
        _print_metric("Resolved total:", hltb_resolved, total)
        _print_metric("Still pending:", hltb_pending, total)
        print()

        if steam_ready >= int(total * 0.80):
            steam_verdict = "GOOD for bootstrap testing"
        elif steam_ready >= int(total * 0.70):
            steam_verdict = "CLOSE to bootstrap-testing target"
        else:
            steam_verdict = "still early for bootstrap testing"

        if hltb_resolved >= int(total * 0.70):
            hltb_verdict = "GOOD for bootstrap testing"
        elif hltb_resolved >= int(total * 0.60):
            hltb_verdict = "CLOSE to bootstrap-testing target"
        else:
            hltb_verdict = "still early for bootstrap testing"

        print("[library-coverage] Quick verdict")
        print(
            f"  Steam ready:             {_percentage(steam_ready, total):6.2f}%"
            f"  -> {steam_verdict}"
        )
        print(
            f"  HLTB resolved:           {_percentage(hltb_resolved, total):6.2f}%"
            f"  -> {hltb_verdict}"
        )
        print()


if __name__ == "__main__":
    main()
