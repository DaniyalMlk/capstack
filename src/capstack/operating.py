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
from .drivers import Driver, compounded_over, within_year_weights
from .money import ONE, ZERO, Money, Numeric, money, safe_div
from .periods import Period, PeriodGrid, TrailingWindow, trailing_window

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

    def revenue_at(
        self,
        index: int,
        default_growth: Driver,
        *,
        periods_per_year: int = 1,
    ) -> Money:
        """Annualised revenue contributed in period ``index``, zero-based.

        Compounded from the run-rate at purchase across every period since,
        which is why a business bought at the end of period two is at one
        period's growth in period three rather than at its purchase run-rate.

        The figure is a run-rate: what the stream earns over a year at the point
        this period reaches. The caller scales it to the length of the period,
        for the same reason the platform's own revenue is scaled there — an
        acquisition contributes a quarter of its run-rate to a quarter.

        ``periods_per_year`` divides the growth assumption, which is annual, so
        an add-on on a quarterly grid grows at the quarterly equivalent of its
        rate rather than at the whole rate four times over. The growth entry is
        read by the year each elapsed period falls in.
        """
        elapsed = self._elapsed(index)
        if elapsed <= 0:
            return ZERO
        driver = self.growth if self.growth is not None else default_growth
        share = ONE / Decimal(periods_per_year)
        revenue = self.revenue
        for i in range(self.period, self.period + elapsed):
            revenue = revenue * (
                ONE + compounded_over(driver.at(i // periods_per_year), share)
            )
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

    def ebitda_at(
        self,
        index: int,
        default_growth: Driver,
        *,
        periods_per_year: int = 1,
    ) -> Money:
        """Annualised EBITDA this stream contributes in period ``index``."""
        return self.revenue_at(
            index, default_growth, periods_per_year=periods_per_year
        ) * self.margin + self.synergies_at(index)


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
        ppy = grid.frequency.periods_per_year

        # The organic revenue line, one figure per year of the case. It is grown
        # once a year at the annual rate, which is what the file says and what
        # an annual grid has always done, and the periods inside a year then
        # divide that figure between them. Growing it once per period instead
        # would compound an annual rate as many times as the year is reported.
        years = max((grid[i].driver_index for i in range(count)), default=0) + 1
        annual_revenue: list[Money] = []
        base = revenue
        for y in range(years):
            base = base * (ONE + assumptions.revenue_growth.at(y))
            annual_revenue.append(base)
        weights = {
            y: within_year_weights(assumptions.revenue_growth.at(y), ppy)
            for y in range(years)
        }

        rows: list[OperatingPeriod] = []
        traded: list[Money] = []
        covered: list[Money] = []
        for i in range(count):
            period = grid[i]
            j = period.driver_index
            # How much of a year this column trades, and where it sits in the
            # run of columns. The two are different questions on a sub-annual
            # grid: the assumptions are read by year and the events — an add-on
            # closing, a synergy phasing in — happen in a period.
            elapsed = period.share_of_year(day_count)
            position = max(period.index - 1, 0)

            # A stub trades at the rate the business is running at when the deal
            # closes, for as much of the year as it owns. Growth is a whole
            # year's assumption and is not applied inside it, so the base the
            # first whole period compounds from is the base the deal was
            # underwritten on — six weeks of ownership does not advance the
            # operating case by a year.
            if period.is_stub:
                organic = money(opening_revenue) * elapsed
                organic_annualised = money(opening_revenue)
            else:
                organic = annual_revenue[j] * weights[j][position % ppy]
                organic_annualised = organic / elapsed

            # The organic line is the platform. Acquired revenue is added to the
            # period's total but never to the platform, because each stream
            # compounds from its own purchase run-rate; folding it back in would
            # grow it a second time next period.
            #
            # A stream ramps period by period rather than being distributed
            # across a year like the platform is: it is bought on a date that
            # owes nothing to a year end, so there is no year of its own to
            # divide. The two conventions differ by a fraction of a period's
            # growth and only for the streams, not for the underwritten case.
            acquired_run_rate = ZERO
            acquired_run_rate_ebitda = ZERO
            for stream in streams:
                contributed = stream.revenue_at(
                    position,
                    assumptions.revenue_growth,
                    periods_per_year=period.periods_per_year,
                )
                acquired_run_rate += contributed
                acquired_run_rate_ebitda += (
                    contributed * stream.margin + stream.synergies_at(position)
                )

            acquired_revenue = acquired_run_rate * elapsed
            acquired_ebitda = acquired_run_rate_ebitda * elapsed
            revenue = organic + acquired_revenue
            ebitda = organic * assumptions.ebitda_margin.at(j) + acquired_ebitda
            traded.append(revenue)
            covered.append(elapsed)

            # The run-rate the balance-sheet lines are struck against: the
            # revenue of the twelve months to this date, annualised if fewer
            # than twelve months have been traded. A ratio like working capital
            # is a stock over a year of sales, so a year of sales is what it
            # divides — taking one period and multiplying by four instead would
            # make the balance jump about with the seasonality of a quarter, and
            # would leave a year-end balance that disagreed with the same case
            # run annually.
            window = {p.index for p in grid.trailing(i)}
            seen = [k for k in range(i + 1) if grid[k].index in window]
            annualised = safe_div(
                sum((traded[k] for k in seen), ZERO),
                sum((covered[k] for k in seen), ZERO),
                default=ZERO,
            )

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
    def grid_periods(self) -> tuple[Period, ...]:
        return tuple(p.period for p in self.periods)

    @property
    def periods_per_year(self) -> int:
        return self.periods[0].period.periods_per_year

    def window(self, position: int, months: int = 12) -> TrailingWindow:
        """The trailing window ending with the period at ``position``."""
        return trailing_window(self.grid_periods, position, months)

    def trailing_ebitda(self, position: int, months: int = 12) -> Money:
        """Earnings over the twelve months ending with period ``position``.

        The figure every multiple and every covenant is struck against. On an
        annual grid it is the period's own EBITDA and this is an expensive way
        of saying so; on a quarterly one it is the four columns that make up the
        year, which is the only figure a lender would recognise.

        An incomplete window sums what there is. Callers that must not report a
        part-year as a year ask :meth:`window` whether it was complete — the
        covenant layer does exactly that, and refuses to certify against one.
        """
        rows = {p.index for p in self.window(position, months)}
        return sum((r.ebitda for r in self.periods if r.period.index in rows), ZERO)

    def trailing_revenue(self, position: int, months: int = 12) -> Money:
        rows = {p.index for p in self.window(position, months)}
        return sum((r.revenue for r in self.periods if r.period.index in rows), ZERO)

    @property
    def entry_ebitda(self) -> Money:
        """Earnings over the first twelve months of whole-period trading.

        What the business is bought on. A stub is left out of it deliberately:
        the entry multiple is applied to a year, and the six weeks between
        signing and the first accounts are not one — folding them in would price
        the deal on thirteen and a half months of earnings.

        On an annual grid without a stub this is the first period, which is what
        it has always been.
        """
        whole = [r for r in self.periods if not r.period.is_stub]
        if not whole:
            return self.periods[0].ebitda
        return sum((r.ebitda for r in whole[: self.periods_per_year]), ZERO)

    @property
    def exit_ebitda(self) -> Money:
        """Earnings over the twelve months to the exit, which price it.

        A trailing figure rather than the final column, because a multiple is
        applied to a year of earnings. On an annual grid the two are the same
        number and nothing about an existing model moves; on a quarterly grid
        the final column is three months, and pricing an exit on it would value
        the business at a quarter of what it is worth.
        """
        return self.trailing_ebitda(len(self.periods) - 1)

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
        last = len(self.periods) - 1
        rows = {p.index for p in self.window(last)}
        return sum(
            (r.acquired_ebitda for r in self.periods if r.period.index in rows), ZERO
        )

    @property
    def organic_exit_ebitda(self) -> Money:
        """The rest of the earnings that price the exit, over the same year."""
        return self.exit_ebitda - self.exit_acquired_ebitda
