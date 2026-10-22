"""Read legacy local timestamps and catalogue UTC timestamps consistently."""
from datetime import datetime, timedelta, timezone


def parse_timestamp(value):
    parsed = datetime.fromisoformat(value)
    # astimezone interprets legacy naive values in the machine's local zone.
    return parsed.astimezone(timezone.utc)


def is_fresh(value, days):
    try:
        return datetime.now(timezone.utc) - parse_timestamp(value) < timedelta(days=days)
    except (TypeError, ValueError):
        return False
