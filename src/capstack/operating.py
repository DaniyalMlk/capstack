"""The operating case: drivers through to unlevered free cash flow.

What the business earns, and what it converts into cash, before any of it is
claimed by lenders. Everything here is unlevered on purpose — the capital
structure has no bearing on how many widgets get sold, and keeping the two
apart means the debt schedule in the next layer has a clean input rather than a
circular one.

Two things are easy to get wrong and are handled explicitly.

*Working capital is a movement, not a level.* A business growing at 8% with
working capital steady at 15% of revenue consumes cash every single period, even
though the ratio never budges, because the balance is rising and the increase
has to be funded. Subtracting the balance rather than the change is the classic
error and it understates cash flow by an order of magnitude.

*Losses carry forward, but they do not shelter everything.* A loss in one period
reduces tax in a later one, so the tax line depends on the whole history rather
than on the period in front of you. The usable amount is also capped at a
percentage of taxable income, which means a company with large historic losses
still writes a cheque to the revenue authority the moment it turns profitable.
Modelling the pool without the cap overstates cash in precisely the years a
sponsor is counting on it.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from decimal import Decimal

from .daycount import DayCount
from .drivers import Driver
from .money import ONE, ZERO, Money, Numeric, money, safe_div
from .periods import Period, PeriodGrid

__all__ = [
    "AcquiredStream",
    "OperatingAssumptions",
    "OperatingModel",
    "OperatingPeriod",
    "TaxResult",
    "apply_carryforward",
]

#: Default cap on how much of a period's taxable income a carried-forward loss
#: may shelter. Eighty per cent matches the limitation that applies to losses
#: arising in US tax years from 2018 onward; set it to 1 to model a regime with
#: no such cap.
DEFAULT_NOL_USAGE_LIMIT = money("0.80")


@dataclass(frozen=True, slots=True)
class TaxResult:
    """What a period's tax computation produced, with the pool movement shown."""

    taxable_income: Money
    loss_relief_used: Money
    taxable_after_relief: Money
    cash_tax: Money
    closing_carryforward: Money


def apply_carryforward(
    taxable_income: Money,
    opening_carryforward: Money,
    tax_rate: Money,
    *,
    usage_limit: Money = DEFAULT_NOL_USAGE_LIMIT,
) -> TaxResult:
    """Compute cash tax for one period against a loss carryforward.

    A loss adds to the pool and pays nothing. A profit draws on the pool, but
    only up to ``usage_limit`` of that profit, so the remainder is taxed even
    when the pool is large enough to cover it several times over.
    """
    if tax_rate < 0 or tax_rate > 1:
        raise ValueError("the tax rate must be between 0 and 1")
    if usage_limit < 0 or usage_limit > 1:
        raise ValueError("the loss usage limit must be between 0 and 1")
    if opening_carryforward < 0:
        raise ValueError("the carryforward pool must not be negative")

    if taxable_income <= 0:
        # The loss itself joins the pool; nothing is payable.
        return TaxResult(
            taxable_income=taxable_income,
            loss_relief_used=ZERO,
            taxable_after_relief=ZERO,
            cash_tax=ZERO,
            closing_carryforward=opening_carryforward - taxable_income,
        )

    shelterable = taxable_income * usage_limit
    used = min(opening_carryforward, shelterable)
    after_relief = taxable_income - used
    return TaxResult(
        taxable_income=taxable_income,
        loss_relief_used=used,
        taxable_after_relief=after_relief,
        cash_tax=after_relief * tax_rate,
        closing_carryforward=opening_carryforward - used,
    )


@dataclass(frozen=True, slots=True)
class OperatingAssumptions:
    """The operating case, one driver per line.

    Rates are expressed against revenue because that is how an operating case is
    negotiated and how it is sanity-checked. Capital expenditure at 4% of
    revenue is a statement someone can argue with; 47.2 of capital expenditure
    is not.
    """

    revenue_growth: Driver
    ebitda_margin: Driver
    da_rate: Driver
    capex_rate: Driver
    nwc_rate: Driver
    tax_rate: Money
    opening_carryforward: Money = ZERO
    nol_usage_limit: Money = DEFAULT_NOL_USAGE_LIMIT

    @classmethod
    def of(
        cls,
        *,
        revenue_growth: Driver,
        ebitda_margin: Driver,
        da_rate: Driver,
        capex_rate: Driver,
        nwc_rate: Driver,
        tax_rate: Numeric,
        opening_carryforward: Numeric = 0,
        nol_usage_limit: Numeric = DEFAULT_NOL_USAGE_LIMIT,
    ) -> OperatingAssumptions:
        return cls(
            revenue_growth=revenue_growth,
            ebitda_margin=ebitda_margin,
            da_rate=da_rate,
            capex_rate=capex_rate,
            nwc_rate=nwc_rate,
            tax_rate=money(tax_rate),
            opening_carryforward=money(opening_carryforward),
            nol_usage_limit=money(nol_usage_limit),
        )

    def __post_init__(self) -> None:
        if self.tax_rate < 0 or self.tax_rate > 1:
            raise ValueError("the tax rate must be between 0 and 1")
        if self.nol_usage_limit < 0 or self.nol_usage_limit > 1:
            raise ValueError("the loss usage limit must be between 0 and 1")
        if self.opening_carryforward < 0:
            raise ValueError("the opening carryforward must not be negative")


