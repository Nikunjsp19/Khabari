"""Market-hours window: Mon–Fri 09:00–16:00 (inclusive) America/New_York."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import get_settings


def market_tz() -> ZoneInfo:
    return ZoneInfo(get_settings().market_timezone)


def now_market() -> datetime:
    return datetime.now(market_tz())


def is_market_hours(when: datetime | None = None) -> bool:
    """True Mon–Fri between start_hour and end_hour inclusive (local market tz)."""
    settings = get_settings()
    dt = when.astimezone(market_tz()) if when else now_market()
    if dt.weekday() > 4:  # Sat=5 Sun=6
        return False
    return settings.market_start_hour <= dt.hour <= settings.market_end_hour


def market_hours_status() -> dict:
    settings = get_settings()
    dt = now_market()
    open_now = is_market_hours(dt)
    return {
        "open": open_now,
        "now": dt.isoformat(),
        "timezone": settings.market_timezone,
        "days": "Mon–Fri",
        "hours": f"{settings.market_start_hour:02d}:00–{settings.market_end_hour:02d}:59",
        "weekday": dt.strftime("%A"),
        "message": (
            "Within trading window — analysis allowed"
            if open_now
            else "Outside Mon–Fri 9am–4pm ET — scheduled runs are paused"
        ),
    }
