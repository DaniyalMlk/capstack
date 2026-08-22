"""Discounting and the return measures.

The measure a sponsor is judged on is the internal rate of return, which is a
root of the net-present-value function. That function is a polynomial in the
discount factor, and finding its root is where naive implementations fail.

Two failure modes matter and both are handled explicitly here rather than
papered over:

*No root.* A stream that never changes sign has no internal rate of return at
all. There is no rate at which a series of pure outflows has zero present value.
The honest answer is to refuse, not to return whatever the iteration drifted to.

*More than one root.* Descartes' rule of signs bounds the number of positive
roots by the number of sign changes in the coefficient sequence. A stream that
goes out, comes back, goes out again — a bolt-on acquisition funded by a follow-
on equity cheque, say — can genuinely have two rates at which its present value
is zero, and neither is more the answer than the other. Reporting the first one
the solver happened to land on is how a model comes to claim a 40% return on a
deal that also returns 5%. This module finds every root in the search range and
refuses to pick between them.

The solver itself is Brent's method: bisection's guarantee of convergence, with
inverse quadratic interpolation to get there quickly. It cannot leave the
bracket, which is what makes it the right choice over Newton here.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .daycount import DayCount, year_fraction
from .money import ZERO, Money, Numeric, money, to_float

__all__ = [
    "CashFlow",
    "CashFlowStream",
    "IRRError",
    "NoSignChange",
    "AmbiguousIRR",
    "DidNotConverge",
    "brent_root",
    "npv_periodic",
    "irr_periodic",
    "moic",
    "cagr",
]


class IRRError(ValueError):
    """Base for every reason an internal rate of return cannot be reported."""


class NoSignChange(IRRError):
    """The stream never changes sign, so no rate sets its present value to zero."""


class AmbiguousIRR(IRRError):
    """More than one rate sets the present value to zero.

    Carries every root found so the caller can decide what to do — typically
    quote the one nearest a hurdle, or fall back to a money multiple.
    """

    def __init__(self, roots: Sequence[float]) -> None:
        self.roots = tuple(roots)
        formatted = ", ".join(f"{r:.6%}" for r in self.roots)
        super().__init__(
            f"the stream has {len(self.roots)} internal rates of return ({formatted}); "
            "none of them is the answer on its own"
        )


class DidNotConverge(IRRError):
    """The solver exhausted its iteration budget inside a valid bracket."""


# --------------------------------------------------------------------------
# Root finding
# --------------------------------------------------------------------------

def brent_root(
    f: Callable[[float], float],
    lower: float,
    upper: float,
    *,
    xtol: float = 1e-12,
    rtol: float = 8.9e-16,
    max_iter: int = 200,
) -> float:
    """Find a root of ``f`` in ``[lower, upper]`` by Brent's method.

    The interval must bracket a root: ``f(lower)`` and ``f(upper)`` must have
    opposite signs. Given that, the method always converges, because it falls
    back to bisection whenever interpolation would step outside the bracket or
    fail to shrink it fast enough.
    """
    xpre, xcur = float(lower), float(upper)
    fpre, fcur = f(xpre), f(xcur)

    if fpre == 0.0:
        return xpre
    if fcur == 0.0:
        return xcur
    if fpre * fcur > 0.0:
        raise ValueError("the interval does not bracket a root")

    xblk = 0.0
    fblk = 0.0
    spre = 0.0
    scur = 0.0

    for _ in range(max_iter):
        if fpre * fcur < 0.0:
            xblk, fblk = xpre, fpre
            spre = scur = xcur - xpre
        if abs(fblk) < abs(fcur):
            xpre, xcur, xblk = xcur, xblk, xcur
            fpre, fcur, fblk = fcur, fblk, fcur

        delta = (xtol + rtol * abs(xcur)) / 2.0
        sbis = (xblk - xcur) / 2.0

        if fcur == 0.0 or abs(sbis) < delta:
            return xcur

        if abs(spre) > delta and abs(fcur) < abs(fpre):
            if xpre == xblk:
                # Two distinct points only: secant step.
                stry = -fcur * (xcur - xpre) / (fcur - fpre)
            else:
                # Three points: inverse quadratic interpolation.
                dpre = (fpre - fcur) / (xpre - xcur)
                dblk = (fblk - fcur) / (xblk - xcur)
                stry = -fcur * (fblk * dblk - fpre * dpre) / (dblk * dpre * (fblk - fpre))
            if 2.0 * abs(stry) < min(abs(spre), 3.0 * abs(sbis) - delta):
                spre, scur = scur, stry
            else:
                spre = scur = sbis
        else:
            spre = scur = sbis

        xpre, fpre = xcur, fcur
        if abs(scur) > delta:
            xcur += scur
        else:
            xcur += delta if sbis > 0.0 else -delta
        fcur = f(xcur)

    raise DidNotConverge(f"no root after {max_iter} iterations")


# --------------------------------------------------------------------------
# Periodic measures
# --------------------------------------------------------------------------

#: The search range for an internal rate of return. The lower bound sits just
#: above -100%, where the discount factor is singular. The upper bound is
#: deliberately generous: venture-style outcomes can exceed 1000% a year, and a
#: bound that excludes them turns a real answer into a spurious "no root".
RATE_FLOOR = -0.999_999
RATE_CEILING = 100.0

#: The scan is graded rather than uniform. Two roots are only found separately
#: if the scan puts a point between them, so resolution has to be fine where
#: deal returns actually live. A uniform scan across a range this wide would
#: need tens of thousands of points to achieve the same thing below 100%.
_SCAN_BANDS: tuple[tuple[float, float, float], ...] = (
    (RATE_FLOOR, 1.0, 0.002),  # -100% to 100%, resolved to a fifth of a point
    (1.0, 10.0, 0.05),  # 100% to 1000%
    (10.0, RATE_CEILING, 1.0),  # beyond that, coarse
)


def _scan_grid() -> list[float]:
    """Rates at which the present value is sampled looking for sign changes."""
    grid = [RATE_FLOOR]
    for lower, upper, step in _SCAN_BANDS:
        count = int(round((upper - lower) / step))
        grid.extend(lower + i * step for i in range(1, count + 1))
    return grid


def _sign_changes(amounts: Sequence[Money]) -> int:
    """Count sign changes in the sequence, skipping zeros."""
    changes = 0
    previous = 0
    for amount in amounts:
        current = 1 if amount > 0 else (-1 if amount < 0 else 0)
        if current == 0:
            continue
        if previous != 0 and current != previous:
            changes += 1
        previous = current
    return changes


def npv_periodic(rate: Numeric, amounts: Sequence[Money], *, first_period_offset: int = 0) -> Money:
    """Net present value of evenly spaced ``amounts`` at ``rate``.

    ``amounts[0]`` falls at time ``first_period_offset``, which is zero by
    convention: the first flow is the one happening now.
    """
    r = money(rate)
    if r <= -1:
        raise ValueError("discount rate must exceed -100%")
    base = Decimal(1) + r
    total = ZERO
    for i, amount in enumerate(amounts):
        total += amount / (base ** (i + first_period_offset))
    return total


def _npv_float(rate: float, amounts: Sequence[float], times: Sequence[float]) -> float:
    """Floating-point present value, for the solver's inner loop."""
    base = 1.0 + rate
    total = 0.0
    for amount, t in zip(amounts, times):
        total += amount / (base**t)
    return total