@dataclass(frozen=True, slots=True)
class AcquiredStream:
    """Earnings bought part-way through the hold and carried from then on.

    A platform that buys three businesses over five years is not the business
    that was underwritten, and modelling it as though a bolt-on simply lifted
    the margin loses the thing that makes buy-and-build work: the acquired
    revenue arrives at its own margin, on a base that did not compound from
    close, and it is paid for at a different multiple from the platform's.

    So an acquisition is carried as a separate stream rather than folded into
    the platform's revenue line. ``revenue`` and ``margin`` are the run-rate the
    business is bought on, and both are struck at the moment it closes: the
    stream contributes nothing to the period it is acquired in and a full
    period's trading to every period after.

    Growth defaults to the platform's own driver, which is the assumption a
    model makes unless it has a reason not to. Passing ``growth`` states the
    reason — a tuck-in bought precisely because it is growing faster than the
    platform is the usual one.
    """

    period: int
    revenue: Money
    margin: Money
    synergies: Money = ZERO
    synergy_phase_in: int = 1
    growth: Driver | None = None
    label: str = "Add-on"

    @classmethod
    def of(
        cls,
        period: int,
        *,
        revenue: Numeric,
        margin: Numeric,
        synergies: Numeric = 0,
        synergy_phase_in: int = 1,
        growth: Driver | None = None,
        label: str = "Add-on",
    ) -> AcquiredStream:
        return cls(
            period=int(period),
            revenue=money(revenue),
            margin=money(margin),
            synergies=money(synergies),
            synergy_phase_in=int(synergy_phase_in),
            growth=growth,
            label=label,
        )

    def __post_init__(self) -> None:
        if self.period < 1:
            raise ValueError(
                f"periods are numbered from one, so period {self.period} is not one "
                f"of them"
            )
        if self.revenue <= 0:
            raise ValueError(f"{self.label}: an acquired stream has to earn something")
        if not (-1 < self.margin <= 1):
            raise ValueError(
                f"{self.label}: a margin of {self.margin} is not a share of revenue"
            )
        if self.synergy_phase_in < 1:
            raise ValueError(
                f"{self.label}: synergies phase in over at least one period"
            )
        if self.synergies < 0:
            raise ValueError(
                f"{self.label}: a negative synergy is a dis-synergy, which belongs in "
                f"the margin the business is carried at"
            )

    @property
    def ebitda(self) -> Money:
        """Run-rate EBITDA at the moment of purchase, before any synergy."""
        return self.revenue * self.margin

    def _elapsed(self, index: int) -> int:
        """Whole periods traded under ownership by the end of period ``index``."""
        return index - (self.period - 1)

    def revenue_at(self, index: int, default_growth: Driver) -> Money:
        """Revenue contributed in period ``index``, zero-based.

        Compounded from the run-rate at purchase across every period since,
        which is why a business bought at the end of period two is at one
        period's growth in period three rather than at its purchase run-rate.
        """
        elapsed = self._elapsed(index)
        if elapsed <= 0:
            return ZERO
        driver = self.growth if self.growth is not None else default_growth
        revenue = self.revenue
        for i in range(self.period, self.period + elapsed):
            revenue = revenue * (ONE + driver.at(i))
        return revenue

    def synergies_at(self, index: int) -> Money:
        """Synergy realised in period ``index``, phased straight-line to run-rate.

        Phasing is the difference between an add-on that pays for itself in year
        one and one that pays for itself in year three, and underwriting the
        first when the second is what happens is how a buy-and-build case gets
        into trouble. The default of one period is full realisation immediately,
        which is the right assumption only for a synergy that is a signature —
        a duplicated licence, a lease not renewed.
        """
        elapsed = self._elapsed(index)
        if elapsed <= 0 or self.synergies == 0:
            return ZERO
        if elapsed >= self.synergy_phase_in:
            return self.synergies
        return self.synergies * Decimal(elapsed) / Decimal(self.synergy_phase_in)

    def ebitda_at(self, index: int, default_growth: Driver) -> Money:
        """Everything this stream contributes to EBITDA in period ``index``."""
        return self.revenue_at(index, default_growth) * self.margin + self.synergies_at(
            index
        )


