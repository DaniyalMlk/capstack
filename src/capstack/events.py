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

An add-on acquisition breaks the shape in the other direction. A
recapitalisation changes who holds the claims on a fixed business; an add-on
changes the business. Earnings arrive that were not underwritten, paid for at a
multiple that is usually below the platform's, and the whole argument for
buy-and-build is that difference: buy at seven, own inside something the market
prices at eleven, and the arbitrage is booked the moment the transaction closes
rather than earned over the hold.

Which is why the number the strategy lives or dies by is the blended entry
multiple — every pound of capital deployed into the business, over every turn of
EBITDA that capital bought. A platform bought at 9.75x that adds three tuck-ins
at 6.5x has an entry multiple somewhere in between, and it is that number, not
the headline one, that the exit multiple has to be compared against. Reporting
the platform multiple alone flatters the deal by exactly the arbitrage.

Two things keep the arithmetic honest here. Synergies are held out of the
unsynergised multiple, because a multiple struck on earnings that do not exist
yet is a forecast wearing the clothes of a fact; both are reported. And fees are
held out of the multiple but not out of the capital deployed, because a multiple
is quoted on enterprise value while the cheque is written for rather more than
that.

A refinancing changes neither the business nor who holds the equity. It changes
the price of money already borrowed, for whatever part of the hold remains, and
it is one of the few things a sponsor still controls after signing. The trade is
a cash cost now — a call premium, and the fees on the new paper — against a
lower coupon later, and whether it clears is arithmetic anybody can check.

The write-off of unamortised financing fees is the part most often reported
wrongly. It is not cash. The fees on the original paper were capitalised at
close and written down over its life; taking that paper out early charges
whatever is left in one go. No balance in this model moves and no return
changes. It is reported anyway, and reported separately from the cash cost,
because it is a real charge in a real set of accounts and because netting it
into a cash figure is precisely the error that separation prevents.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from .drivers import Driver
from .money import ONE, ZERO, Money, Numeric, money, safe_div
from .operating import AcquiredStream

