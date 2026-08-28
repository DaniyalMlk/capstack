"""Capitalised financing costs, and the balance they leave behind.

Arrangement fees and original issue discount are raised at close and spent at
close, but they are not a cost of the period they were paid in. They are a cost
of borrowing the money, and borrowing the money is what the next several years
consist of. So they are capitalised — carried as a deduction from the debt
rather than as an asset, which is what a lender's economics actually describe —
and released to the income statement across the life of the paper they bought.

Three things make that more than a division.

*The two amounts belong together.* A fee of 2% on face and a placement at 98
raise the same cash and leave the same hole; presenting the fee as an asset and
the discount as a contra-liability puts one number in two places and invites a
model to amortise one and forget the other. They are one carrying-amount
adjustment here.

*The effective rate is not the coupon.* A five-year loan at 6% placed at 98 with
2% of fees does not cost 6%. It costs the rate at which the contractual cash
flows discount back to what was actually received, which is higher, and the gap
between that rate and the coupon is precisely the amortisation charge. Charging
the capitalised balance evenly instead front-loads too little in the early years,
when the balance is largest and the charge should be too.

*The profile is contractual, not actual.* The rate is solved against the
scheduled amortisation and the bullet at maturity. A cash sweep is discretionary
and cannot be part of the profile — a borrower who sweeps hard in year one has
not renegotiated the effective rate on the money it borrowed.

Nothing here is cash. The charge does not move a balance in the debt schedule
and does not shift a return by a basis point. It is carried because the balance
it leaves is what a refinancing writes off, and a write-off that is derived can
be checked where one that is typed in cannot.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from .debt import CapitalStructure, Tranche
from .money import ZERO, Money, Numeric, is_close, money, safe_div
from .returns import brent_root

__all__ = [
    "FeeMethod",
    "FeePeriod",
    "FeeSchedule",
    "TrancheFees",
    "effective_rate",
]

#: The widest per-period rate the effective-rate solve will look over. A period
#: rate of 500% is far past any paper that would be placed; the bracket exists
#: to fail loudly on a profile with no solution rather than to accommodate one.
MAXIMUM_PERIOD_RATE = 5.0

#: How close the carrying amount must come to zero at maturity for the solved
#: rate to be accepted, per unit of face.
SOLVE_TOLERANCE = "1E-9"


class FeeMethod(Enum):
    """How a capitalised balance is released over the life of the paper.

    ``EFFECTIVE_INTEREST`` is the general rule for term debt: the charge is the
    difference between interest at the effective rate on the carrying amount and
    interest at the coupon on the balance outstanding. The charge tracks the
    carrying amount rather than moving in one direction — on a bullet it rises
    across the life as the discount unwinds towards face, and on an amortising
    loan it falls, because principal comes off faster than the discount closes.
    A straight line is neither, which is the point.

    ``STRAIGHT_LINE`` spreads the balance evenly. It is the right answer for a
    revolving facility, which has no principal profile to solve a rate against —
    the fee buys a commitment for a term, and the commitment is consumed evenly
    whether or not the facility is drawn — and it is permitted for term debt
    where the difference from the effective method is immaterial.
    """

    EFFECTIVE_INTEREST = "effective_interest"
    STRAIGHT_LINE = "straight_line"

    def __str__(self) -> str:
        return self.value.replace("_", " ")


def contractual_profile(
    tranche: Tranche, periods: int, *, basis: Money | None = None
) -> tuple[list[Money], list[Money]]:
    """The balances and principal repayments the credit agreement contemplates.

    Returns the opening balance for each period and the principal repaid in it.
    Scheduled amortisation runs until the facility matures, and whatever is left
    is repaid at maturity — which is the last period of the projection where the
    tranche has no stated maturity, because a model cannot amortise paper past
    the end of its own grid.

    The sweep is deliberately absent. It is discretionary, and a profile that
    included it would solve a different effective rate for every operating case
    the same loan was run against.
    """
    if periods <= 0:
        raise ValueError("a profile needs at least one period")
    face = tranche.face if basis is None else basis
    last = periods if tranche.maturity is None else min(tranche.maturity - 1, periods)
    opening: list[Money] = []
    principal: list[Money] = []
    balance = face
    for index in range(periods):
        opening.append(balance)
        if index >= last:
            principal.append(ZERO)
            continue
        due = min(tranche.scheduled_amortisation(index, face), balance)
        if index == last - 1:
            due = balance
        principal.append(due)
        balance -= due
    return opening, principal


def effective_rate(
    proceeds: Money,
    opening: Sequence[Money],
    principal: Sequence[Money],
    coupon: Money,
) -> Money:
    """The per-period rate at which the contractual flows discount to ``proceeds``.

    Solved rather than derived, because there is no closed form once principal
    repays on a schedule. The function is monotone in the rate over any interval
    where the flows are all outward — every flow is discounted harder as the
    rate rises — so a bracket and a root find are enough, and the bracket runs
    from zero to a rate no instrument would ever carry.

    ``coupon`` is the contractual rate per period, so a 6% annual coupon on a
    quarterly grid is 0.015 here. The caller does that conversion because only
    the caller knows the day count the interest was actually accrued on.
    """
    if not opening:
        raise ValueError("an effective rate needs at least one period")
    if len(opening) != len(principal):
        raise ValueError("one principal repayment per period")
    if proceeds <= 0:
        raise ValueError("paper that raised nothing has no effective rate")

    flows = [float(o * coupon + p) for o, p in zip(opening, principal)]
    target = float(proceeds)

    def carrying_at_maturity(rate: float) -> float:
        balance = target
        for flow in flows:
            balance = balance * (1.0 + rate) - flow
        return balance

    # At a rate of zero the flows total more than the proceeds whenever any
    # interest is charged at all, so the balance ends negative; at a very high
    # rate it ends positive. That sign change is the bracket.
    low = carrying_at_maturity(0.0)
    if abs(low) < float(Decimal(SOLVE_TOLERANCE)) * target:
        return ZERO
    high = carrying_at_maturity(MAXIMUM_PERIOD_RATE)
    if low * high > 0:
        raise ValueError(
            "no effective rate reconciles these flows to the proceeds; the paper "
            "as described repays less than it raised"
        )
    return money(str(brent_root(carrying_at_maturity, 0.0, MAXIMUM_PERIOD_RATE)))


@dataclass(frozen=True, slots=True)
class FeePeriod:
    """One period of one tranche's capitalised balance."""

    index: int
    opening: Money
    charge: Money
    closing: Money

    def reconciles(self, tolerance: Numeric = "1E-9") -> bool:
        return is_close(self.closing, self.opening - self.charge, tolerance=tolerance)


