"""The sweep grid: which rung applies, and what the schedule does with it."""

from __future__ import annotations

from datetime import date

import pytest

from capstack.daycount import DayCount
from capstack.debt import (
    CapitalStructure,
    DebtSchedule,
    InterestBasis,
    SweepGrid,
    SweepStep,
    Tranche,
    TrancheKind,
)
from capstack.drivers import Driver
from capstack.money import ONE, ZERO, money
from capstack.operating import OperatingAssumptions, OperatingModel
from capstack.periods import Frequency, Period, PeriodGrid

CLOSE = date(2026, 6, 30)


def periods(count: int) -> list[Period]:
    return list(PeriodGrid.build(CLOSE, years=count, frequency=Frequency.ANNUAL))


def stack(*tranches: Tranche, **kwargs: object) -> CapitalStructure:
    return CapitalStructure.of(
        tranches,
        interest_basis=InterestBasis.OPENING,
        day_count=DayCount.ACT_365F,
        **kwargs,  # type: ignore[arg-type]
    )


def term_loan(face: int = 1000, rate: str = "0.05") -> Tranche:
    return Tranche.of(
        "Term Loan B", TrancheKind.TERM_LOAN, face, cash_rate=rate, floating=False
    )


class TestGridConstruction:
    def test_steps_are_sorted_highest_first_however_they_are_given(self) -> None:
        grid = SweepGrid.of([("3.5", "0.25"), ("4.5", "0.50")])
        assert [s.leverage for s in grid.steps] == [money("4.5"), money("3.5")]
        assert grid.top_rate == money("0.50")

    def test_a_grid_needs_at_least_one_step(self) -> None:
        with pytest.raises(ValueError, match="at least one step"):
            SweepGrid.of([])

    def test_two_steps_at_the_same_level_are_refused(self) -> None:
        with pytest.raises(ValueError, match="contradict"):
            SweepGrid.of([("4.5", "0.50"), ("4.5", "0.25")])

    def test_a_rate_outside_zero_to_one_is_refused(self) -> None:
        with pytest.raises(ValueError, match="between 0 and 1"):
            SweepStep.of("4.5", "1.5")

    def test_a_negative_level_is_refused(self) -> None:
        with pytest.raises(ValueError, match="not negative"):
            SweepStep.of("-1", "0.5")

    def test_a_floor_outside_zero_to_one_is_refused(self) -> None:
        with pytest.raises(ValueError, match="between 0 and 1"):
            SweepGrid.of([("4.5", "0.5")], floor="1.4")

    def test_steps_can_be_supplied_as_objects(self) -> None:
        grid = SweepGrid.of([SweepStep.of("4.5", "0.5"), SweepStep.of("3.5", "0.25")])
        assert grid.rate_at(money("4.0")) == money("0.25")


class TestWhichRungApplies:
    def test_the_rate_steps_down_as_leverage_falls(self) -> None:
        grid = SweepGrid.of([("4.5", "0.50"), ("3.5", "0.25")])
        assert grid.rate_at(money("6.0")) == money("0.50")
        assert grid.rate_at(money("4.0")) == money("0.25")
        assert grid.rate_at(money("3.0")) == ZERO

    def test_a_level_exactly_on_a_rung_takes_that_rung(self) -> None:
        grid = SweepGrid.of([("4.5", "0.50"), ("3.5", "0.25")])
        assert grid.rate_at(money("4.5")) == money("0.50")
        assert grid.rate_at(money("3.5")) == money("0.25")

    def test_below_every_rung_the_floor_applies(self) -> None:
        grid = SweepGrid.of([("4.5", "0.50")], floor="0.10")
        assert grid.rate_at(money("1.0")) == money("0.10")

    def test_unmeasurable_leverage_takes_the_top_rate(self) -> None:
        grid = SweepGrid.of([("4.5", "0.50"), ("3.5", "0.25")])
        assert grid.rate_at(None) == money("0.50")

    def test_negative_leverage_is_below_every_rung(self) -> None:
        grid = SweepGrid.of([("0.5", "0.50")])
        assert grid.rate_at(money("-2")) == ZERO


