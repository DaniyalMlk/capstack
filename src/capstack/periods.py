"""Projection period grids.

An LBO model is a table whose columns are periods. Building that spine correctly
is unglamorous and easy to get wrong in one specific way: adding a month to
31 January. There is no 31 February, so the naive answer overflows into March
and every subsequent period in the schedule is anchored to the wrong day.

The rule used here is the one credit agreements use. A date is clamped to the
last day of the target month, and — this is the part that is usually missed —
the clamping is applied to the *original* anchor each time rather than
compounding. A schedule anchored on 31 January runs 31 Jan, 28 Feb, 31 Mar, not
31 Jan, 28 Feb, 28 Mar.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from enum import Enum

from .daycount import DayCount, year_fraction
from .money import Money

__all__ = ["Frequency", "Period", "PeriodGrid", "add_months", "end_of_month"]


class Frequency(Enum):
    """How often the model steps."""

    ANNUAL = 1
    SEMI_ANNUAL = 2
    QUARTERLY = 4
    MONTHLY = 12

    @property
    def months(self) -> int:
        """Calendar months in one step at this frequency."""
        return 12 // self.value

    @property
    def periods_per_year(self) -> int:
        return self.value

    def __str__(self) -> str:
        return self.name.replace("_", "-").lower()


def end_of_month(year: int, month: int) -> int:
    """Last day number of the given month."""
    return calendar.monthrange(year, month)[1]


def add_months(anchor: date, months: int) -> date:
    """Advance ``anchor`` by ``months``, clamping to the end of the target month.

    Clamping always measures from the anchor's own day number, so the day does
    not ratchet downwards over a long schedule.
    """
    total = anchor.month - 1 + months
    year = anchor.year + total // 12
    month = total % 12 + 1
    day = min(anchor.day, end_of_month(year, month))
    return date(year, month, day)


@dataclass(frozen=True, slots=True)
class Period:
    """One column of the model.

    ``index`` is 1-based: period 1 is the first full period after close, which
    matches how deal teams talk about the model. The stub before it, if any, is
    period 0.
    """

    index: int
    start: date
    end: date

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(f"period {self.index} ends ({self.end}) before it starts ({self.start})")
        if self.index < 0:
            raise ValueError("period index must not be negative")

    @property
    def days(self) -> int:
        return (self.end - self.start).days

    def year_fraction(self, convention: DayCount = DayCount.ACT_365F) -> Money:
        """Length of this period as a fraction of a year."""
        return year_fraction(self.start, self.end, convention)

    @property
    def label(self) -> str:
        """A short label of the kind that would head a column."""
        return f"P{self.index} to {self.end.isoformat()}"

    def __str__(self) -> str:
        return self.label


@dataclass(frozen=True, slots=True)
class PeriodGrid:
    """An ordered, contiguous set of projection periods.

    Contiguity is enforced rather than assumed: each period starts on the day
    the previous one ended. Interest accrual and balance roll-forward both
    depend on it, and a gap would silently lose a day of accrual.
    """

    periods: tuple[Period, ...]
    frequency: Frequency

    def __post_init__(self) -> None:
        if not self.periods:
            raise ValueError("a grid needs at least one period")
        for earlier, later in zip(self.periods, self.periods[1:]):
            if later.start != earlier.end:
                raise ValueError(
                    f"gap in grid: period {earlier.index} ends {earlier.end} "
                    f"but period {later.index} starts {later.start}"
                )
            if later.index != earlier.index + 1:
                raise ValueError("period indices must run consecutively")

    @classmethod
    def build(cls, close: date, years: int, frequency: Frequency = Frequency.ANNUAL) -> PeriodGrid:
        """Build ``years`` years of periods forward from ``close``.

        ``close`` is the transaction date, so it is the start of period 1 rather
        than a period in its own right.
        """
        if years <= 0:
            raise ValueError("projection must cover at least one year")
        count = years * frequency.periods_per_year
        step = frequency.months
        periods = []
        for i in range(count):
            start = add_months(close, i * step)
            end = add_months(close, (i + 1) * step)
            periods.append(Period(index=i + 1, start=start, end=end))
        return cls(periods=tuple(periods), frequency=frequency)

    @property
    def start(self) -> date:
        return self.periods[0].start

    @property
    def end(self) -> date:
        return self.periods[-1].end

    def __len__(self) -> int:
        return len(self.periods)

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self.periods)

    def __getitem__(self, index: int) -> Period:
        return self.periods[index]

    def period_ending_on_or_after(self, when: date) -> Period:
        """The first period whose end is on or after ``when``.

        Used to locate an exit date on the grid.
        """
        for period in self.periods:
            if period.end >= when:
                return period
        raise ValueError(f"{when} falls beyond the end of the grid ({self.end})")