def _solve_rate(amounts: Sequence[Money], times: Sequence[float]) -> float:
    """Find the unique rate at which the stream's present value is zero.

    Scans the search range for sign changes, then refines each one. Raises if
    there is no root, or if there is more than one.
    """
    if _sign_changes(amounts) == 0:
        raise NoSignChange(
            "the stream does not change sign, so it has no internal rate of return"
        )

    values = [to_float(a) for a in amounts]

    def f(r: float) -> float:
        return _npv_float(r, values, times)

    brackets: list[tuple[float, float]] = []
    exact: list[float] = []

    grid = _scan_grid()
    previous_r = grid[0]
    previous_v = f(previous_r)
    for current_r in grid[1:]:
        current_v = f(current_r)
        if current_v == 0.0:
            exact.append(current_r)
        elif previous_v != 0.0 and (previous_v > 0.0) != (current_v > 0.0):
            brackets.append((previous_r, current_r))
        previous_r, previous_v = current_r, current_v

    roots = [brent_root(f, lo, hi) for lo, hi in brackets] + exact
    roots.sort()

    # Two brackets either side of a touching root can converge to the same
    # place; collapse those before deciding the answer is ambiguous.
    distinct: list[float] = []
    for r in roots:
        if not distinct or abs(r - distinct[-1]) > 1e-9:
            distinct.append(r)

    if not distinct:
        raise NoSignChange(
            "the stream changes sign but no rate in the search range sets its "
            "present value to zero"
        )
    if len(distinct) > 1:
        raise AmbiguousIRR(distinct)
    return distinct[0]


