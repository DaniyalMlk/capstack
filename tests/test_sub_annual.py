"""A case projected onto a sub-annual grid describes the same business.

The property under test throughout is an equivalence rather than a value: the
same deal file, run annually and then quarterly, has to underwrite the same
company. It is worth stating why that is the right test. Almost any arithmetic
slip inside a period — a rate applied four times, a flow booked whole into a
quarter, an assumption read by column — survives a test that only asks whether
the numbers are plausible, because they remain plausible. It does not survive a
comparison against the same case measured a different way.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from capstack.daycount import DayCount
from capstack.debt import CapitalStructure, DebtSchedule, Tranche, TrancheKind
from capstack.drivers import Driver, compounded_over, within_year_weights
from capstack.money import ONE, ZERO, is_close, money
from capstack.operating import OperatingAssumptions, OperatingModel
from capstack.periods import Frequency, Period, PeriodGrid, trailing_window

CLOSE = date(2026, 1, 1)
YEARS = 3
FREQUENCIES = (
    Frequency.ANNUAL,
    Frequency.SEMI_ANNUAL,
    Frequency.QUARTERLY,
    Frequency.MONTHLY,
)
SUB_ANNUAL = FREQUENCIES[1:]


def assumptions() -> OperatingAssumptions:
    """A case whose assumptions move, so a mis-read series shows up."""
    return OperatingAssumptions.of(
        revenue_growth=Driver.of(["0.09", "0.07", "0.05"]),
        ebitda_margin=Driver.of(["0.20", "0.21", "0.22"]),
        da_rate=Driver.of(["0.030", "0.032", "0.034"]),
        capex_rate=Driver.of(["0.045", "0.042", "0.040"]),
        nwc_rate=Driver.of(["0.15", "0.14", "0.13"]),
        tax_rate="0.25",
    )


def case(frequency: Frequency, *, years: int = YEARS) -> OperatingModel:
    grid = PeriodGrid.build(CLOSE, years, frequency)
    return OperatingModel.project(grid, assumptions(), opening_revenue=1000)


def by_year(model: OperatingModel, attribute: str) -> list[Decimal]:
    """Sum a line over each year of the case."""
    step = model.periods_per_year
    whole = [p for p in model.periods if not p.period.is_stub]
    return [
        sum((getattr(p, attribute) for p in whole[y : y + step]), ZERO)
        for y in range(0, len(whole), step)
    ]


class TestAPeriodKnowsItsYear:
    def test_a_bare_period_is_annual(self) -> None:
        period = Period(index=1, start=CLOSE, end=date(2027, 1, 1))
        assert period.periods_per_year == 1
        assert period.share_of_year() == ONE

    def test_a_year_holds_at_least_one_period(self) -> None:
        with pytest.raises(ValueError, match="at least one period"):
            Period(index=1, start=CLOSE, end=date(2027, 1, 1), periods_per_year=0)

    @pytest.mark.parametrize("frequency", FREQUENCIES)
    def test_a_grid_stamps_its_frequency_on_every_period(
        self, frequency: Frequency
    ) -> None:
        grid = PeriodGrid.build(CLOSE, YEARS, frequency)
        assert all(
            p.periods_per_year == frequency.periods_per_year for p in grid
        )

    def test_a_grid_refuses_periods_that_disagree_with_it(self) -> None:
        quarters = PeriodGrid.build(CLOSE, 1, Frequency.QUARTERLY).periods
        annual = tuple(
            Period(index=p.index, start=p.start, end=p.end) for p in quarters
        )
        with pytest.raises(ValueError, match="divides its year"):
            PeriodGrid(periods=annual, frequency=Frequency.QUARTERLY)

    @pytest.mark.parametrize("frequency", FREQUENCIES)
    def test_the_periods_of_a_year_share_it_out(self, frequency: Frequency) -> None:
        grid = PeriodGrid.build(CLOSE, YEARS, frequency)
        step = frequency.periods_per_year
        first_year = grid.periods[:step]
        assert is_close(
            sum((p.share_of_year() for p in first_year), ZERO), ONE, tolerance="1E-25"
        )

    @pytest.mark.parametrize(
        ("frequency", "expected"),
        [
            (Frequency.ANNUAL, [0, 1, 2]),
            (Frequency.SEMI_ANNUAL, [0, 0, 1, 1, 2, 2]),
            (Frequency.QUARTERLY, [0] * 4 + [1] * 4 + [2] * 4),
        ],
    )
    def test_an_assumption_is_read_by_year(
        self, frequency: Frequency, expected: list[int]
    ) -> None:
        grid = PeriodGrid.build(CLOSE, YEARS, frequency)
        assert [p.driver_index for p in grid] == expected

    def test_a_stub_reads_the_year_it_closed_in(self) -> None:
        grid = PeriodGrid.build(
            date(2026, 11, 15), 1, Frequency.QUARTERLY, stub_to=date(2027, 1, 1)
        )
        assert grid.has_stub
        assert [p.driver_index for p in grid] == [0, 0, 0, 0, 0]

    def test_a_stub_takes_the_days_it_actually_traded(self) -> None:
        grid = PeriodGrid.build(
            date(2026, 11, 15), 1, Frequency.QUARTERLY, stub_to=date(2027, 1, 1)
        )
        stub = grid.periods[0]
        assert stub.share_of_year() == stub.year_fraction()
        assert stub.share_of_year() < money("0.25")


class TestARateStatedAnnually:
    def test_a_whole_year_is_the_rate_itself(self) -> None:
        assert compounded_over(money("0.08"), ONE) == money("0.08")

    @pytest.mark.parametrize("periods", [2, 4, 12])
    def test_the_parts_compound_back_to_the_whole(self, periods: int) -> None:
        annual = money("0.08")
        part = compounded_over(annual, ONE / Decimal(periods))
        assert is_close((ONE + part) ** periods - ONE, annual, tolerance="1E-25")

    def test_a_quarter_of_eight_per_cent_is_not_two_per_cent(self) -> None:
        # The whole point: naive division would give 2%, which compounds to
        # 8.24%, and naive application would give 8%, which compounds to 36%.
        quarterly = compounded_over(money("0.08"), ONE / Decimal(4))
        assert money("0.0194") < quarterly < money("0.0195")

    def test_a_business_that_ends_the_year_at_nothing(self) -> None:
        assert compounded_over(money("-1"), ONE / Decimal(4)) == -ONE

    def test_a_rate_below_minus_one_has_no_equivalent(self) -> None:
        with pytest.raises(ValueError, match="past nothing"):
            compounded_over(money("-1.5"), ONE / Decimal(4))

    def test_a_period_covers_a_positive_share(self) -> None:
        with pytest.raises(ValueError, match="positive share"):
            compounded_over(money("0.08"), ZERO)


class TestDividingAYearOfTrading:
    @pytest.mark.parametrize("periods", [1, 2, 4, 12])
    def test_the_shares_sum_to_exactly_one(self, periods: int) -> None:
        assert sum(within_year_weights(money("0.09"), periods), ZERO) == ONE

    def test_one_period_a_year_takes_all_of_it(self) -> None:
        assert within_year_weights(money("0.09"), 1) == (ONE,)

    def test_a_growing_business_earns_more_later_in_the_year(self) -> None:
        weights = within_year_weights(money("0.09"), 4)
        assert list(weights) == sorted(weights)
        assert weights[0] < weights[-1]

    def test_a_shrinking_business_earns_more_earlier(self) -> None:
        weights = within_year_weights(money("-0.09"), 4)
        assert list(weights) == sorted(weights, reverse=True)

    def test_a_flat_year_divides_evenly(self) -> None:
        assert within_year_weights(ZERO, 4) == (money("0.25"),) * 4

    def test_a_year_ending_at_nothing_trades_in_its_first_period(self) -> None:
        assert within_year_weights(money("-1"), 4) == (ONE, ZERO, ZERO, ZERO)

    def test_a_year_holds_at_least_one_period(self) -> None:
        with pytest.raises(ValueError, match="at least one period"):
            within_year_weights(money("0.09"), 0)


class TestTheTrailingWindow:
    def test_an_annual_period_is_its_own_year(self) -> None:
        grid = PeriodGrid.build(CLOSE, YEARS, Frequency.ANNUAL)
        for position in range(len(grid)):
            window = grid.trailing(position)
            assert window.complete
            assert window.indices == (position + 1,)

    def test_a_quarterly_year_takes_four_quarters_to_assemble(self) -> None:
        grid = PeriodGrid.build(CLOSE, YEARS, Frequency.QUARTERLY)
        assert [grid.trailing(i).complete for i in range(4)] == [
            False,
            False,
            False,
            True,
        ]
        assert grid.trailing(3).indices == (1, 2, 3, 4)
        assert grid.trailing(3).days == 365
        assert grid.trailing(7).indices == (5, 6, 7, 8)

    def test_a_stub_delays_the_first_complete_year(self) -> None:
        grid = PeriodGrid.build(
            date(2026, 11, 15), 2, Frequency.QUARTERLY, stub_to=date(2027, 1, 1)
        )
        # The stub is inside the first four windows and none of them covers a
        # year; the first complete one is the fourth whole quarter.
        assert [w.complete for w in (grid.trailing(i) for i in range(5))] == [
            False,
            False,
            False,
            False,
            True,
        ]
        assert grid.trailing(4).indices == (1, 2, 3, 4)

    def test_a_window_never_reaches_past_the_grid(self) -> None:
        grid = PeriodGrid.build(CLOSE, 1, Frequency.ANNUAL)
        with pytest.raises(IndexError, match="outside"):
            grid.trailing(5)

    def test_a_window_needs_something_to_look_at(self) -> None:
        with pytest.raises(ValueError, match="at least one period"):
            trailing_window([], 0)


class TestTheSameCaseOnEveryFrequency:
    """The year-by-year equivalence, line by line.

    Every one of these lines is settled by the annual assumption for the year,
    so dividing the year differently cannot move it. The tolerance is a rounding
    unit of the working precision rather than a modelling allowance: the errors
    these tests exist to catch — a rate applied four times, a flow booked whole
    into a quarter — are wrong by tens of per cent, some twenty-five orders of
    magnitude larger than anything admitted here.
    """

    TOLERANCE = "1E-20"

    @pytest.mark.parametrize("frequency", SUB_ANNUAL)
    @pytest.mark.parametrize(
        "line", ["revenue", "ebitda", "depreciation_and_amortisation", "ebit"]
    )
    def test_the_profit_and_loss_ties_year_by_year(
        self, frequency: Frequency, line: str
    ) -> None:
        sub = by_year(case(frequency), line)
        annual = by_year(case(Frequency.ANNUAL), line)
        assert all(
            is_close(a, b, tolerance=self.TOLERANCE) for a, b in zip(sub, annual)
        )

    @pytest.mark.parametrize("frequency", SUB_ANNUAL)
    def test_capital_expenditure_ties_year_by_year(
        self, frequency: Frequency
    ) -> None:
        sub = by_year(case(frequency), "capital_expenditure")
        annual = by_year(case(Frequency.ANNUAL), "capital_expenditure")
        assert all(
            is_close(a, b, tolerance=self.TOLERANCE) for a, b in zip(sub, annual)
        )

    @pytest.mark.parametrize("frequency", SUB_ANNUAL)
    def test_cash_tax_ties_year_by_year(self, frequency: Frequency) -> None:
        def taxes(model: OperatingModel) -> list[Decimal]:
            step = model.periods_per_year
            rows = model.periods
            return [
                sum((p.tax.cash_tax for p in rows[y : y + step]), ZERO)
                for y in range(0, len(rows), step)
            ]

        sub, annual = taxes(case(frequency)), taxes(case(Frequency.ANNUAL))
        assert all(
            is_close(a, b, tolerance=self.TOLERANCE) for a, b in zip(sub, annual)
        )

    @pytest.mark.parametrize("frequency", SUB_ANNUAL)
    def test_the_working_capital_balance_ties_at_each_year_end(
        self, frequency: Frequency
    ) -> None:
        step = frequency.periods_per_year
        sub = case(frequency)
        annual = case(Frequency.ANNUAL)
        year_ends = [sub[i].net_working_capital for i in range(step - 1, len(sub), step)]
        assert all(
            is_close(a, b.net_working_capital, tolerance=self.TOLERANCE)
            for a, b in zip(year_ends, annual)
        )

    @pytest.mark.parametrize("frequency", SUB_ANNUAL)
    def test_free_cash_flow_ties_year_by_year(self, frequency: Frequency) -> None:
        sub = by_year(case(frequency), "unlevered_free_cash_flow")
        annual = by_year(case(Frequency.ANNUAL), "unlevered_free_cash_flow")
        assert all(
            is_close(a, b, tolerance=self.TOLERANCE) for a, b in zip(sub, annual)
        )

    @pytest.mark.parametrize("frequency", FREQUENCIES)
    def test_the_earnings_that_price_the_exit_are_a_year_of_them(
        self, frequency: Frequency
    ) -> None:
        sub = case(frequency)
        assert is_close(
            sub.exit_ebitda, case(Frequency.ANNUAL).exit_ebitda, tolerance="1E-20"
        )
        # And they really are the trailing year rather than the final column.
        if frequency is not Frequency.ANNUAL:
            assert sub.exit_ebitda > sub[-1].ebitda

    @pytest.mark.parametrize("frequency", FREQUENCIES)
    def test_the_earnings_the_deal_is_bought_on_are_a_year_of_them(
        self, frequency: Frequency
    ) -> None:
        assert is_close(
            case(frequency).entry_ebitda,
            case(Frequency.ANNUAL).entry_ebitda,
            tolerance="1E-20",
        )


class TestGrowthMeansWhatItSays:
    @pytest.mark.parametrize("frequency", FREQUENCIES)
    def test_the_revenue_line_grows_at_the_annual_rate(
        self, frequency: Frequency
    ) -> None:
        revenue = by_year(case(frequency), "revenue")
        assert is_close(revenue[1] / revenue[0] - ONE, money("0.07"), tolerance="1E-20")
        assert is_close(revenue[2] / revenue[1] - ONE, money("0.05"), tolerance="1E-20")

    def test_a_quarter_slopes_within_its_year(self) -> None:
        quarterly = case(Frequency.QUARTERLY)
        first_year = [p.revenue for p in quarterly.periods[:4]]
        assert first_year == sorted(first_year)
        assert first_year[0] < first_year[3]

    def test_a_flat_case_divides_its_year_evenly(self) -> None:
        flat = OperatingAssumptions.of(
            revenue_growth=Driver.constant(0, 2),
            ebitda_margin=Driver.constant("0.20", 2),
            da_rate=Driver.constant(0, 2),
            capex_rate=Driver.constant(0, 2),
            nwc_rate=Driver.constant(0, 2),
            tax_rate=0,
        )
        grid = PeriodGrid.build(CLOSE, 1, Frequency.QUARTERLY)
        model = OperatingModel.project(grid, flat, opening_revenue=1000)
        assert [p.revenue for p in model] == [money(250)] * 4


class TestAmortisationIsWrittenPerYear:
    """A term loan repaying 1% a year repays 1% a year on any grid."""

    def structure(self) -> CapitalStructure:
        return CapitalStructure.of(
            [
                Tranche.of(
                    "Term Loan A",
                    TrancheKind.TERM_LOAN,
                    500,
                    cash_rate="0.06",
                    floating=False,
                    amortisation=Driver.constant("0.05", YEARS),
                    swept=False,
                )
            ],
            day_count=DayCount.ACT_360,
        )

    def schedule(self, frequency: Frequency) -> DebtSchedule:
        model = case(frequency)
        return DebtSchedule.from_operating_model(
            self.structure(), model, opening_cash=200
        )

    @pytest.mark.parametrize("frequency", FREQUENCIES)
    def test_a_year_repays_five_per_cent_of_face(self, frequency: Frequency) -> None:
        schedule = self.schedule(frequency)
        step = frequency.periods_per_year
        first_year = sum(
            (row.mandatory_repayment for row in schedule.periods[:step]), ZERO
        )
        assert is_close(first_year, money(25), tolerance="1E-20")

    @pytest.mark.parametrize("frequency", SUB_ANNUAL)
    def test_the_whole_schedule_repays_what_the_annual_one_does(
        self, frequency: Frequency
    ) -> None:
        total = sum(
            (row.mandatory_repayment for row in self.schedule(frequency)), ZERO
        )
        annual = sum(
            (row.mandatory_repayment for row in self.schedule(Frequency.ANNUAL)), ZERO
        )
        assert is_close(total, annual, tolerance="1E-20")

    @pytest.mark.parametrize("frequency", SUB_ANNUAL)
    def test_the_closing_balance_lands_where_the_annual_one_does(
        self, frequency: Frequency
    ) -> None:
        assert is_close(
            self.schedule(frequency)[-1].closing_debt,
            self.schedule(Frequency.ANNUAL)[-1].closing_debt,
            tolerance="1E-20",
        )


class TestNothingAnnualMoved:
    """The annual grid is the one every existing example is checked against.

    These are the values the case produced before a period knew what a quarter
    was, written out rather than derived, so a future change to the sub-annual
    machinery cannot quietly restate an annual model.
    """

    def test_revenue(self) -> None:
        model = case(Frequency.ANNUAL)
        assert [round(float(p.revenue), 4) for p in model] == [
            1090.0,
            1166.3,
            1224.615,
        ]

    def test_ebitda(self) -> None:
        model = case(Frequency.ANNUAL)
        assert [round(float(p.ebitda), 4) for p in model] == [
            218.0,
            244.923,
            269.4153,
        ]

    def test_free_cash_flow(self) -> None:
        model = case(Frequency.ANNUAL)
        assert [round(float(p.unlevered_free_cash_flow), 4) for p in model] == [
            109.125,
            144.256,
            167.5682,
        ]