@dataclass(frozen=True, slots=True)
class TrancheFees:
    """What one tranche capitalised, and how it is released."""

    name: str
    capitalised: Money
    method: FeeMethod
    effective_rate: Money
    coupon: Money
    periods: tuple[FeePeriod, ...]

    def __len__(self) -> int:
        return len(self.periods)

    def __iter__(self) -> Iterator[FeePeriod]:
        return iter(self.periods)

    def unamortised_at(self, index: int) -> Money:
        """The balance left at the end of period ``index``, zero-based.

        Reading past the end returns zero rather than raising: paper that has
        matured has nothing left to write off, and a caller asking about a
        period beyond the grid is asking a question with an obvious answer.
        """
        if index < 0:
            raise IndexError("period index must not be negative")
        if not self.periods:
            return ZERO
        if index >= len(self.periods):
            return self.periods[-1].closing
        return self.periods[index].closing

    @property
    def total_charged(self) -> Money:
        return sum((p.charge for p in self.periods), ZERO)

    @property
    def rate_uplift(self) -> Money:
        """How much the capitalised cost adds to the cost of the money.

        The number worth quoting next to a coupon. A loan at 6% placed at 98
        with 2% of fees is not a 6% loan, and the difference is what the
        borrower is actually paying for having borrowed.
        """
        return self.effective_rate - self.coupon


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    """Capitalised financing costs across the whole structure."""

    tranches: tuple[TrancheFees, ...]

    def __len__(self) -> int:
        return len(self.tranches)

    def __iter__(self) -> Iterator[TrancheFees]:
        return iter(self.tranches)

    def tranche(self, name: str) -> TrancheFees:
        for row in self.tranches:
            if row.name == name:
                return row
        raise KeyError(f"no capitalised balance for a tranche named {name!r}")

    def unamortised_at(self, name: str, index: int) -> Money:
        """What is left on ``name`` at the end of period ``index``.

        Zero for a tranche that capitalised nothing, rather than a lookup
        failure: a structure can carry paper placed at par with no arrangement
        fee, and asking what it has left to write off is a fair question with a
        zero answer.
        """
        for row in self.tranches:
            if row.name == name:
                return row.unamortised_at(index)
        return ZERO

    @property
    def total_capitalised(self) -> Money:
        return sum((t.capitalised for t in self.tranches), ZERO)

    def charge_in(self, index: int) -> Money:
        """The non-cash charge across the structure in period ``index``."""
        return sum(
            (
                t.periods[index].charge
                for t in self.tranches
                if index < len(t.periods)
            ),
            ZERO,
        )

    @property
    def total_charged(self) -> Money:
        return sum((t.total_charged for t in self.tranches), ZERO)

    @property
    def unreleased(self) -> Money:
        """What is still capitalised at the end of the projection.

        Non-zero whenever paper outlives the model, which is the normal case: a
        seven-year loan on a five-year hold has two years of balance left, and
        it is written off at the exit by whoever repays the loan.
        """
        return sum((t.unamortised_at(len(t) - 1) for t in self.tranches if len(t)), ZERO)

    @classmethod
    def build(
        cls,
        structure: CapitalStructure,
        capitalised: dict[str, Money],
        periods: int,
        *,
        periods_per_year: int = 1,
        base_rate: Money | None = None,
        method: FeeMethod = FeeMethod.EFFECTIVE_INTEREST,
    ) -> FeeSchedule:
        """Amortise each tranche's capitalised balance across ``periods``.

        ``capitalised`` is the arrangement fee plus the original issue discount,
        by tranche name. A tranche absent from it, or present with nothing,
        carries no balance and no charge.

        ``base_rate`` prices the floating tranches. It is a single rate rather
        than a curve on purpose: the effective rate is a property of the paper
        as it was placed, and re-solving it every period as the forward curve
        moves would restate a historic cost with information that arrived later.

        A revolving facility is always released straight-line whatever
        ``method`` says. There is no principal profile to solve a rate against —
        the fee buys a commitment for a term, and the term is what gets
        consumed.
        """
        if periods <= 0:
            raise ValueError("a fee schedule needs at least one period")
        if periods_per_year <= 0:
            raise ValueError("a year holds at least one period")
        for name in capitalised:
            if not any(t.name == name for t in structure):
                raise KeyError(
                    f"there is no tranche named {name!r} to capitalise anything against"
                )

        rows: list[TrancheFees] = []
        for tranche in structure:
            amount = capitalised.get(tranche.name, ZERO)
            if amount < 0:
                raise ValueError(
                    f"{tranche.name}: a negative capitalised cost is a premium, and "
                    f"paper placed above par is not modelled here"
                )
            rows.append(
                _amortise(
                    tranche,
                    amount,
                    periods,
                    periods_per_year=periods_per_year,
                    base_rate=base_rate,
                    method=method,
                )
            )
        return cls(tranches=tuple(rows))