@dataclass(frozen=True, slots=True)
class OperatingPeriod:
    """One column of the operating case, from revenue down to cash."""

    period: Period
    revenue: Money
    ebitda: Money
    depreciation_and_amortisation: Money
    ebit: Money
    tax: TaxResult
    nopat: Money
    capital_expenditure: Money
    net_working_capital: Money
    change_in_net_working_capital: Money
    unlevered_free_cash_flow: Money
    acquired_revenue: Money = ZERO
    acquired_ebitda: Money = ZERO

    @property
    def index(self) -> int:
        return self.period.index

    @property
    def organic_revenue(self) -> Money:
        """Revenue from the business that was bought at close.

        Kept separate because the two grow for different reasons, and a case
        that reports only the total cannot answer the one question an investment
        committee always asks of a buy-and-build: how much of this is the
        platform, and how much of it did we pay for again.
        """
        return self.revenue - self.acquired_revenue

    @property
    def organic_ebitda(self) -> Money:
        return self.ebitda - self.acquired_ebitda

    @property
    def ebitda_margin(self) -> Money:
        return safe_div(self.ebitda, self.revenue, default=ZERO)

    @property
    def cash_conversion(self) -> Money:
        """Unlevered free cash flow as a share of EBITDA.

        The number that decides how much leverage a business can carry. Two
        companies with identical EBITDA and different capital intensity support
        very different capital structures.
        """
        return safe_div(self.unlevered_free_cash_flow, self.ebitda, default=ZERO)


