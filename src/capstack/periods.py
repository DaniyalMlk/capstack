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
    def is_stub(self) -> bool:
        """Whether this is the short period between close and the first reporting date."""
        return self.index == 0

    @property
    def driver_index(self) -> int:
        """Where to read an assumption series for this period, zero-based.

        A stub is not a year of its own. A deal closing in November trades six
        weeks of the year it closed in, at that year's margin and that year's
        capital intensity, and the first full period is still year one — so the
        stub and the period after it read the same assumptions.

        Without a stub the mapping is the identity it always was: period 1 reads
        index 0, period 2 reads index 1, and a grid built the old way is
        indistinguishable from one built today.
        """
        return max(self.index - 1, 0)

    @property
    def days(self) -> int:
        return (self.end - self.start).days

    def year_fraction(self, convention: DayCount = DayCount.ACT_365F) -> Money:
        """Length of this period as a fraction of a year."""
        return year_fraction(self.start, self.end, convention)

    @property
    def label(self) -> str:
        """A short label of the kind that would head a column."""
        if self.is_stub:
            return f"Stub to {self.end.isoformat()}"
        return f"P{self.index} to {self.end.isoformat()}"

    @property
    def short_label(self) -> str:
        """What heads a narrow column: ``Stub`` or ``P3``."""
        return "Stub" if self.is_stub else f"P{self.index}"

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
        if any(p.is_stub for p in self.periods[1:]):
            raise ValueError("a stub is the first period or it is not a stub")
        for earlier, later in zip(self.periods, self.periods[1:]):
            if later.start != earlier.end:
                raise ValueError(
                    f"gap in grid: period {earlier.index} ends {earlier.end} "
                    f"but period {later.index} starts {later.start}"
                )
            if later.index != earlier.index + 1:
                raise ValueError("period indices must run consecutively")

    @classmethod
    def build(
        cls,
        close: date,
        years: int,
        frequency: Frequency = Frequency.ANNUAL,
        *,
        stub_to: date | None = None,
    ) -> PeriodGrid:
        """Build ``years`` years of periods forward from ``close``.

        ``close`` is the transaction date, so it is the start of period 1 rather
        than a period in its own right.

        ``stub_to`` is the first reporting date, for a deal that does not close
        on one. A deal signing on 15 November against a 31 December year end
        trades six weeks before its first accounts, and modelling that as a full
        period puts the trading in the wrong place and dates every column after
        it early — which moves the exit date, the holding period and therefore
        the rate of return on a deal where nothing else changed.

        The stub is period 0 and the whole periods that follow are anchored on
        ``stub_to`` rather than on ``close``, so period ends fall on reporting
        dates rather than on deal anniversaries. ``years`` counts the whole
        periods; the stub is extra, being a fraction of the year it sits in.

        A ``stub_to`` exactly one whole period after ``close`` describes no stub
        at all, and is built as the ordinary grid rather than as a first period
        of full length numbered zero.
        """
        if years <= 0:
            raise ValueError("projection must cover at least one year")
        count = years * frequency.periods_per_year
        step = frequency.months
        periods: list[Period] = []
        anchor = close

        if stub_to is not None:
            if stub_to <= close:
                raise ValueError(
                    f"the first reporting date ({stub_to}) is on or before the close "
                    f"date ({close}), so there is no stub between them"
                )
            if stub_to > add_months(close, step):
                raise ValueError(
                    f"a stub is shorter than a whole period, and {close} to {stub_to} "
                    f"is longer than one {frequency}"
                )
            if stub_to != add_months(close, step):
                periods.append(Period(index=0, start=close, end=stub_to))
            anchor = stub_to

        # The whole periods are numbered from one whether or not a stub sits in
        # front of them, so a maturity or an event stated as "period 2" means
        # the same thing in either grid.
        for i in range(count):
            periods.append(
                Period(
                    index=i + 1,
                    start=add_months(anchor, i * step),
                    end=add_months(anchor, (i + 1) * step),
                )
            )
        return cls(periods=tuple(periods), frequency=frequency)

    @property
    def has_stub(self) -> bool:
        return self.periods[0].is_stub

    @property
    def stub(self) -> Period | None:
        """The short period at close, if the grid has one."""
        return self.periods[0] if self.has_stub else None

    @property
    def whole_periods(self) -> tuple[Period, ...]:
        """Every period but the stub."""
        return tuple(p for p in self.periods if not p.is_stub)

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
