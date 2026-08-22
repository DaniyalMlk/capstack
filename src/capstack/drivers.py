"""Assumption series.

Every line of an operating case is a number per period. Some are flat across the
hold, some are written out year by year, and a great many are a glide from one
value to another — growth stepping down from 9% to 3% as the business matures,
or margin building 150bp over five years as a cost programme lands.

Writing those out by hand for every line is where transcription errors get into
a model, so they are expressed once and expanded here.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from decimal import Decimal

from .money import Money, Numeric, money

__all__ = ["Driver"]


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