def irr_periodic(amounts: Sequence[Money]) -> float:
    """Internal rate of return of evenly spaced cash flows, per period.

    The first amount is at time zero. The result is a per-period rate, so a
    quarterly stream returns a quarterly rate.
    """
    if len(amounts) < 2:
        raise NoSignChange("a single cash flow has no rate of return")
    times = [float(i) for i in range(len(amounts))]
    return _solve_rate(amounts, times)


def moic(amounts: Iterable[Money]) -> Money:
    """Multiple of invested capital: everything received over everything put in.

    Unlike a rate of return this ignores timing entirely, which is exactly why
    it is quoted alongside one rather than instead of one.
    """
    contributed = ZERO
    distributed = ZERO
    for amount in amounts:
        if amount < 0:
            contributed -= amount
        else:
            distributed += amount
    if contributed == 0:
        raise ValueError("no capital was invested, so there is no multiple on it")
    return distributed / contributed


def cagr(begin: Money, end: Money, years: Numeric) -> float:
    """Compound annual growth rate taking ``begin`` to ``end`` over ``years``."""
    y = float(money(years))
    if y <= 0:
        raise ValueError("the period must be positive")
    b, e = to_float(begin), to_float(end)
    if b <= 0:
        raise ValueError("the opening value must be positive")
    if e < 0:
        raise ValueError("the closing value must not be negative")
    return float((e / b) ** (1.0 / y) - 1.0)


# --------------------------------------------------------------------------
# Dated streams
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class CashFlow:
    """One dated movement of money.

    Sign is from the perspective of the party whose return is being measured:
    negative is money out of their pocket, positive is money coming back.
    """

    when: date
    amount: Money
    label: str = ""

    @classmethod
    def of(cls, when: date, amount: Numeric, label: str = "") -> CashFlow:
        return cls(when=when, amount=money(amount), label=label)


@dataclass(frozen=True, slots=True)
class CashFlowStream:
    """A dated series of cash flows, held in date order."""

    flows: tuple[CashFlow, ...]
    convention: DayCount = DayCount.ACT_365F

    def __post_init__(self) -> None:
        if not self.flows:
            raise ValueError("a cash-flow stream needs at least one flow")
        object.__setattr__(self, "flows", tuple(sorted(self.flows, key=lambda f: f.when)))

    @classmethod
    def of(
        cls,
        items: Iterable[tuple[date, Numeric]] | Iterable[CashFlow],
        convention: DayCount = DayCount.ACT_365F,
    ) -> CashFlowStream:
        flows: list[CashFlow] = []
        for item in items:
            if isinstance(item, CashFlow):
                flows.append(item)
            else:
                when, amount = item
                flows.append(CashFlow.of(when, amount))
        return cls(flows=tuple(flows), convention=convention)

    def __len__(self) -> int:
        return len(self.flows)

    def __iter__(self) -> Iterator[CashFlow]:
        return iter(self.flows)

    @property
    def start(self) -> date:
        return self.flows[0].when

    @property
    def end(self) -> date:
        return self.flows[-1].when

    @property
    def amounts(self) -> tuple[Money, ...]:
        return tuple(f.amount for f in self.flows)

    @property
    def total(self) -> Money:
        return sum(self.amounts, ZERO)

    def _times(self, as_of: date | None = None) -> list[float]:
        origin = as_of or self.start
        return [float(year_fraction(origin, f.when, self.convention)) for f in self.flows]

    def npv(self, rate: Numeric, *, as_of: date | None = None) -> Money:
        """Present value at ``rate``, discounted to ``as_of`` (default: the first flow).

        Computed in exact decimal rather than floating point, because this is a
        reported number rather than a solver intermediate.
        """
        r = money(rate)
        if r <= -1:
            raise ValueError("discount rate must exceed -100%")
        origin = as_of or self.start
        base = Decimal(1) + r
        total = ZERO
        for flow in self.flows:
            t = year_fraction(origin, flow.when, self.convention)
            total += flow.amount / (base**t)
        return total

    def xirr(self) -> float:
        """Annualised internal rate of return over the actual dates.

        Irregular spacing is handled by measuring each flow's time from the
        first one under the stream's day-count convention, so the rate is a
        genuine annual figure rather than a per-period one.
        """
        if len(self.flows) < 2:
            raise NoSignChange("a single cash flow has no rate of return")
        return _solve_rate(self.amounts, self._times())

    def moic(self) -> Money:
        return moic(self.amounts)

    def holding_period_years(self) -> Money:
        """Elapsed years from the first flow to the last."""
        return year_fraction(self.start, self.end, self.convention)
