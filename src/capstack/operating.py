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

from collections.abc import Iterator
from dataclasses import dataclass

from .drivers import Driver
from .money import ONE, ZERO, Money, Numeric, money, safe_div
from .periods import Period, PeriodGrid

__all__ = [
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

    @property
    def index(self) -> int:
        return self.period.index

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
    ) -> OperatingModel:
        """Roll the case forward across ``grid``.

        ``opening_net_working_capital`` defaults to the opening revenue at the
        first period's working-capital rate. That default matters: it is the
        base the first period's movement is measured against, and assuming a
        zero opening balance would charge the entire working-capital balance to
        period one as though the business had been founded at close.
        """
        revenue = money(opening_revenue)
        if revenue < 0:
            raise ValueError("opening revenue must not be negative")

        count = len(grid)
        nwc = (
            money(opening_net_working_capital)
            if opening_net_working_capital is not None
            else revenue * assumptions.nwc_rate.at(0)
        )
        opening_nwc = nwc
        carryforward = assumptions.opening_carryforward

        rows: list[OperatingPeriod] = []
        for i in range(count):
            period = grid[i]

            revenue = revenue * (ONE + assumptions.revenue_growth.at(i))
            ebitda = revenue * assumptions.ebitda_margin.at(i)
            da = revenue * assumptions.da_rate.at(i)
            ebit = ebitda - da

            tax = apply_carryforward(
                ebit,
                carryforward,
                assumptions.tax_rate,
                usage_limit=assumptions.nol_usage_limit,
            )
            carryforward = tax.closing_carryforward

            nopat = ebit - tax.cash_tax
            capex = revenue * assumptions.capex_rate.at(i)

            closing_nwc = revenue * assumptions.nwc_rate.at(i)
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
