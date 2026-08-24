"""Covenant tests: thresholds, headroom, and the cases where a ratio has no value."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from capstack.covenants import (
    Covenant,
    CovenantReport,
    Direction,
    Measure,
)
from capstack.daycount import DayCount
from capstack.debt import CapitalStructure, DebtSchedule, InterestBasis, Tranche, TrancheKind
from capstack.drivers import Driver
from capstack.money import is_close, money
from capstack.operating import OperatingAssumptions, OperatingModel
from capstack.periods import Frequency, PeriodGrid

CLOSE = date(2026, 6, 30)


def grid(years: int = 5) -> PeriodGrid:
    return PeriodGrid.build(CLOSE, years=years, frequency=Frequency.ANNUAL)


def case(
    years: int = 5,
    *,
    margin: str = "0.20",
    growth: str = "0.05",
    capex: str = "0.04",
    tax: str = "0.25",
) -> OperatingModel:
    """A plain operating case with no working-capital movement to reason about."""
    return OperatingModel.project(
        grid(years),
        OperatingAssumptions.of(
            revenue_growth=Driver.constant(growth, years),
            ebitda_margin=Driver.constant(margin, years),
            da_rate=Driver.constant("0.03", years),
            capex_rate=Driver.constant(capex, years),
            nwc_rate=Driver.constant("0", years),
            tax_rate=tax,
        ),
        opening_revenue=1000,
    )


def stack(*tranches: Tranche, **kwargs: object) -> CapitalStructure:
    """A structure on ACT/365F, so an annual period is exactly one year of interest.

    Every rate here is fixed. The point of these tests is the covenant
    arithmetic, and a floating coupon would put a base-rate series between the
    assumption and the number being checked.
    """
    return CapitalStructure.of(
        tranches,
        interest_basis=InterestBasis.OPENING,
        day_count=DayCount.ACT_365F,
        **kwargs,  # type: ignore[arg-type]
    )


def structure() -> CapitalStructure:
    return stack(
        Tranche.of("Term Loan B", TrancheKind.TERM_LOAN, 800, cash_rate="0.06", floating=False),
        Tranche.of("Notes", TrancheKind.NOTES, 400, cash_rate="0.08"),
    )


def run(model: OperatingModel, cap: CapitalStructure | None = None) -> DebtSchedule:
    return DebtSchedule.from_operating_model(
        cap if cap is not None else structure(), model, opening_cash=50
    )


class TestDirection:
    def test_leverage_is_capped_and_coverage_has_a_floor(self) -> None:
        assert Measure.LEVERAGE.direction is Direction.MAXIMUM
        assert Measure.NET_LEVERAGE.direction is Direction.MAXIMUM
        assert Measure.INTEREST_COVERAGE.direction is Direction.MINIMUM
        assert Measure.FIXED_CHARGE_COVERAGE.direction is Direction.MINIMUM

    def test_only_leverage_measures_debt(self) -> None:
        assert Measure.LEVERAGE.is_leverage
        assert not Measure.INTEREST_COVERAGE.is_leverage

    def test_measures_and_directions_render_readably(self) -> None:
        assert str(Measure.NET_LEVERAGE) == "net_leverage"
        assert Measure.NET_LEVERAGE.label == "net leverage"
        assert str(Direction.MAXIMUM) == "maximum"


class TestCovenantValidation:
    def test_a_covenant_needs_a_name(self) -> None:
        with pytest.raises(ValueError, match="needs a name"):
            Covenant.of("  ", Measure.LEVERAGE, 6)

    def test_the_first_test_period_is_one_based(self) -> None:
        with pytest.raises(ValueError, match="1-based"):
            Covenant.of("Leverage", Measure.LEVERAGE, 6, first_test_period=0)

    def test_a_threshold_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            Covenant.of("Leverage", Measure.LEVERAGE, 0)

    def test_naming_tranches_on_a_coverage_test_is_refused(self) -> None:
        with pytest.raises(ValueError, match="does not measure debt"):
            Covenant.of(
                "Cover", Measure.INTEREST_COVERAGE, 2, tranches=["Term Loan B"]
            )

    def test_tranche_names_must_be_distinct(self) -> None:
        with pytest.raises(ValueError, match="distinct"):
            Covenant.of(
                "Leverage", Measure.LEVERAGE, 6, tranches=["Term Loan B", "Term Loan B"]
            )

    def test_a_bare_number_becomes_a_flat_threshold(self) -> None:
        covenant = Covenant.of("Leverage", Measure.LEVERAGE, "5.5")
        assert covenant.threshold_at(0) == money("5.5")
        assert covenant.threshold_at(9) == money("5.5")


class TestSteppedThresholds:
    def test_the_threshold_in_force_follows_the_series(self) -> None:
        covenant = Covenant.of(
            "Leverage", Measure.LEVERAGE, Driver.of(["7.0", "6.5", "6.0", "5.5"])
        )
        assert [covenant.threshold_at(i) for i in range(4)] == [
            money("7.0"),
            money("6.5"),
            money("6.0"),
            money("5.5"),
        ]

    def test_a_short_series_holds_its_final_level(self) -> None:
        covenant = Covenant.of("Leverage", Measure.LEVERAGE, Driver.of(["7.0", "5.0"]))
        assert covenant.threshold_at(4) == money("5.0")

    def test_a_test_holiday_defers_the_first_test(self) -> None:
        covenant = Covenant.of(
            "Leverage", Measure.LEVERAGE, 6, first_test_period=3
        )
        assert not covenant.tests(0)
        assert not covenant.tests(1)
        assert covenant.tests(2)

    def test_an_untested_period_passes_and_reports_no_ratio(self) -> None:
        model = case()
        report = CovenantReport.test(
            [Covenant.of("Leverage", Measure.LEVERAGE, "0.5", first_test_period=3)],
            run(model),
            model,
        )
        first = report.at(0)[0]
        assert not first.tested
        assert first.passes
        assert first.actual is None
        assert first.note == "not yet tested"
        # The same impossible threshold breaches the moment it goes live.
        assert report.at(2)[0].breached


class TestLeverageMeasurement:
    def test_leverage_is_closing_debt_over_ebitda(self) -> None:
        model = case()
        schedule = run(model)
        report = CovenantReport.test(
            [Covenant.of("Leverage", Measure.LEVERAGE, 10)], schedule, model
        )
        row = report.at(0)[0]
        expected = schedule[0].closing_debt / model[0].ebitda
        assert row.actual == expected

    def test_net_leverage_credits_the_cash_balance(self) -> None:
        model = case()
        schedule = run(model)
        report = CovenantReport.test(
            [
                Covenant.of("Gross", Measure.LEVERAGE, 10),
                Covenant.of("Net", Measure.NET_LEVERAGE, 10),
            ],
            schedule,
            model,
        )
        gross, net = report.at(0)
        assert gross.actual is not None and net.actual is not None
        assert net.actual < gross.actual
        difference = schedule[0].closing_cash / model[0].ebitda
        assert is_close(gross.actual - net.actual, difference, tolerance="1E-20")

    def test_naming_tranches_narrows_the_numerator(self) -> None:
        model = case()
        schedule = run(model)
        report = CovenantReport.test(
            [
                Covenant.of("Total", Measure.LEVERAGE, 10),
                Covenant.of("First lien", Measure.LEVERAGE, 10, tranches=["Term Loan B"]),
            ],
            schedule,
            model,
        )
        total, first_lien = report.at(0)
        assert first_lien.actual is not None and total.actual is not None
        assert first_lien.actual < total.actual
        assert first_lien.actual == schedule[0].tranche("Term Loan B").closing / model[0].ebitda

    def test_an_unknown_tranche_is_refused(self) -> None:
        model = case()
        with pytest.raises(ValueError, match="no tranche named"):
            CovenantReport.test(
                [Covenant.of("Leverage", Measure.LEVERAGE, 6, tranches=["Mezzanine"])],
                run(model),
                model,
            )

    def test_more_cash_than_debt_reports_negative_leverage(self) -> None:
        model = case()
        cap = stack(
            Tranche.of("Term Loan B", TrancheKind.TERM_LOAN, 10, cash_rate="0.05", floating=False)
        )
        schedule = DebtSchedule.from_operating_model(cap, model, opening_cash=500)
        report = CovenantReport.test(
            [Covenant.of("Net", Measure.NET_LEVERAGE, 6)], schedule, model
        )
        row = report.at(0)[0]
        assert row.actual is not None and row.actual < 0
        assert row.passes


class TestCoverageMeasurement:
    def test_interest_cover_is_ebitda_over_the_cash_cost_of_debt(self) -> None:
        model = case()
        schedule = run(model)
        report = CovenantReport.test(
            [Covenant.of("Cover", Measure.INTEREST_COVERAGE, "1.0")], schedule, model
        )
        row = report.at(0)[0]
        assert row.actual == model[0].ebitda / schedule[0].cash_cost_of_debt

    def test_fixed_charge_cover_deducts_capex_and_tax_and_adds_amortisation(self) -> None:
        model = case()
        cap = stack(
            Tranche.of(
                "Term Loan B",
                TrancheKind.TERM_LOAN,
                800,
                cash_rate="0.06",
                floating=False,
                amortisation=Driver.constant("0.05", 5),
            )
        )
        schedule = DebtSchedule.from_operating_model(cap, model, opening_cash=50)
        report = CovenantReport.test(
            [Covenant.of("FCCR", Measure.FIXED_CHARGE_COVERAGE, "1.0")], schedule, model
        )
        row = report.at(0)[0]
        period, debt = model[0], schedule[0]
        earnings = period.ebitda - period.capital_expenditure - period.tax.cash_tax
        charges = debt.cash_cost_of_debt + debt.mandatory_repayment
        assert charges > debt.cash_cost_of_debt
        assert row.actual == earnings / charges

    def test_fixed_charge_cover_is_the_tighter_of_the_two(self) -> None:
        model = case()
        schedule = run(model)
        report = CovenantReport.test(
            [
                Covenant.of("Interest", Measure.INTEREST_COVERAGE, "1.0"),
                Covenant.of("Fixed charge", Measure.FIXED_CHARGE_COVERAGE, "1.0"),
            ],
            schedule,
            model,
        )
        interest, fixed = report.at(0)
        assert interest.actual is not None and fixed.actual is not None
        assert fixed.actual < interest.actual


class TestUndefinedRatios:
    def test_no_earnings_and_debt_outstanding_is_a_breach(self) -> None:
        model = case(margin="0")
        schedule = run(model)
        report = CovenantReport.test(
            [Covenant.of("Leverage", Measure.LEVERAGE, 6)], schedule, model
        )
        row = report.at(0)[0]
        assert row.actual is None
        assert row.breached
        assert "earned nothing" in row.note

    def test_no_earnings_and_no_debt_passes_without_a_ratio(self) -> None:
        # Nothing earned and nothing owed. The ratio is still undefined, but a
        # business with no debt cannot be over-levered, so it is a pass.
        model = case(margin="0")
        cap = stack(Tranche.of("Vendor loan", TrancheKind.SELLER_NOTE, 0))
        schedule = DebtSchedule.from_operating_model(cap, model, opening_cash=500)
        report = CovenantReport.test(
            [Covenant.of("Leverage", Measure.LEVERAGE, 6)], schedule, model
        )
        row = report.at(0)[0]
        assert row.actual is None
        assert row.passes
        assert row.note == "no exposure to measure"

    def test_nothing_to_cover_passes_without_a_ratio(self) -> None:
        model = case()
        cap = stack(Tranche.of("Free money", TrancheKind.SELLER_NOTE, 100, cash_rate=0))
        schedule = DebtSchedule.from_operating_model(cap, model, opening_cash=0)
        report = CovenantReport.test(
            [Covenant.of("Cover", Measure.INTEREST_COVERAGE, 3)], schedule, model
        )
        row = report.at(0)[0]
        assert row.actual is None
        assert row.passes
        assert row.headroom is None
        assert row.ebitda_cushion is None


class TestHeadroom:
    def test_headroom_is_positive_on_both_sides_when_a_test_passes(self) -> None:
        model = case()
        schedule = run(model)
        report = CovenantReport.test(
            [
                Covenant.of("Leverage", Measure.LEVERAGE, 20),
                Covenant.of("Cover", Measure.INTEREST_COVERAGE, "0.5"),
            ],
            schedule,
            model,
        )
        for row in report.at(0):
            assert row.passes
            assert row.headroom is not None and row.headroom > 0

    def test_headroom_is_negative_when_a_test_is_breached(self) -> None:
        model = case()
        report = CovenantReport.test(
            [Covenant.of("Leverage", Measure.LEVERAGE, "0.5")], run(model), model
        )
        row = report.at(0)[0]
        assert row.breached
        assert row.headroom is not None and row.headroom < 0

    def test_leverage_breaches_exactly_at_the_ebitda_it_names(self) -> None:
        model = case()
        schedule = run(model)
        report = CovenantReport.test(
            [Covenant.of("Leverage", Measure.LEVERAGE, 6)], schedule, model
        )
        row = report.at(0)[0]
        assert row.ebitda_at_breach is not None
        # At that EBITDA the ratio is the threshold to the last decimal place.
        assert is_close(
            schedule[0].closing_debt / row.ebitda_at_breach, money(6), tolerance="1E-20"
        )

    def test_interest_cover_breaches_exactly_at_the_ebitda_it_names(self) -> None:
        model = case()
        schedule = run(model)
        report = CovenantReport.test(
            [Covenant.of("Cover", Measure.INTEREST_COVERAGE, "2.5")], schedule, model
        )
        row = report.at(0)[0]
        assert row.ebitda_at_breach is not None
        assert is_close(
            row.ebitda_at_breach / schedule[0].cash_cost_of_debt,
            money("2.5"),
            tolerance="1E-20",
        )

    def test_fixed_charge_cover_breaches_exactly_at_the_ebitda_it_names(self) -> None:
        model = case()
        schedule = run(model)
        report = CovenantReport.test(
            [Covenant.of("FCCR", Measure.FIXED_CHARGE_COVERAGE, "1.2")], schedule, model
        )
        row = report.at(0)[0]
        assert row.ebitda_at_breach is not None
        period, debt = model[0], schedule[0]
        earnings = row.ebitda_at_breach - period.capital_expenditure - period.tax.cash_tax
        charges = debt.cash_cost_of_debt + debt.mandatory_repayment
        assert is_close(earnings / charges, money("1.2"), tolerance="1E-20")

    def test_the_cushion_is_the_shortfall_as_a_share_of_projected_ebitda(self) -> None:
        model = case()
        report = CovenantReport.test(
            [Covenant.of("Leverage", Measure.LEVERAGE, 6)], run(model), model
        )
        row = report.at(0)[0]
        assert row.ebitda_at_breach is not None and row.ebitda_cushion is not None
        assert row.ebitda_cushion == (row.ebitda - row.ebitda_at_breach) / row.ebitda

    def test_a_breached_test_reports_a_negative_cushion(self) -> None:
        model = case()
        report = CovenantReport.test(
            [Covenant.of("Leverage", Measure.LEVERAGE, "0.5")], run(model), model
        )
        row = report.at(0)[0]
        assert row.ebitda_cushion is not None and row.ebitda_cushion < 0

    def test_a_covenant_at_the_projected_level_has_no_cushion(self) -> None:
        model = case()
        schedule = run(model)
        exact = schedule[0].closing_debt / model[0].ebitda
        report = CovenantReport.test(
            [Covenant.of("Leverage", Measure.LEVERAGE, exact)], schedule, model
        )
        row = report.at(0)[0]
        assert row.passes
        assert row.ebitda_cushion == 0

    def test_no_debt_means_the_test_survives_any_fall_in_earnings(self) -> None:
        model = case()
        cap = stack(
            Tranche.of("Undrawn", TrancheKind.REVOLVER, 0, commitment=100, floating=False)
        )
        schedule = DebtSchedule.from_operating_model(cap, model, opening_cash=0)
        report = CovenantReport.test(
            [Covenant.of("Leverage", Measure.LEVERAGE, 6)], schedule, model
        )
        row = report.at(0)[0]
        assert row.ebitda_at_breach == 0
        assert row.ebitda_cushion == 1


class TestBreachDetection:
    def test_a_comfortable_case_passes_every_period(self) -> None:
        model = case()
        report = CovenantReport.test(
            [Covenant.of("Leverage", Measure.LEVERAGE, 20)], run(model), model
        )
        assert report.passes
        assert report.breaches == ()
        assert report.first_breach is None

    def test_the_first_breach_names_its_period_and_its_test(self) -> None:
        model = case()
        schedule = run(model)
        # A threshold that the case clears at first and steps below later.
        levels = [schedule[i].closing_debt / model[i].ebitda for i in range(5)]
        thresholds = [levels[0] + 1, levels[1] + 1, levels[2] - 1, levels[3] - 1, levels[4] - 1]
        report = CovenantReport.test(
            [Covenant.of("Leverage", Measure.LEVERAGE, Driver.of(thresholds))],
            schedule,
            model,
        )
        breach = report.first_breach
        assert breach is not None
        assert breach.index == 3
        assert breach.covenant == "Leverage"
        assert len(report.breaches) == 3

    def test_a_report_is_ordered_by_period_then_covenant(self) -> None:
        model = case()
        report = CovenantReport.test(
            [
                Covenant.of("Leverage", Measure.LEVERAGE, 20),
                Covenant.of("Cover", Measure.INTEREST_COVERAGE, "0.5"),
            ],
            run(model),
            model,
        )
        assert len(report) == 10
        names = [o.covenant for o in report]
        assert names[:4] == ["Leverage", "Cover", "Leverage", "Cover"]
        assert [o.index for o in report][:4] == [1, 1, 2, 2]

    def test_the_tightest_test_is_the_one_with_the_least_cushion(self) -> None:
        model = case()
        schedule = run(model)
        loose = Covenant.of("Loose", Measure.LEVERAGE, 20)
        tight = Covenant.of(
            "Tight",
            Measure.LEVERAGE,
            schedule[0].closing_debt / model[0].ebitda * money("1.01"),
        )
        report = CovenantReport.test([loose, tight], schedule, model)
        tightest = report.tightest
        assert tightest is not None
        assert tightest.covenant == "Tight"
        assert report.minimum_cushion == tightest.ebitda_cushion

    def test_a_report_with_no_measurable_test_has_no_tightest(self) -> None:
        model = case(margin="0")
        report = CovenantReport.test(
            [Covenant.of("Leverage", Measure.LEVERAGE, 6)], run(model), model
        )
        assert report.tightest is None
        assert report.minimum_cushion is None

    def test_a_covenant_can_be_read_back_by_name(self) -> None:
        model = case()
        report = CovenantReport.test(
            [Covenant.of("Leverage", Measure.LEVERAGE, 20)], run(model), model
        )
        assert len(report.for_covenant("Leverage")) == 5
        with pytest.raises(KeyError):
            report.for_covenant("Cover")

    def test_a_period_outside_the_report_is_refused(self) -> None:
        model = case()
        report = CovenantReport.test(
            [Covenant.of("Leverage", Measure.LEVERAGE, 20)], run(model), model
        )
        assert report.at(-1)[0].index == 5
        with pytest.raises(IndexError):
            report.at(9)

    def test_a_mismatched_schedule_and_case_are_refused(self) -> None:
        model = case(5)
        short = case(3)
        with pytest.raises(ValueError, match="operating case 3"):
            CovenantReport.test(
                [Covenant.of("Leverage", Measure.LEVERAGE, 6)], run(model), short
            )


class TestAgainstIndependentArithmetic:
    """One deal worked by hand, so the engine is checked against a number it did not produce."""

    def test_a_hand_computed_period(self) -> None:
        # Revenue 1,000 growing 5%, margin 20%: period one EBITDA is 210.
        model = case()
        assert model[0].ebitda == money(210)

        # Debt 1,200 at close, no amortisation and no sweep, so the balance does
        # not move. Opening-balance interest on ACT/365F: 800 at 6% is 48 and
        # 400 at 8% is 32, so 80 of cash interest.
        cap = stack(
            Tranche.of(
                "Term Loan B", TrancheKind.TERM_LOAN, 800, cash_rate="0.06", floating=False
            ),
            Tranche.of("Notes", TrancheKind.NOTES, 400, cash_rate="0.08"),
            sweep_rate=0,
        )
        schedule = run(model, cap)
        assert schedule[0].cash_interest == money(80)

        report = CovenantReport.test(
            [
                Covenant.of("Leverage", Measure.LEVERAGE, "6.00"),
                Covenant.of("Cover", Measure.INTEREST_COVERAGE, "2.00"),
            ],
            schedule,
            model,
        )
        leverage, cover = report.at(0)

        # Debt does not move in period one, so leverage is 1,200 / 210.
        assert leverage.actual is not None
        assert schedule[0].closing_debt == money(1200)
        assert leverage.actual == money(1200) / money(210)
        assert round(Decimal(leverage.actual), 4) == Decimal("5.7143")
        assert leverage.passes

        # It would breach at 1,200 / 6.00 = 200 of EBITDA, a 4.76% fall.
        assert leverage.ebitda_at_breach == money(200)
        assert leverage.ebitda_cushion is not None
        assert round(Decimal(leverage.ebitda_cushion), 4) == Decimal("0.0476")

        # Cover is 210 / 80 = 2.625x against a 2.00x floor, so it trips at 160.
        assert cover.actual == money("2.625")
        assert cover.ebitda_at_breach == money(160)
        assert cover.ebitda_cushion is not None
        assert round(Decimal(cover.ebitda_cushion), 6) == Decimal("0.238095")

        # The leverage test is the binding one: less room to give.
        tightest = report.tightest
        assert tightest is not None and tightest.covenant == "Leverage"
