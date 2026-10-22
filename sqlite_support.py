"""Shared SQLite lifetime and contention helpers."""
import sqlite3


class ClosingConnection(sqlite3.Connection):
    """Keep SQLite's transaction context semantics, then always close."""

    def __exit__(self, *exc):
        try:
            return super().__exit__(*exc)
        finally:
            self.close()


def is_busy(error):
    code = getattr(error, "sqlite_errorcode", 0) or 0
    return isinstance(error, sqlite3.OperationalError) and (
        code & 0xff in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
        or "locked" in str(error).lower()
        or "busy" in str(error).lower()
    )
