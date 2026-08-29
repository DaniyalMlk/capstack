"""Assumption series.

Every line of an operating case is a number per period. Some are flat across the
hold, some are written out year by year, and a great many are a glide from one
value to another — growth stepping down from 9% to 3% as the business matures,
or margin building 150bp over five years as a cost programme lands.

Writing those out by hand for every line is where transcription errors get into
a model, so they are expressed once and expanded here.

An assumption is annual. That is a statement about the file rather than about
the grid: 8% growth means 8% over a year and 20% margin means 20% for the year,
and the same file has to describe the same business whether the year is reported
once or twelve times. Two things follow, and they are different for the two
kinds of assumption. A *rate of change* has to be converted before it is applied
more than once a year, which is what :func:`compounded_over` is for. A *level* —
a margin, a capital-intensity ratio — is already a share of something that has
itself been scaled to the period, so it is applied as written.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from decimal import Decimal

from .money import ONE, Money, Numeric, money

__all__ = ["Driver", "compounded_over", "within_year_weights"]


def compounded_over(annual: Money, share: Money) -> Money:
    """The rate that compounds to ``annual`` over the whole of a year.

    ``share`` is the fraction of a year the period covers. Applying 8% in each
    of four quarters grows a business by 36% over the year, which is not what
    the file said. The rate that belongs in a quarter is the one that compounds
    to 8% across four of them — ``1.08 ** 0.25 - 1``, or about 1.943%.

    A ``share`` of exactly one returns ``annual`` unchanged rather than routing
    it through a power. The result would agree to thirty-four digits either way;
    returning it untouched makes an annual grid identical rather than merely
    indistinguishable, which is the property the existing worked examples are
    checked against.

    A rate at or below -100% has no real root above zero. -100% itself is
    admitted, because a business that ends the year at nothing is at nothing
    however the year is divided. Anything below it is rejected rather than
    quietly turned into a positive number by an even root.
    """
    if share <= 0:
        raise ValueError("a period covers a positive share of a year")
    if share == ONE:
        return annual
    base = ONE + annual
    if base < 0:
        raise ValueError(
            f"a rate of {annual} shrinks the base past nothing, so it has no "
            f"equivalent over part of a year"
        )
    return base**share - ONE


def within_year_weights(annual: Money, periods_per_year: int) -> tuple[Money, ...]:
    """How a year's trading divides between the periods that report it.

    A year's revenue is settled before this is asked: the annual line grows at
    the annual rate, once, exactly as it does on an annual grid. What is left is
    the question of where inside the year it was earned, and the answer cannot
    be an equal split, because a business growing at 9% earns more in its fourth
    quarter than in its first.

    So the shares ramp at the rate that compounds to the annual one, and are
    then normalised to sum to one. That gives both of the properties wanted at
    once: the quarters of a year add back to the year exactly, so a file run
    quarterly underwrites the same case it underwrote annually, and the quarters
    still slope, so a covenant tested in the first quarter is tested against a
    first quarter rather than against an average.

    One period a year is the degenerate case and returns a single weight of one,
    which keeps an annual grid untouched.
    """
    if periods_per_year < 1:
        raise ValueError("a year is divided into at least one period")
    if periods_per_year == 1:
        return (ONE,)
    step = ONE + compounded_over(annual, ONE / Decimal(periods_per_year))
    if step <= 0:
        # Only reachable at exactly -100% a year, where the business is at
        # nothing from the first period on. All of the year's trading is in it.
        return tuple(
            ONE if k == 0 else Decimal(0) for k in range(periods_per_year)
        )
    shares = tuple(step**k for k in range(periods_per_year))
    total = sum(shares, Decimal(0))
    weights = [s / total for s in shares[:-1]]
    # The last share is the residual rather than its own quotient, so the
    # weights sum to exactly one and a year divided into twelve reassembles into
    # the same year rather than into one a rounding unit away from it.
    weights.append(ONE - sum(weights, Decimal(0)))
    return tuple(weights)


@dataclass(frozen=True, slots=True)
class Driver:
    """One assumption, resolved to a value for each period."""

    values: tuple[Money, ...]

    def __post_init__(self) -> None:
        if not self.values:
            raise ValueError("a driver needs at least one value")

    @classmethod
    def of(cls, values: Sequence[Numeric]) -> Driver:
        """An explicit value per period."""
        return cls(values=tuple(money(v) for v in values))

    @classmethod
    def constant(cls, value: Numeric, periods: int) -> Driver:
        """The same value in every period."""
        if periods <= 0:
            raise ValueError("a driver must cover at least one period")
        return cls(values=tuple([money(value)] * periods))

    @classmethod
    def ramp(cls, start: Numeric, end: Numeric, periods: int) -> Driver:
        """A straight line from ``start`` to ``end``, inclusive of both.

        The last period holds ``end`` exactly rather than approaching it, which
        is what someone writing "growth tapers from 9% to 3%" means.
        """
        if periods <= 0:
            raise ValueError("a driver must cover at least one period")
        first, last = money(start), money(end)
        if periods == 1:
            return cls(values=(first,))
        span = last - first
        steps = Decimal(periods - 1)
        return cls(values=tuple(first + span * Decimal(i) / steps for i in range(periods)))

    def __len__(self) -> int:
        return len(self.values)

    def __iter__(self) -> Iterator[Money]:
        return iter(self.values)

    def __getitem__(self, index: int) -> Money:
        return self.values[index]

    def at(self, index: int) -> Money:
        """Value for period ``index``, zero-based.

        Reading past the end holds the final value rather than raising. A
        five-year assumption applied to a six-year projection means the last
        year repeats, which is what an analyst extending a case expects, and it
        keeps a one-period mismatch from taking down the whole model.
        """
        if index < 0:
            raise IndexError("period index must not be negative")
        return self.values[min(index, len(self.values) - 1)]

    def extended_to(self, periods: int) -> Driver:
        """This driver stretched to ``periods``, holding the final value."""
        if periods <= 0:
            raise ValueError("a driver must cover at least one period")
        return Driver(values=tuple(self.at(i) for i in range(periods)))
