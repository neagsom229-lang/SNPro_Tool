from datetime import datetime, timezone

def utcnow():
    """Return timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)

def ensure_utc(dt):
    """Ensure a datetime object from DB is timezone-aware in UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt
