"""A covenant is certified over the year behind the test date.

The measurement a credit agreement names is a stock on a date against a flow
over the twelve months to it. On an annual grid those twelve months are the
column, which is why dividing one column by another was right for as long as
the engine only had annual grids and silently wrong the moment it did not.
"""

from __future__ import annotations

from datetime import date

import pytest

from capstack.covenants import (
    NO_YEAR_YET,
    Certification,
    Covenant,
    CovenantReport,
    Measure,
)
from capstack.daycount import DayCount
from capstack.debt import (
    CapitalStructure,
    DebtSchedule,
    SweepGrid,
    Tranche,
    TrancheKind,
)
from capstack.drivers import Driver
from capstack.money import ZERO, is_close, money
from capstack.operating import OperatingAssumptions, OperatingModel
from capstack.periods import Frequency, PeriodGrid

CLOSE = date(2026, 1, 1)
YEARS = 3
FREQUENCIES = (Frequency.ANNUAL, Frequency.QUARTERLY, Frequency.MONTHLY)
EXACT = "1E-20"


def assumptions() -> OperatingAssumptions:
    return OperatingAssumptions.of(
        revenue_growth=Driver.of(["0.09", "0.07", "0.05"]),
        ebitda_margin=Driver.of(["0.20", "0.21", "0.22"]),
        da_rate=Driver.constant("0.03", YEARS),
        capex_rate=Driver.constant("0.04", YEARS),
        nwc_rate=Driver.constant("0.15", YEARS),
        tax_rate="0.25",
    )


def structure(*, sweep: SweepGrid | None = None) -> CapitalStructure:
    """Debt that does not move, so a ratio can be compared exactly.

    Fixed coupon, no amortisation, no sweep, no maturity: the balance stands at
    face on every date on every grid. That isolates the denominator, which is
    the thing these tests are about — a schedule that deleverages would differ
    between frequencies for perfectly good reasons and would make an exact
    comparison impossible to write.
    """
    return CapitalStructure.of(
        [
            Tranche.of(
                "Notes",
                TrancheKind.NOTES,
                800,
                cash_rate="0.07",
                floating=False,
                swept=sweep is not None,
            )
        ],
        day_count=DayCount.ACT_360,
        sweep_grid=sweep,
    )


def model(frequency: Frequency, *, stub_to: date | None = None) -> OperatingModel:
    grid = PeriodGrid.build(CLOSE, YEARS, frequency, stub_to=stub_to)
    return OperatingModel.project(grid, assumptions(), opening_revenue=1000)


def schedule(frequency: Frequency, **kwargs: object) -> DebtSchedule:
    case = model(frequency)
    return DebtSchedule.from_operating_model(
        structure(**kwargs), case, opening_cash=400  # type: ignore[arg-type]
    )


def covenants() -> list[Covenant]:
    return [
        Covenant.of("Leverage", Measure.LEVERAGE, Driver.constant("6.0", YEARS)),
        Covenant.of("Coverage", Measure.INTEREST_COVERAGE, Driver.constant("1.5", YEARS)),
        Covenant.of(
            "Fixed charge",
            Measure.FIXED_CHARGE_COVERAGE,
            Driver.constant("1.0", YEARS),
        ),
    ]


def report(frequency: Frequency) -> CovenantReport:
    case = model(frequency)
    return CovenantReport.test(
        covenants(),
        DebtSchedule.from_operating_model(structure(), case, opening_cash=400),
        case,
    )


class TestAssemblingAYear:
    def test_an_annual_period_is_its_own_year(self) -> None:
        case = model(Frequency.ANNUAL)
        rows = DebtSchedule.from_operating_model(
            structure(), case, opening_cash=400
        )
        certification = Certification.assemble(1, rows, case)
        assert certification.window.indices == (2,)
        assert certification.ebitda == case[1].ebitda
        assert certification.cash_cost_of_debt == rows[1].cash_cost_of_debt

    def test_a_quarterly_date_gathers_four_quarters(self) -> None:
        case = model(Frequency.QUARTERLY)
        rows = DebtSchedule.from_operating_model(
            structure(), case, opening_cash=400
        )
        certification = Certification.assemble(7, rows, case)
        assert certification.window.indices == (5, 6, 7, 8)
        assert certification.ebitda == sum((p.ebitda for p in case.periods[4:8]), ZERO)
        assert certification.cash_cost_of_debt == sum(
            (r.cash_cost_of_debt for r in rows.periods[4:8]), ZERO
        )
        assert certification.capital_expenditure == sum(
            (p.capital_expenditure for p in case.periods[4:8]), ZERO
        )
        assert certification.cash_tax == sum(
            (p.tax.cash_tax for p in case.periods[4:8]), ZERO
        )

    def test_the_balance_is_the_one_standing_on_the_date(self) -> None:
        """A stock is not summed over the year. It would be nonsense to."""
        case = model(Frequency.QUARTERLY)
        rows = DebtSchedule.from_operating_model(
            structure(), case, opening_cash=400
        )
        certification = Certification.assemble(7, rows, case)
        assert certification.debt.closing_debt == rows[7].closing_debt

    @pytest.mark.parametrize(
        ("frequency", "first_certifiable"),
        [(Frequency.ANNUAL, 0), (Frequency.QUARTERLY, 3), (Frequency.MONTHLY, 11)],
    )
    def test_a_year_has_to_have_been_traded(
        self, frequency: Frequency, first_certifiable: int
    ) -> None:
        case = model(frequency)
        rows = DebtSchedule.from_operating_model(
            structure(), case, opening_cash=400
        )
        certifiable = [
            Certification.assemble(i, rows, case).certifiable for i in range(len(rows))
        ]
        assert certifiable.index(True) == first_certifiable
        assert all(certifiable[first_certifiable:])

    def test_the_interval_is_stated_the_way_a_certificate_states_it(self) -> None:
        case = model(Frequency.QUARTERLY)
        rows = DebtSchedule.from_operating_model(
            structure(), case, opening_cash=400
        )
        assert (
            Certification.assemble(7, rows, case).interval
            == "twelve months to 2028-01-01"
        )


class TestTheSameCertificationOnEveryFrequency:
    """The property that matters: a year end certifies the same on any grid.

    Exact, because the structure holds its balance and the trailing earnings tie
    exactly across frequencies. Any drift here would be the covenant layer
    mixing an interval into the arithmetic, which is the defect.
    """

    @pytest.mark.parametrize("frequency", FREQUENCIES[1:])
    def test_every_year_end_matches_the_annual_run(
        self, frequency: Frequency
    ) -> None:
        step = frequency.periods_per_year
        annual = {
            (o.covenant, o.index): o for o in report(Frequency.ANNUAL) if o.tested
        }
        for observed in report(frequency):
            if not observed.tested or observed.index % step:
                continue
            reference = annual[(observed.covenant, observed.index // step)]
            assert reference.actual is not None and observed.actual is not None
            assert is_close(observed.actual, reference.actual, tolerance=EXACT)
            assert observed.passes == reference.passes

    @pytest.mark.parametrize("frequency", FREQUENCIES)
    def test_a_leverage_ratio_is_a_leverage_ratio(
        self, frequency: Frequency
    ) -> None:
        """The headline symptom: 800 of debt on a business earning about 218."""
        first = next(
            o for o in report(frequency) if o.covenant == "Leverage" and o.tested
        )
        assert first.actual is not None
        assert money("3.6") < first.actual < money("3.7")


class TestWhatCannotBeCertified:
    @pytest.mark.parametrize(
        ("frequency", "silent"),
        [(Frequency.ANNUAL, 0), (Frequency.QUARTERLY, 3), (Frequency.MONTHLY, 11)],
    )
    def test_the_periods_before_a_year_has_passed(
        self, frequency: Frequency, silent: int
    ) -> None:
        rows = [o for o in report(frequency) if o.covenant == "Leverage"]
        untested = [o for o in rows if not o.tested]
        assert len(untested) == silent
        assert all(o.note == NO_YEAR_YET for o in untested)

    def test_an_uncertifiable_period_reports_nothing_rather_than_a_number(
        self,
    ) -> None:
        quiet = [o for o in report(Frequency.QUARTERLY) if not o.tested]
        assert quiet
        assert all(o.actual is None for o in quiet)
        assert all(o.headroom is None for o in quiet)
        # And it is not a breach. Nobody has failed a test that was not run.
        assert all(o.passes and not o.breached for o in quiet)

    def test_a_stub_says_it_is_a_stub(self) -> None:
        case = model(Frequency.ANNUAL, stub_to=date(2026, 6, 30))
        rows = DebtSchedule.from_operating_model(
            structure(), case, opening_cash=400
        )
        observed = CovenantReport.test(covenants(), rows, case)
        stub_rows = [o for o in observed if o.period.is_stub]
        assert stub_rows
        assert all("stub period" in o.note for o in stub_rows)

    def test_a_stub_pushes_the_first_certification_out(self) -> None:
        case = model(Frequency.QUARTERLY, stub_to=date(2026, 2, 15))
        rows = DebtSchedule.from_operating_model(
            structure(), case, opening_cash=400
        )
        observed = [
            o
            for o in CovenantReport.test(covenants(), rows, case)
            if o.covenant == "Leverage"
        ]
        # The stub plus three quarters is not a year; the fourth whole quarter
        # is the first date with twelve months behind it.
        assert [o.tested for o in observed[:5]] == [False, False, False, False, True]


class TestTheSweepStepsOnAYearToo:
    def grid(self) -> SweepGrid:
        return SweepGrid.of(
            [("4.50", "0.75"), ("3.00", "0.50"), ("0", "0.25")], net=True
        )

    @pytest.mark.parametrize("frequency", (Frequency.ANNUAL, Frequency.QUARTERLY))
    def test_a_deal_sweeps_at_the_rate_its_leverage_earns(
        self, frequency: Frequency
    ) -> None:
        rows = schedule(frequency, sweep=self.grid())
        certified = [row.certified_leverage for row in rows if row.certified_leverage]
        # Measured on a year, the structure certifies in single-digit turns. On
        # a period it would certify at four times that on a quarterly grid and
        # pin the sweep at its top rate for the whole hold.
        assert certified
        assert all(level < money(8) for level in certified)

    def test_the_first_year_falls_back_to_the_level_it_was_priced_at(self) -> None:
        rows = schedule(Frequency.QUARTERLY, sweep=self.grid())
        annual = schedule(Frequency.ANNUAL, sweep=self.grid())
        # No date inside the first year has a year behind it, so all four
        # certify against the level the deal was priced at. At close nothing
        # else has moved either, so the first date agrees with the annual grid's
        # exactly; the later ones drift only because the balance and the cash
        # they are measured against are the live ones, which is the point of
        # certifying in arrears rather than of the fallback.
        assert rows[0].certified_leverage == annual[0].certified_leverage
        opening = annual[0].certified_leverage
        assert opening is not None
        first_year = [row.certified_leverage for row in rows.periods[:4]]
        assert all(level is not None and level < opening * money(2) for level in first_year)

    def test_the_priced_level_is_a_year_of_earnings_not_a_quarter(self) -> None:
        """Without this the fallback itself carries the defect it exists to avoid."""
        quarterly = schedule(Frequency.QUARTERLY, sweep=self.grid())[0]
        annual = schedule(Frequency.ANNUAL, sweep=self.grid())[0]
        assert quarterly.certified_leverage == annual.certified_leverage
