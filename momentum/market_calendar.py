"""
market_calendar.py

When the US stock market (NYSE/Nasdaq) is open: weekdays 9:30-16:00 New York
time, minus exchange holidays, with 13:00 early closes. Holiday rules follow
NYSE: a holiday on Saturday is observed the Friday before and one on Sunday
the Monday after, except New Year's Day on a Saturday, which isn't moved.

Pure date arithmetic, no network. Rules only change when NYSE adds a holiday
(Juneteenth was added in 2022), so check this list against nyse.com yearly.
"""

from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
OPEN, CLOSE, EARLY_CLOSE = time(9, 30), time(16, 0), time(13, 0)


def _nth_weekday(year, month, weekday, n):
    """The nth (1-based) given weekday of a month; n=-1 means the last one."""
    if n > 0:
        first = date(year, month, 1)
        return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    last = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _easter(year):
    """Gregorian Easter Sunday (anonymous Gregorian algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def _observed(day):
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


@lru_cache(maxsize=None)
def holidays(year):
    """{date: name} of full-day NYSE closures in a year."""
    days = {
        _nth_weekday(year, 1, 0, 3): "Martin Luther King Jr. Day",
        _nth_weekday(year, 2, 0, 3): "Washington's Birthday",
        _easter(year) - timedelta(days=2): "Good Friday",
        _nth_weekday(year, 5, 0, -1): "Memorial Day",
        _observed(date(year, 7, 4)): "Independence Day",
        _nth_weekday(year, 9, 0, 1): "Labor Day",
        _nth_weekday(year, 11, 3, 4): "Thanksgiving Day",
        _observed(date(year, 12, 25)): "Christmas Day",
    }
    new_year = date(year, 1, 1)
    if new_year.weekday() != 5:  # Saturday: not moved to Dec 31
        days[_observed(new_year)] = "New Year's Day"
    if year >= 2022:
        days[_observed(date(year, 6, 19))] = "Juneteenth"
    return days


def early_close(day):
    """True on the half days that close at 13:00: July 3, the day after
    Thanksgiving, and Christmas Eve, when they're regular trading days."""
    candidates = {date(day.year, 7, 3), _nth_weekday(day.year, 11, 3, 4) + timedelta(days=1), date(day.year, 12, 24)}
    return day in candidates and is_trading_day(day)


def is_trading_day(day):
    return day.weekday() < 5 and day not in holidays(day.year)


def closed_reason(day):
    """Why the market is closed all day, or None on a trading day."""
    if day.weekday() >= 5:
        return day.strftime("%A")
    return holidays(day.year).get(day)


def session(day):
    """(open, close) datetimes in New York for a trading day, else None."""
    if not is_trading_day(day):
        return None
    close = EARLY_CLOSE if early_close(day) else CLOSE
    return datetime.combine(day, OPEN, NEW_YORK), datetime.combine(day, close, NEW_YORK)


def market_status(now=None):
    """{"open": bool, "reason": why it's closed, "closes_at", "next_open"} for
    `now` (default: the current time)."""
    now = (now or datetime.now(NEW_YORK)).astimezone(NEW_YORK)
    today = session(now.date())
    if today and today[0] <= now < today[1]:
        return {"open": True, "reason": None, "closes_at": today[1].isoformat(), "next_open": None}
    if today and now < today[0]:
        reason, next_open = "Before the 9:30 New York open", today[0]
    else:
        reason = closed_reason(now.date()) or ("After the 13:00 early close" if today and early_close(now.date())
                                               else "After the 16:00 New York close")
        day = now.date() + timedelta(days=1)
        while not is_trading_day(day):
            day += timedelta(days=1)
        next_open = session(day)[0]
    return {"open": False, "reason": reason, "closes_at": None, "next_open": next_open.isoformat()}
