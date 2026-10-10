"""NYSE trading days, holidays and hours (checked against nyse.com for 2025-2026)."""
from datetime import date, datetime

from momentum.market_calendar import NEW_YORK, early_close, holidays, market_status


def at(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=NEW_YORK)


def test_2026_holidays_match_nyse():
    assert sorted(holidays(2026)) == [
        date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3), date(2026, 5, 25),
        date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25)]


def test_weekend_holiday_rules():
    assert date(2027, 12, 24) in holidays(2027)        # Christmas on Saturday -> Friday
    assert date(2022, 12, 31) not in holidays(2022)    # New Year's on Saturday is not moved back
    assert date(2023, 1, 2) in holidays(2023)          # New Year's on Sunday -> Monday


def test_open_closed_and_next_open():
    saturday = market_status(at(2026, 10, 10, 12, 0))
    assert saturday == {"open": False, "reason": "Saturday", "closes_at": None, "next_open": "2026-10-12T09:30:00-04:00"}
    assert market_status(at(2026, 10, 12, 9, 29))["open"] is False
    assert market_status(at(2026, 10, 12, 9, 30))["open"] is True
    assert market_status(at(2026, 10, 12, 16, 0))["reason"] == "After the 16:00 New York close"
    assert market_status(at(2026, 11, 26, 11, 0))["reason"] == "Thanksgiving Day"


def test_early_close_days():
    assert early_close(date(2026, 11, 27)) and early_close(date(2026, 12, 24))
    assert not early_close(date(2026, 7, 3))           # a full holiday in 2026, not a half day
    assert market_status(at(2026, 11, 27, 12, 59))["open"] is True
    assert market_status(at(2026, 11, 27, 13, 0))["reason"] == "After the 13:00 early close"
