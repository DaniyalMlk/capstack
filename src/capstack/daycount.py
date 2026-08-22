"""Day-count conventions.

Interest is quoted as an annual rate and accrued over a period that is not a
year, so every accrual needs a rule for what fraction of a year has passed. The
rules disagree with each other by design, and the disagreement is worth real
money: a 500bp margin on a 500 million term loan accrued over a calendar year
differs by about 347,000 between ACT/360 and ACT/365F, because ACT/360 charges
365 days against a 360-day year. Credit agreements name the convention
explicitly, so the model has to as well.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import Enum

from .money import Money, money

__all__ = ["DayCount", "year_fraction", "days_between", "is_leap_year"]


class DayCount(Enum):
    """The conventions this engine supports.

    ``ACT_360`` is the market standard for floating-rate bank debt in USD and
    EUR, which is most of an LBO capital structure. ``THIRTY_360_US`` is bond
    basis and is what fixed-rate notes and most seller paper use. ``ACT_365F``
    is sterling convention and is also the natural choice for discounting.
    ``ACT_ACT_ISDA`` splits an accrual across the year boundary so that each
    part is divided by the length of its own year.
    """

    ACT_365F = "ACT/365F"
    ACT_360 = "ACT/360"
    THIRTY_360_US = "30/360 US"
    ACT_ACT_ISDA = "ACT/ACT ISDA"

    def __str__(self) -> str:
        return self.value


def is_leap_year(year: int) -> bool:
    """Whether ``year`` is a leap year under the Gregorian rules."""
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def days_between(start: date, end: date) -> int:
    """Actual calendar days from ``start`` to ``end``, signed."""
    return (end - start).days


def _thirty_360_us_days(start: date, end: date) -> int:
    """Day count under 30/360 US bond basis.

    The adjustment rules are asymmetric and the asymmetry matters. The end day
    is only pulled back to 30 if the start day was already 30 or 31; otherwise a
    period ending on the 31st genuinely earns the extra day. Applying the
    adjustment unconditionally is the usual implementation bug.
    """
    d1, d2 = start.day, end.day
    if d1 == 31:
        d1 = 30
    if d2 == 31 and d1 == 30:
        d2 = 30
    return 360 * (end.year - start.year) + 30 * (end.month - start.month) + (d2 - d1)


def _act_act_isda(start: date, end: date) -> Money:
    """Year fraction under ACT/ACT ISDA.

    Days falling in a leap year are divided by 366 and the rest by 365, so the
    period is split at each year boundary.

    The days are accumulated into two counters and divided once each, rather
    than dividing year by year and summing the quotients. Both give the same
    real number, but only the first is exact in decimal: 139/365 + 195/365
    rounds twice and lands a unit in the last place away from 334/365, which is
    enough to fail an equality check against a hand-computed figure.
    """
    if start == end:
        return money(0)
    if start > end:
        return -_act_act_isda(end, start)

    days_365 = 0
    days_366 = 0
    for year in range(start.year, end.year + 1):
        year_start = max(start, date(year, 1, 1))
        year_end = min(end, date(year + 1, 1, 1))
        if year_end <= year_start:
            continue
        days = (year_end - year_start).days
        if is_leap_year(year):
            days_366 += days
        else:
            days_365 += days

    total = Decimal(0)
    if days_365:
        total += Decimal(days_365) / Decimal(365)
    if days_366:
        total += Decimal(days_366) / Decimal(366)
    return total


def year_fraction(start: date, end: date, convention: DayCount = DayCount.ACT_365F) -> Money:
    """The fraction of a year between ``start`` and ``end`` under ``convention``.

    A reversed pair returns a negative fraction rather than raising, so that
    accrual arithmetic stays consistent when a schedule is walked backwards.
    """
    if convention is DayCount.ACT_365F:
        return Decimal(days_between(start, end)) / Decimal(365)
    if convention is DayCount.ACT_360:
        return Decimal(days_between(start, end)) / Decimal(360)
    if convention is DayCount.THIRTY_360_US:
        return Decimal(_thirty_360_us_days(start, end)) / Decimal(360)
    if convention is DayCount.ACT_ACT_ISDA:
        return _act_act_isda(start, end)
    raise ValueError(f"unsupported day-count convention: {convention!r}")