__all__ = [
    "AddOn",
    "AddOnError",
    "AddOnOutcome",
    "BlendedEntry",
    "Draw",
    "Recapitalisation",
    "RecapitalisationError",
    "RecapitalisationOutcome",
    "Refinancing",
    "RefinancingError",
    "RefinancingOutcome",
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


class AddOnError(ValueError):
    """The acquisition as described cannot be funded or cannot be applied."""


@dataclass(frozen=True, slots=True)
class AddOn:
    """A business bought during the hold, at a period boundary.

    Priced cash-free and debt-free, which is how a mid-market bolt-on is
    actually bought and which removes an entire class of ambiguity from the
    funding table: the enterprise value is what leaves the structure, because
    whatever debt the target carried is repaid out of the same cheque.

    ``period`` is the one at whose end the acquisition closes, numbered from
    one, matching :class:`Recapitalisation`. It has to fall strictly inside the
    projection. An acquisition closing at the end of the final period would pay
    cash for earnings that no period ever records, and would then be sold at a
    multiple applied to a figure it never reached — a deal that destroys its own
    purchase price and reports nothing.

    Funding is a plug against the balance sheet rather than a table that has to
    be made to balance by hand. The draws raise what they raise; whatever is
    left of the uses comes off the cash the business is holding, and if that
    would breach the minimum the structure is required to keep, the acquisition
    is refused rather than silently shrunk. Draws that raise more than the uses
    leave the surplus on the balance sheet, which is what over-funding a
    facility does.
    """

    period: int
    ebitda: Money
    multiple: Money
    revenue: Money | None = None
    synergies: Money = ZERO
    synergy_phase_in: int = 1
    growth: Driver | None = None
    fee_rate: Money = ZERO
    integration_cost: Money = ZERO
    draws: tuple[Draw, ...] = ()
    label: str = "Add-on acquisition"

    @classmethod
    def of(
        cls,
        period: int,
        *,
        ebitda: Numeric,
        multiple: Numeric,
        revenue: Numeric | None = None,
        synergies: Numeric = 0,
        synergy_phase_in: int = 1,
        growth: Driver | None = None,
        fee_rate: Numeric = 0,
        integration_cost: Numeric = 0,
        draws: Sequence[Draw] = (),
        label: str = "Add-on acquisition",
    ) -> AddOn:
        return cls(
            period=int(period),
            ebitda=money(ebitda),
            multiple=money(multiple),
            revenue=None if revenue is None else money(revenue),
            synergies=money(synergies),
            synergy_phase_in=int(synergy_phase_in),
            growth=growth,
            fee_rate=money(fee_rate),
            integration_cost=money(integration_cost),
            draws=tuple(draws),
            label=label,
        )

    def __post_init__(self) -> None:
        if self.period < 1:
            raise AddOnError(
                f"periods are numbered from one, so period {self.period} is not one "
                f"of them"
            )
        if not self.label.strip():
            raise AddOnError("an acquisition needs a label")
        if self.ebitda <= 0:
            raise AddOnError(
                f"{self.label}: an acquisition of a business earning {self.ebitda} is "
                f"a purchase of losses; model it as a cost, not a multiple"
            )
        if self.multiple <= 0:
            raise AddOnError(
                f"{self.label}: a multiple of {self.multiple} is not a price"
            )
        if self.revenue is not None and self.revenue < self.ebitda:
            raise AddOnError(
                f"{self.label}: revenue of {self.revenue} against EBITDA of "
                f"{self.ebitda} is a margin above one"
            )
        if self.synergies < 0:
            raise AddOnError(
                f"{self.label}: a negative synergy is a dis-synergy, which belongs in "
                f"the earnings the business is bought on"
            )
        if self.synergy_phase_in < 1:
            raise AddOnError(
                f"{self.label}: synergies phase in over at least one period"
            )
        if not (0 <= self.fee_rate < 1):
            raise AddOnError(
                f"{self.label}: transaction fees of {self.fee_rate} of enterprise "
                f"value are not a fee rate"
            )
        if self.integration_cost < 0:
            raise AddOnError(
                f"{self.label}: a negative integration cost is a receipt, not a cost"
            )
        names = [d.tranche for d in self.draws]
        if len(names) != len(set(names)):
            raise AddOnError(
                "each tranche can be drawn once per acquisition; combine the amounts "
                "rather than listing the tranche twice"
            )

    def __len__(self) -> int:
        return len(self.draws)

    def __iter__(self) -> Iterator[Draw]:
        return iter(self.draws)

    @property
    def enterprise_value(self) -> Money:
        """What the business is bought for, before the cost of buying it."""
        return self.ebitda * self.multiple

    @property
    def fees(self) -> Money:
        """Advisory and financing-adjacent fees, quoted on enterprise value."""
        return self.enterprise_value * self.fee_rate

    @property
    def uses(self) -> Money:
        """Everything the acquisition has to fund.

        Integration cost sits here rather than in the operating case. It is a
        one-off, it is known at signing, and charging it to EBITDA would put it
        through the exit multiple — valuing a cost that will never recur at nine
        times itself.
        """
        return self.enterprise_value + self.fees + self.integration_cost

    @property
    def face(self) -> Money:
        """Incremental face taken down to fund it, which the leverage carries."""
        return sum((d.amount for d in self.draws), ZERO)

    @property
    def financing_fees(self) -> Money:
        return sum((d.fees for d in self.draws), ZERO)

    @property
    def discount(self) -> Money:
        return sum((d.discount for d in self.draws), ZERO)

    @property
    def debt_proceeds(self) -> Money:
        """What the draws deliver, after the discount and the fees on them."""
        return sum((d.net_proceeds for d in self.draws), ZERO)

    @property
    def from_cash(self) -> Money:
        """The plug: uses not met by the new debt come off the balance sheet.

        Negative when the draws over-fund, in which case the surplus is left as
        cash rather than being handed back — a facility drawn for more than the
        purchase price leaves the money in the account.
        """
        return self.uses - self.debt_proceeds

    @property
    def total_cost(self) -> Money:
        """Capital deployed: the price, plus everything paid to pay the price."""
        return self.uses + self.financing_fees + self.discount

    @property
    def implied_margin(self) -> Money | None:
        """The margin the business is bought on, where revenue was stated."""
        if self.revenue is None:
            return None
        return safe_div(self.ebitda, self.revenue, default=ZERO)

    @property
    def synergised_ebitda(self) -> Money:
        return self.ebitda + self.synergies

    @property
    def synergised_multiple(self) -> Money:
        """What the business costs per turn of earnings once synergies land.

        The number a buyer defends the price with, and the number to be most
        careful about: it is a multiple of an outcome rather than of a fact.
        """
        return safe_div(self.enterprise_value, self.synergised_ebitda, default=ZERO)

    def draw_on(self, tranche: str) -> Money:
        for draw in self.draws:
            if draw.tranche == tranche:
                return draw.amount
        return ZERO

    def stream(self, platform_margin: Money) -> AcquiredStream:
        """The earnings this acquisition puts into the operating case.

        ``platform_margin`` is used only when the file did not state the
        acquired revenue. Carrying the business at the platform's margin is the
        assumption that changes the model least — the earnings are what was
        bought and the revenue behind them is a detail — but it is an assumption,
        and stating revenue in the file replaces it with a fact.
        """
        if self.revenue is not None:
            revenue, margin = self.revenue, self.ebitda / self.revenue
        else:
            if platform_margin <= 0:
                raise AddOnError(
                    f"{self.label}: no revenue was stated and the platform earns "
                    f"nothing on its own revenue in period {self.period}, so there is "
                    f"no margin to carry the acquisition at"
                )
            revenue, margin = self.ebitda / platform_margin, platform_margin
        return AcquiredStream(
            period=self.period,
            revenue=revenue,
            margin=margin,
            synergies=self.synergies,
            synergy_phase_in=self.synergy_phase_in,
            growth=self.growth,
            label=self.label,
        )


@dataclass(frozen=True, slots=True)
class AddOnOutcome:
    """An acquisition as it actually landed in the schedule."""

    event: AddOn
    index: int
    cash_before: Money
    cash_after: Money
    leverage_before: Money | None = None
    leverage_after: Money | None = None

    @property
    def label(self) -> str:
        return self.event.label

    @property
    def enterprise_value(self) -> Money:
        return self.event.enterprise_value

    @property
    def face(self) -> Money:
        return self.event.face

    @property
    def from_cash(self) -> Money:
        return self.event.from_cash

    @property
    def debt_funded_share(self) -> Money:
        """How much of the price the new debt carried, as a share of the uses."""
        return safe_div(self.event.debt_proceeds, self.event.uses, default=ZERO)

    @property
    def turns_added(self) -> Money | None:
        """The leverage effect: new debt against newly acquired earnings.

        An acquisition funded entirely with debt at a multiple below the
        structure's own leverage is deleveraging, which is the mechanism behind
        a buy-and-build that stays inside its covenants while doubling in size.
        """
        if self.leverage_before is None or self.leverage_after is None:
            return None
        return self.leverage_after - self.leverage_before


@dataclass(frozen=True, slots=True)
class BlendedEntry:
    """The platform and everything bought after it, priced as one entry.

    Deliberately built from the events rather than from the schedule, so the
    blended multiple can be quoted from a deal file before anything has been
    run. Nothing here depends on how the acquisitions were funded — the multiple
    is a statement about price, and price does not change because the money came
    from a revolver.
    """

    platform_enterprise_value: Money
    platform_ebitda: Money
    add_ons: tuple[AddOn, ...] = ()

    def __post_init__(self) -> None:
        if self.platform_ebitda <= 0:
            raise AddOnError(
                "a blended multiple needs the earnings the platform was priced on"
            )

    def __len__(self) -> int:
        return len(self.add_ons)

    def __iter__(self) -> Iterator[AddOn]:
        return iter(self.add_ons)

    @property
    def acquired_enterprise_value(self) -> Money:
        return sum((a.enterprise_value for a in self.add_ons), ZERO)

    @property
    def acquired_ebitda(self) -> Money:
        """Run-rate earnings bought, before synergies."""
        return sum((a.ebitda for a in self.add_ons), ZERO)

    @property
    def synergies(self) -> Money:
        return sum((a.synergies for a in self.add_ons), ZERO)

    @property
    def enterprise_value(self) -> Money:
        return self.platform_enterprise_value + self.acquired_enterprise_value

    @property
    def ebitda(self) -> Money:
        return self.platform_ebitda + self.acquired_ebitda

    @property
    def capital_deployed(self) -> Money:
        """Every pound put into the business, fees and discount included.

        Larger than the enterprise value, and the gap is the cost of doing five
        transactions instead of one. A buy-and-build that adds a turn of
        arbitrage per deal and spends most of it on advisers has a strategy on
        paper only, and this is the line that shows it.
        """
        return self.platform_enterprise_value + sum(
            (a.total_cost for a in self.add_ons), ZERO
        )

    @property
    def platform_multiple(self) -> Money:
        return safe_div(
            self.platform_enterprise_value, self.platform_ebitda, default=ZERO
        )

    @property
    def blended_multiple(self) -> Money:
        """Enterprise value over earnings, platform and add-ons together."""
        return safe_div(self.enterprise_value, self.ebitda, default=ZERO)

    @property
    def synergised_multiple(self) -> Money:
        """The same, crediting the synergies the add-ons were bought for."""
        return safe_div(self.enterprise_value, self.ebitda + self.synergies, default=ZERO)

    @property
    def all_in_multiple(self) -> Money:
        """Capital deployed over earnings bought: what it really cost per turn."""
        return safe_div(self.capital_deployed, self.ebitda, default=ZERO)

    @property
    def arbitrage(self) -> Money:
        """Turns taken off the entry multiple by buying below it.

        Positive when the add-ons were bought cheaper than the platform, which
        is the only reason to do them at this scale. Reported in turns because
        that is the unit the exit multiple is argued in.
        """
        return self.platform_multiple - self.blended_multiple

    @property
    def acquired_share(self) -> Money:
        """Share of the combined entry earnings that was bought rather than built."""
        return safe_div(self.acquired_ebitda, self.ebitda, default=ZERO)


class RefinancingError(ValueError):
    """The takeout as described cannot be funded or cannot be applied."""


@dataclass(frozen=True, slots=True)
class Refinancing:
    """An existing facility repaid in full and replaced with new paper.

    The event a sponsor reaches for when the market improves. Nothing about the
    business changes and nothing reaches the shareholders; what changes is the
    coupon on the money already borrowed, for the periods that remain.

    ``into`` may draw on the tranche being taken out, which is a repricing of
    the same facility, or on a different one, which is a genuine refinancing
    into new paper. Both are the same arithmetic: the old balance goes to zero
    and the new draws land, in that order, so a same-tranche takeout nets to the
    difference rather than doubling the balance for an instant.

    Two costs make a refinancing something other than free, and they behave
    differently enough that reporting one without the other is misleading.

    The *call premium* is cash. Non-call protection is what the original lenders
    were paid for taking the risk that this would happen, and it is charged on
    the balance repaid at the moment it is repaid.

    The *unamortised financing fees* are not cash. The fees on the original
    paper were capitalised at close and written down over its life; taking the
    paper out early writes off whatever is left in a single charge. Nothing
    moves in the bank account, no balance in this model changes, and the return
    does not shift by a basis point — but it is a real charge in a real set of
    accounts, and a model that never mentions it leaves its reader to discover
    the number from somebody else.
    """

    period: int
    tranche: str
    into: tuple[Draw, ...] = ()
    call_premium_rate: Money = ZERO
    unamortised_fees: Money | None = None
    label: str = "Refinancing"

    @classmethod
    def of(
        cls,
        period: int,
        tranche: str,
        into: Sequence[Draw] = (),
        *,
        call_premium_rate: Numeric = 0,
        unamortised_fees: Numeric | None = None,
        label: str = "Refinancing",
    ) -> Refinancing:
        return cls(
            period=int(period),
            tranche=tranche,
            into=tuple(into),
            call_premium_rate=money(call_premium_rate),
            unamortised_fees=(
                None if unamortised_fees is None else money(unamortised_fees)
            ),
            label=label,
        )

    def __post_init__(self) -> None:
        if self.period < 1:
            raise RefinancingError(
                f"periods are numbered from one, so period {self.period} is not one "
                f"of them"
            )
        if not self.tranche.strip():
            raise RefinancingError("a refinancing has to name the facility it takes out")
        if not self.label.strip():
            raise RefinancingError("a refinancing needs a label")
        if not (0 <= self.call_premium_rate < 1):
            raise RefinancingError(
                f"{self.label}: a call premium of {self.call_premium_rate} of the "
                f"balance is not a premium"
            )
        if self.unamortised_fees is not None and self.unamortised_fees < 0:
            raise RefinancingError(
                f"{self.label}: a negative write-off is a credit, not a cost"
            )
        names = [d.tranche for d in self.into]
        if len(names) != len(set(names)):
            raise RefinancingError(
                "each tranche can be drawn once per refinancing; combine the amounts "
                "rather than listing the tranche twice"
            )

    def __len__(self) -> int:
        return len(self.into)

    def __iter__(self) -> Iterator[Draw]:
        return iter(self.into)

    @property
    def states_its_write_off(self) -> bool:
        """Whether the file put a figure on the unamortised balance.

        A deal file may state it, and an older one always did. Where it does
        not, the balance is derived from the capitalised costs the paper was
        placed with and how long it had run — which is the reading a reader can
        check, because it comes from the funding table rather than from an
        assertion.
        """
        return self.unamortised_fees is not None

    def write_off(self, derived: Money = ZERO) -> Money:
        """The unamortised balance charged off, stated or derived."""
        return derived if self.unamortised_fees is None else self.unamortised_fees

    @property
    def face(self) -> Money:
        """Face of the new paper, which is what the leverage will carry."""
        return sum((d.amount for d in self.into), ZERO)

    @property
    def financing_fees(self) -> Money:
        return sum((d.fees for d in self.into), ZERO)

    @property
    def discount(self) -> Money:
        return sum((d.discount for d in self.into), ZERO)

    @property
    def proceeds(self) -> Money:
        """What the new paper delivers, after the discount and the fees on it."""
        return sum((d.net_proceeds for d in self.into), ZERO)

    def premium_on(self, balance: Money) -> Money:
        return balance * self.call_premium_rate

    def uses(self, balance: Money) -> Money:
        """The balance being retired, plus what it costs to retire it early."""
        return balance + self.premium_on(balance)

    def from_cash(self, balance: Money) -> Money:
        """The plug, on the same terms as every other event in this module.

        Negative when the new paper raises more than the old balance and its
        premium, which is an upsizing rather than a refinancing and leaves the
        surplus on the balance sheet.
        """
        return self.uses(balance) - self.proceeds

    def draw_on(self, tranche: str) -> Money:
        for draw in self.into:
            if draw.tranche == tranche:
                return draw.amount
        return ZERO


@dataclass(frozen=True, slots=True)
class RefinancingOutcome:
    """A refinancing as it actually landed in the schedule."""

    event: Refinancing
    index: int
    repaid: Money
    cash_before: Money
    cash_after: Money
    old_rate: Money = ZERO
    new_rate: Money = ZERO
    periods_remaining: int = 0
    #: The unamortised balance actually charged off, whether the file stated it
    #: or the capitalised-cost schedule derived it.
    written_off: Money = ZERO

    @property
    def label(self) -> str:
        return self.event.label

    @property
    def write_off_was_derived(self) -> bool:
        """Whether the charge came from the schedule rather than from the file."""
        return not self.event.states_its_write_off

    @property
    def call_premium(self) -> Money:
        return self.event.premium_on(self.repaid)

    @property
    def face(self) -> Money:
        return self.event.face

    @property
    def from_cash(self) -> Money:
        return self.event.from_cash(self.repaid)

    @property
    def fees_written_off(self) -> Money:
        return self.written_off

    @property
    def cash_cost(self) -> Money:
        """What the exercise takes out of the business.

        The premium, the fees on the new paper and the discount it clears at.
        The write-off is deliberately not here: it is a charge against reported
        earnings and nothing else, and adding it to a cash cost would be the
        error this separation exists to prevent.
        """
        return self.call_premium + self.event.financing_fees + self.event.discount

    @property
    def spread_saved(self) -> Money:
        """Coupon given up, in rate terms. Negative if the new paper is dearer."""
        return self.old_rate - self.new_rate

    @property
    def annual_saving(self) -> Money:
        """Interest saved in a full period, on the balance that was refinanced.

        Struck on the old balance rather than the new face, so an upsizing does
        not report the cost of the extra money it borrowed as a saving on the
        money it already owed.
        """
        return self.repaid * self.spread_saved

    @property
    def saving_over_the_remainder(self) -> Money:
        """That saving across the periods left in the projection.

        An undiscounted sum, and read as an order of magnitude rather than a
        valuation: it ignores the amortisation and the sweep that will reduce
        the balance it is struck on, both of which cut it. What it answers is
        whether the exercise was worth its cash cost at all, which is a
        comparison the premium and this figure can settle between them.
        """
        return self.annual_saving * money(self.periods_remaining)

    @property
    def pays_back(self) -> bool:
        """Whether the interest saved covers what the exercise cost in cash."""
        return self.saving_over_the_remainder > self.cash_cost