class TestStructureValidation:
    def test_a_grid_and_a_flat_rate_together_are_refused(self) -> None:
        with pytest.raises(ValueError, match="two different things"):
            stack(
                term_loan(),
                sweep_grid=SweepGrid.of([("4.5", "0.5")]),
                sweep_rate="0.5",
            )

    def test_a_grid_with_the_default_rate_is_accepted(self) -> None:
        structure = stack(term_loan(), sweep_grid=SweepGrid.of([("4.5", "0.5")]))
        assert structure.sweep_grid is not None
        assert structure.sweep_rate == ONE

    def test_a_grid_without_earnings_is_refused(self) -> None:
        structure = stack(term_loan(), sweep_grid=SweepGrid.of([("4.5", "0.5")]))
        with pytest.raises(ValueError, match="EBITDA each step is measured against"):
            DebtSchedule.run(structure, periods(3), [100, 100, 100])

    def test_mismatched_earnings_are_refused(self) -> None:
        structure = stack(term_loan(), sweep_grid=SweepGrid.of([("4.5", "0.5")]))
        with pytest.raises(ValueError, match="2 EBITDA figures"):
            DebtSchedule.run(
                structure, periods(3), [100, 100, 100], ebitda=[200, 200]
            )

    def test_earnings_without_a_grid_are_simply_carried(self) -> None:
        structure = stack(term_loan())
        schedule = DebtSchedule.run(
            structure, periods(2), [100, 100], ebitda=[200, 200]
        )
        assert all(row.sweep_rate == ONE for row in schedule)
        assert all(row.certified_leverage is None for row in schedule)


