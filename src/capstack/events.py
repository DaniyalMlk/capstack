"""Things that happen between buying a business and selling it.

Every layer below this one models a deal done once, held flat and sold once. That
shape makes the equity a two-point cash-flow stream — money out at close, money
back at exit — which is why none of the return machinery's harder cases have ever
had to fire for the sponsor's own paper.

A dividend recapitalisation breaks the shape in the way that matters most. New
debt is raised part-way through the hold and the proceeds go straight out to the
shareholders. Nothing about the business changes: the same earnings, the same
multiple, the same buyer at the end. The sponsor simply has some of its money
back three years early, and money three years early is worth more than the same
money at exit.

That is the whole trade, and it is worth being precise about which measure sees
it. The money multiple barely moves — the sponsor is paid the same total, give or
take the interest the new debt costs. The rate of return moves a lot, because it
is the only measure that knows when anything happened. A model that reports one
without the other reports either a deal that did nothing or a deal that created
value out of nowhere, and neither is what happened.

The cost is real and it is carried by the schedule rather than stated here: the
new debt accrues from the following period, is swept and amortised like anything
else, and shows up in the leverage the covenants test. A recapitalisation that
looks free is one whose interest has not been modelled.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from .money import ONE, ZERO, Money, Numeric, money

__all__ = [
    "Draw",
    "Recapitalisation",
    "RecapitalisationError",
    "RecapitalisationOutcome",
]


class RecapitalisationError(ValueError):
    """The recapitalisation as described cannot be funded or cannot be applied."""


@dataclass(frozen=True, slots=True)
class Draw:
    """Incremental face taken down on one tranche.

    Priced on its own terms rather than the tranche's original ones, because it
    is a separate raise: an add-on to a term loan two years into a hold is
    marketed at whatever the market is paying then, and the discount it clears at
    is not the discount the original clearing price implied.

    The margin it carries, though, *is* the tranche's. Modelling a blended rate
    across an original and an incremental tranche needs the two to be separate
    facilities in the file, which is the honest way to describe them when the
    pricing genuinely differs.
    """

    tranche: str
    amount: Money
    issue_price: Money = ONE
    financing_fee_rate: Money = ZERO

    @classmethod
    def of(
        cls,
        tranche: str,
        amount: Numeric,
        *,
        issue_price: Numeric = 1,
        financing_fee_rate: Numeric = 0,
    ) -> Draw:
        return cls(
            tranche=tranche,
            amount=money(amount),
            issue_price=money(issue_price),
            financing_fee_rate=money(financing_fee_rate),
        )

    def __post_init__(self) -> None:
        if not self.tranche.strip():
            raise RecapitalisationError("a draw has to name the tranche it is taken on")
        if self.amount <= 0:
            raise RecapitalisationError(
                f"{self.tranche}: a draw of {self.amount} raises nothing"
            )
        if not (0 < self.issue_price <= 2):
            raise RecapitalisationError(
                f"{self.tranche}: an issue price of {self.issue_price} is not a price"
            )
        if not (0 <= self.financing_fee_rate < 1):
            raise RecapitalisationError(
                f"{self.tranche}: a financing fee of {self.financing_fee_rate} would "
                f"cost more than the draw raises"
            )

    @property
    def gross_proceeds(self) -> Money:
        """What the paper sells for, before the cost of selling it."""
        return self.amount * self.issue_price

    @property
    def fees(self) -> Money:
        """Charged on face, which is how a financing fee is quoted."""
        return self.amount * self.financing_fee_rate

    @property
    def net_proceeds(self) -> Money:
        return self.gross_proceeds - self.fees

    @property
    def discount(self) -> Money:
        """Original issue discount: face given up to clear the paper."""
        return self.amount - self.gross_proceeds


@dataclass(frozen=True, slots=True)
class Recapitalisation:
    """New debt raised at a period boundary, paid straight out to the equity.

    ``period`` is the one at whose *end* the raise happens, numbered from one.
    Placing it at a boundary rather than at an arbitrary date is a real
    simplification and worth naming: a recapitalisation that closes in March is
    modelled here as closing at the period end, so it carries a period's less
    interest than it would in life. The alternative is a mid-period stub for
    every event, which buys precision the rest of the annual model cannot use.

    ``from_cash`` is balance-sheet cash paid out alongside the new proceeds. It
    is separate because the two behave differently: proceeds never touch the
    balance sheet, while cash taken off it has to still leave the structure with
    the minimum it is required to hold.
    """

    period: int
    draws: tuple[Draw, ...] = ()
    from_cash: Money = ZERO
    label: str = "Dividend recapitalisation"

    @classmethod
    def of(
        cls,
        period: int,
        draws: Sequence[Draw] = (),
        *,
        from_cash: Numeric = 0,
        label: str = "Dividend recapitalisation",
    ) -> Recapitalisation:
        return cls(
            period=int(period),
            draws=tuple(draws),
            from_cash=money(from_cash),
            label=label,
        )

    def __post_init__(self) -> None:
        if self.period < 1:
            raise RecapitalisationError(
                f"periods are numbered from one, so period {self.period} is not one "
                f"of them"
            )
        if not self.label.strip():
            raise RecapitalisationError("a recapitalisation needs a label")
        if self.from_cash < 0:
            raise RecapitalisationError(
                "a recapitalisation pays cash out; a negative amount is a contribution"
            )
        names = [d.tranche for d in self.draws]
        if len(names) != len(set(names)):
            raise RecapitalisationError(
                "each tranche can be drawn once per recapitalisation; combine the "
                "amounts rather than listing the tranche twice"
            )
        if not self.draws and self.from_cash == 0:
            raise RecapitalisationError(
                "this recapitalisation raises nothing and pays nothing out"
            )

    def __len__(self) -> int:
        return len(self.draws)

    def __iter__(self) -> Iterator[Draw]:
        return iter(self.draws)

    @property
    def face(self) -> Money:
        """Total incremental face, which is what the leverage will carry."""
        return sum((d.amount for d in self.draws), ZERO)

    @property
    def fees(self) -> Money:
        return sum((d.fees for d in self.draws), ZERO)

    @property
    def discount(self) -> Money:
        return sum((d.discount for d in self.draws), ZERO)

    @property
    def net_proceeds(self) -> Money:
        return sum((d.net_proceeds for d in self.draws), ZERO)

    @property
    def distribution(self) -> Money:
        """What reaches the shareholders: the net raise plus the cash taken off.

        The cost of raising is borne here rather than left on the balance sheet.
        A recapitalisation that raised 300 of face at 99 with 2% of fees pays out
        291, and the 9 of difference is the reason a recapitalisation is not free
        even before a penny of interest accrues on it.
        """
        return self.net_proceeds + self.from_cash

    def draw_on(self, tranche: str) -> Money:
        for draw in self.draws:
            if draw.tranche == tranche:
                return draw.amount
        return ZERO


@dataclass(frozen=True, slots=True)
class RecapitalisationOutcome:
    """A recapitalisation as it actually landed in the schedule."""

    event: Recapitalisation
    index: int
    cash_before: Money
    cash_after: Money
    leverage_before: Money | None = None
    leverage_after: Money | None = None

    @property
    def label(self) -> str:
        return self.event.label

    @property
    def distribution(self) -> Money:
        return self.event.distribution

    @property
    def face(self) -> Money:
        return self.event.face

    @property
    def cost_of_raising(self) -> Money:
        """Fees and issue discount: the part of the face that never reaches anyone."""
        return self.event.fees + self.event.discount

    @property
    def turns_added(self) -> Money | None:
        """How much leverage the raise put back on, in turns of EBITDA."""
        if self.leverage_before is None or self.leverage_after is None:
            return None
        return self.leverage_after - self.leverage_before
