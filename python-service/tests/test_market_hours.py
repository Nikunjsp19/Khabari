"""Unit tests for the trade window (Mon–Fri 09:00–16:00 ET) gate."""

from datetime import datetime
from zoneinfo import ZoneInfo

from app.market_hours import is_market_hours, market_hours_status

ET = ZoneInfo("America/New_York")


def _et(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=ET)


def test_open_at_start_and_close():
    # Monday 2026-07-13
    assert is_market_hours(_et(2026, 7, 13, 9, 0)) is True  # 9:00am open
    assert is_market_hours(_et(2026, 7, 13, 16, 0)) is True  # 4:00pm close (inclusive)


def test_closed_before_open_and_after_close():
    assert is_market_hours(_et(2026, 7, 13, 8, 59)) is False  # before 9am
    assert is_market_hours(_et(2026, 7, 13, 16, 1)) is False  # 4:01pm past close


def test_four_pm_hour_no_longer_open():
    # Regression: previously the whole 16:xx hour counted as open (until 4:59pm).
    assert is_market_hours(_et(2026, 7, 13, 16, 30)) is False
    assert is_market_hours(_et(2026, 7, 13, 16, 59)) is False


def test_midday_open():
    assert is_market_hours(_et(2026, 7, 13, 12, 15)) is True
    assert is_market_hours(_et(2026, 7, 13, 15, 59)) is True


def test_weekend_closed():
    # Saturday 2026-07-11 / Sunday 2026-07-12
    assert is_market_hours(_et(2026, 7, 11, 12, 0)) is False
    assert is_market_hours(_et(2026, 7, 12, 12, 0)) is False


def test_status_reports_window_ending_at_4pm():
    status = market_hours_status()
    assert status["hours"] == "09:00–16:00"