class TestTheScheduleUnderAGrid:
    def test_the_first_period_is_certified_on_the_opening_level(self) -> None:
        # 1,000 of debt against 200 of LTM EBITDA is 5.0x, so the top rung.
        structure = stack(
            term_loan(), sweep_grid=SweepGrid.of([("4.5", "0.50"), ("3.5", "0.25")])
        )
        schedule = DebtSchedule.run(
            structure,
            periods(1),
            [300],
            ebitda=[400],
            opening_ebitda=200,
        )
        assert schedule[0].certified_leverage == money(5)
        assert schedule[0].sweep_rate == money("0.50")

    def test_without_an_opening_level_the_first_projected_period_stands_in(self) -> None:
        structure = stack(
            term_loan(), sweep_grid=SweepGrid.of([("4.5", "0.50"), ("3.5", "0.25")])
        )
        schedule = DebtSchedule.run(structure, periods(1), [300], ebitda=[250])
        assert schedule[0].certified_leverage == money(4)
        assert schedule[0].sweep_rate == money("0.25")

    def test_a_later_period_is_certified_on_the_period_before_it(self) -> None:
        structure = stack(
            term_loan(), sweep_grid=SweepGrid.of([("4.5", "0.50"), ("3.5", "0.25")])
        )
        schedule = DebtSchedule.run(
            structure,
            periods(2),
            [300, 300],
            ebitda=[250, 250],
            opening_ebitda=200,
        )
        second = schedule[1]
        opening_debt = schedule[0].closing_debt - schedule[0].closing_cash
        assert second.certified_leverage == opening_debt / money(250)

    def test_the_grid_is_measured_net_of_cash_by_default(self) -> None:
        gross = SweepGrid.of([("4.5", "0.50"), ("3.5", "0.25")], net=False)
        net = SweepGrid.of([("4.5", "0.50"), ("3.5", "0.25")], net=True)
        built = [
            DebtSchedule.run(
                stack(term_loan(), sweep_grid=g, minimum_cash=100),
                periods(1),
                [300],
                opening_cash=150,
                ebitda=[400],
                opening_ebitda=200,
            )
            for g in (gross, net)
        ]
        assert built[0][0].certified_leverage == money(5)
        assert built[1][0].certified_leverage == money("4.25")
        assert built[0][0].sweep_rate == money("0.50")
        assert built[1][0].sweep_rate == money("0.25")

    def test_a_grid_that_steps_to_nothing_stops_repaying(self) -> None:
        structure = stack(
            term_loan(100, "0.02"),
            sweep_grid=SweepGrid.of([("100", "1.0")]),
        )
        schedule = DebtSchedule.run(
            structure, periods(2), [50, 50], ebitda=[200, 200], opening_ebitda=200
        )
        # Half a turn of leverage against a hundred-turn rung: never swept.
        assert all(row.sweep_rate == ZERO for row in schedule)
        assert schedule.total_repaid == 0

    def test_a_grid_sweeps_less_than_a_full_sweep_and_more_than_none(self) -> None:
        made = [
            DebtSchedule.run(
                stack(term_loan(1000, "0.05"), **kwargs),
                periods(4),
                [200, 220, 240, 260],
                ebitda=[300, 320, 340, 360],
                opening_ebitda=300,
            )
            for kwargs in (
                {},
                {"sweep_grid": SweepGrid.of([("3.0", "0.50"), ("2.0", "0.25")])},
                {"sweep_rate": 0},
            )
        ]
        full, stepped, none = (s.total_repaid for s in made)
        assert none == 0
        assert 0 < stepped < full

    def test_every_period_still_reconciles_under_a_grid(self) -> None:
        structure = stack(
            term_loan(1000, "0.05"),
            Tranche.of("Notes", TrancheKind.NOTES, 400, cash_rate="0.08"),
            sweep_grid=SweepGrid.of([("4.0", "0.75"), ("3.0", "0.50"), ("2.0", "0.25")]),
            minimum_cash=40,
        )
        schedule = DebtSchedule.run(
            structure,
            periods(5),
            [200, 240, 280, 320, 360],
            opening_cash=40,
            ebitda=[300, 330, 360, 390, 420],
            opening_ebitda=290,
        )
        assert all(row.reconciles() for row in schedule)
        for row in schedule:
            assert all(t.reconciles() for t in row.tranches)

    def test_the_rate_relaxes_as_the_structure_deleverages(self) -> None:
        structure = stack(
            term_loan(1400, "0.04"),
            sweep_grid=SweepGrid.of([("3.0", "1.0"), ("2.0", "0.50")]),
            minimum_cash=0,
        )
        schedule = DebtSchedule.run(
            structure,
            periods(5),
            [300, 320, 340, 360, 380],
            ebitda=[400, 400, 400, 400, 400],
            opening_ebitda=400,
        )
        rates = [row.sweep_rate for row in schedule]
        # Monotone: the certified level only falls, so the rate only relaxes.
        assert rates == sorted(rates, reverse=True)
        assert rates[0] == ONE
        assert rates[-1] < ONE

    def test_a_period_with_no_earnings_takes_the_top_rate(self) -> None:
        structure = stack(
            term_loan(1000, "0.04"), sweep_grid=SweepGrid.of([("3.0", "1.0")])
        )
        schedule = DebtSchedule.run(
            structure, periods(2), [300, 300], ebitda=[0, 400], opening_ebitda=0
        )
        assert schedule[0].certified_leverage is None
        assert schedule[0].sweep_rate == ONE

    def test_no_debt_and_no_earnings_certifies_at_zero(self) -> None:
        structure = stack(
            Tranche.of("Vendor loan", TrancheKind.SELLER_NOTE, 0),
            sweep_grid=SweepGrid.of([("3.0", "1.0")]),
        )
        schedule = DebtSchedule.run(
            structure, periods(1), [10], ebitda=[0], opening_ebitda=0
        )
        assert schedule[0].certified_leverage == ZERO
        assert schedule[0].sweep_rate == ZERO


class TestThroughAnOperatingModel:
    def test_an_operating_case_supplies_its_own_earnings(self) -> None:
        years = 5
        model = OperatingModel.project(
            PeriodGrid.build(CLOSE, years=years, frequency=Frequency.ANNUAL),
            OperatingAssumptions.of(
                revenue_growth=Driver.constant("0.06", years),
                ebitda_margin=Driver.constant("0.22", years),
                da_rate=Driver.constant("0.03", years),
                capex_rate=Driver.constant("0.035", years),
                nwc_rate=Driver.constant("0.10", years),
                tax_rate="0.25",
            ),
            opening_revenue=1200,
        )
        structure = stack(
            term_loan(1400, "0.055"),
            sweep_grid=SweepGrid.of([("4.5", "0.75"), ("3.5", "0.50")]),
            minimum_cash=30,
        )
        schedule = DebtSchedule.from_operating_model(
            structure, model, opening_cash=30, opening_ebitda=260
        )
        assert schedule[0].certified_leverage is not None
        assert schedule[0].sweep_rate in (money("0.75"), money("0.50"), ZERO)
        assert all(row.reconciles() for row in schedule)
        assert schedule.closing_debt < schedule.opening_debt