def _straight_line(amount: Money, life: int, periods: int) -> list[FeePeriod]:
    """Release ``amount`` evenly over ``life`` periods, with the last squaring it.

    The last period takes the rounding rather than spreading it, so the balance
    reaches exactly zero instead of a residual that a reader has to ignore.
    """
    rows: list[FeePeriod] = []
    balance = amount
    per_period = safe_div(amount, money(life), default=ZERO)
    for index in range(periods):
        if index >= life or balance <= 0:
            rows.append(FeePeriod(index=index, opening=balance, charge=ZERO, closing=balance))
            continue
        charge = balance if index == life - 1 else min(per_period, balance)
        rows.append(
            FeePeriod(index=index, opening=balance, charge=charge, closing=balance - charge)
        )
        balance -= charge
    return rows


def _amortise(
    tranche: Tranche,
    amount: Money,
    periods: int,
    *,
    periods_per_year: int,
    base_rate: Money | None,
    method: FeeMethod,
) -> TrancheFees:
    """Build one tranche's release schedule."""
    coupon_annual = tranche.rate_at(base_rate if base_rate is not None else ZERO)
    coupon = coupon_annual / money(periods_per_year)
    life = periods if tranche.maturity is None else min(tranche.maturity - 1, periods)
    life = max(life, 1)

    if amount == 0:
        return TrancheFees(
            name=tranche.name,
            capitalised=ZERO,
            method=method,
            effective_rate=coupon,
            coupon=coupon,
            periods=tuple(
                FeePeriod(index=i, opening=ZERO, charge=ZERO, closing=ZERO)
                for i in range(periods)
            ),
        )

    # A revolver has no principal profile, and a tranche placed with nothing
    # drawn has no proceeds to solve a rate against. Both fall back to the
    # straight line, which is what a fee on a commitment describes anyway.
    if (
        method is FeeMethod.STRAIGHT_LINE
        or tranche.is_revolving
        or tranche.face <= 0
        or tranche.face <= amount
    ):
        even = _straight_line(amount, life, periods)
        return TrancheFees(
            name=tranche.name,
            capitalised=amount,
            method=FeeMethod.STRAIGHT_LINE,
            effective_rate=coupon,
            coupon=coupon,
            periods=tuple(even),
        )

    opening, principal = contractual_profile(tranche, life)
    proceeds = tranche.face - amount
    rate = effective_rate(proceeds, opening, principal, coupon)

    rows: list[FeePeriod] = []
    carrying = proceeds
    balance = amount
    for index in range(periods):
        if index >= life or balance <= 0:
            rows.append(FeePeriod(index=index, opening=balance, charge=ZERO, closing=balance))
            continue
        effective_interest = carrying * rate
        contractual_interest = opening[index] * coupon
        charge = effective_interest - contractual_interest
        # The last period squares the balance. The solve is to a tolerance, not
        # to the last digit, and a residual left on a balance that the paper
        # has finished with would be reported as something still capitalised.
        if index == life - 1:
            charge = balance
        charge = min(max(charge, ZERO), balance)
        rows.append(
            FeePeriod(index=index, opening=balance, charge=charge, closing=balance - charge)
        )
        balance -= charge
        carrying += charge
        carrying -= principal[index]

    return TrancheFees(
        name=tranche.name,
        capitalised=amount,
        method=FeeMethod.EFFECTIVE_INTEREST,
        effective_rate=rate,
        coupon=coupon,
        periods=tuple(rows),
    )