@dataclass(frozen=True, slots=True)
class OperatingModel:
    """A projected operating case across a period grid."""

    periods: tuple[OperatingPeriod, ...]
    opening_revenue: Money
    opening_net_working_capital: Money

    def __len__(self) -> int:
        return len(self.periods)

    def __iter__(self) -> Iterator[OperatingPeriod]:
        return iter(self.periods)

    def __getitem__(self, index: int) -> OperatingPeriod:
        return self.periods[index]

    @classmethod
    def project(
        cls,
        grid: PeriodGrid,
        assumptions: OperatingAssumptions,
        *,
        opening_revenue: Numeric,
        opening_net_working_capital: Numeric | None = None,
        acquisitions: Sequence[AcquiredStream] = (),
        day_count: DayCount = DayCount.ACT_365F,
    ) -> OperatingModel:
        """Roll the case forward across ``grid``.

        ``opening_net_working_capital`` defaults to the opening revenue at the
        first period's working-capital rate. That default matters: it is the
        base the first period's movement is measured against, and assuming a
        zero opening balance would charge the entire working-capital balance to
        period one as though the business had been founded at close.

        ``acquisitions`` are earnings bought during the hold. Each one grows on
        its own base, at its own margin, and joins the combined revenue that the
        capital-intensity lines are then struck against — so a business acquired
        in period two carries working capital and capital expenditure at the
        platform's rates from period three onward. That is a simplification, and
        the honest form of one: a bolt-on that needs materially more capital per
        pound of revenue than the platform is a different business, and belongs
        in the file as a second case rather than as a line in this one.

        ``day_count`` measures a stub period only. It is separate from the one
        the debt schedule accrues on, and defaults to actual days over 365
        because a business trades on calendar days rather than on whatever the
        credit agreement negotiated for its interest.
        """
        revenue = money(opening_revenue)
        if revenue < 0:
            raise ValueError("opening revenue must not be negative")

        count = len(grid)
        streams = tuple(acquisitions)
        for stream in streams:
            if stream.period > count:
                raise ValueError(
                    f"{stream.label}: closes at the end of period {stream.period}, "
                    f"which is beyond the {count} periods the case covers"
                )
        nwc = (
            money(opening_net_working_capital)
            if opening_net_working_capital is not None
            else revenue * assumptions.nwc_rate.at(0)
        )
        opening_nwc = nwc
        carryforward = assumptions.opening_carryforward
        organic = revenue

        rows: list[OperatingPeriod] = []
        for i in range(count):
            period = grid[i]
            j = period.driver_index

            # A stub trades at the rate the business is running at when the deal
            # closes, for as much of the year as it owns. Growth is a full
            # period's assumption and is not applied inside it, so the base the
            # first whole period compounds from is the base the deal was
            # underwritten on — six weeks of ownership does not advance the
            # operating case by a year.
            if period.is_stub:
                elapsed = period.year_fraction(day_count)
            else:
                organic = organic * (ONE + assumptions.revenue_growth.at(j))
                elapsed = ONE

            # The organic base is what carries forward. Acquired revenue is
            # added to the period's total but never to the base, because each
            # stream compounds from its own purchase run-rate; folding it back
            # in would grow it a second time next period.
            acquired_revenue = ZERO
            acquired_ebitda = ZERO
            for stream in streams:
                contributed = stream.revenue_at(j, assumptions.revenue_growth)
                acquired_revenue += contributed
                acquired_ebitda += contributed * stream.margin + stream.synergies_at(j)

            # Flows scale with the length of the period. The annualised total
            # is kept alongside because the balance-sheet lines are struck
            # against it rather than against what the period actually traded.
            annualised = organic + acquired_revenue
            revenue = annualised * elapsed
            ebitda = (
                organic * assumptions.ebitda_margin.at(j) + acquired_ebitda
            ) * elapsed

            da = revenue * assumptions.da_rate.at(j)
            ebit = ebitda - da

            tax = apply_carryforward(
                ebit,
                carryforward,
                assumptions.tax_rate,
                usage_limit=assumptions.nol_usage_limit,
            )
            carryforward = tax.closing_carryforward

            nopat = ebit - tax.cash_tax
            capex = revenue * assumptions.capex_rate.at(j)

            # Working capital is a balance, not a flow, and it is the one line
            # a short period gets catastrophically wrong if it is treated as
            # one. Six weeks of revenue at a 15% working-capital rate implies a
            # balance an eighth of the real one, and the difference is released
            # into the stub as cash the business never had. So the balance is
            # struck against the annualised figure and only the *movement* in it
            # reaches the cash flow, which is what a movement was always for.
            closing_nwc = annualised * assumptions.nwc_rate.at(j)
            change_in_nwc = closing_nwc - nwc
            nwc = closing_nwc

            # Depreciation is added back because it never left; the increase in
            # working capital is subtracted because it did.
            ufcf = nopat + da - capex - change_in_nwc

            rows.append(
                OperatingPeriod(
                    period=period,
                    revenue=revenue,
                    ebitda=ebitda,
                    depreciation_and_amortisation=da,
                    ebit=ebit,
                    tax=tax,
                    nopat=nopat,
                    capital_expenditure=capex,
                    net_working_capital=closing_nwc,
                    change_in_net_working_capital=change_in_nwc,
                    unlevered_free_cash_flow=ufcf,
                    acquired_revenue=acquired_revenue,
                    acquired_ebitda=acquired_ebitda,
                )
            )

        return cls(
            periods=tuple(rows),
            opening_revenue=money(opening_revenue),
            opening_net_working_capital=opening_nwc,
        )

    # -- Aggregates ------------------------------------------------------

    @property
    def entry_ebitda(self) -> Money:
        """EBITDA in the first projected period."""
        return self.periods[0].ebitda

    @property
    def exit_ebitda(self) -> Money:
        """EBITDA in the final projected period, which prices the exit."""
        return self.periods[-1].ebitda

    @property
    def total_unlevered_free_cash_flow(self) -> Money:
        return sum((p.unlevered_free_cash_flow for p in self.periods), ZERO)

    @property
    def total_cash_tax(self) -> Money:
        return sum((p.tax.cash_tax for p in self.periods), ZERO)

    @property
    def total_capital_expenditure(self) -> Money:
        return sum((p.capital_expenditure for p in self.periods), ZERO)

    @property
    def working_capital_absorbed(self) -> Money:
        """Cumulative cash taken up by working capital across the hold.

        Positive means the business consumed cash funding its own growth.
        """
        return sum((p.change_in_net_working_capital for p in self.periods), ZERO)

    @property
    def closing_carryforward(self) -> Money:
        return self.periods[-1].tax.closing_carryforward

    @property
    def has_acquisitions(self) -> bool:
        return any(p.acquired_revenue != 0 for p in self.periods)

    @property
    def exit_acquired_ebitda(self) -> Money:
        """The share of the earnings that price the exit which was bought.

        The uncomfortable number in a buy-and-build, and the one worth putting
        next to the exit multiple: earnings bought at eight turns and sold at
        eleven are worth having, but they are not the same achievement as
        earnings grown from the platform, and the bridge should not pretend
        otherwise.
        """
        return self.periods[-1].acquired_ebitda

    @property
    def organic_exit_ebitda(self) -> Money:
        return self.periods[-1].organic_ebitda
