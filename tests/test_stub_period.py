"""A deal that closes between reporting dates.

The stub grid is built against the same assumptions as a grid without one, so
most of these cases are a comparison: the whole periods must be untouched, and
only the short column in front of them may differ.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from capstack.cli import main
from capstack.daycount import DayCount
from capstack.debt import CapitalStructure, DebtSchedule, Tranche, TrancheKind
from capstack.drivers import Driver
from capstack.money import ZERO, is_close, money
from capstack.operating import OperatingAssumptions, OperatingModel
from capstack.periods import Frequency, Period, PeriodGrid
from capstack.spec import DealSpecError, load_deal

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
ASHCROFT = str(EXAMPLES / "ashcroft.json")
MERIDIAN = str(EXAMPLES / "meridian.json")

CLOSE = date(2026, 11, 16)
YEAR_END = date(2026, 12, 31)


def stub_grid(years: int = 5) -> PeriodGrid:
    return PeriodGrid.build(CLOSE, years=years, stub_to=YEAR_END)


def plain_grid(years: int = 5) -> PeriodGrid:
    """The same projection for a deal that closed on the reporting date."""
    return PeriodGrid.build(YEAR_END, years=years)


def assumptions() -> OperatingAssumptions:
    return OperatingAssumptions.of(
        revenue_growth=Driver.constant("0.08", 6),
        ebitda_margin=Driver.constant("0.25", 6),
        da_rate=Driver.constant("0.04", 6),
        capex_rate=Driver.constant("0.05", 6),
        nwc_rate=Driver.constant("0.15", 6),
        tax_rate="0.25",
    )


class TestBuildingTheGrid:
    def test_the_stub_is_period_zero(self) -> None:
        grid = stub_grid()
        assert grid.has_stub
        assert grid[0].index == 0
        assert grid[0].start == CLOSE
        assert grid[0].end == YEAR_END

    def test_the_whole_periods_are_still_numbered_from_one(self) -> None:
        # A maturity or an event written as "period 2" has to mean the same
        # thing whether or not a stub sits in front of period one.
        assert [p.index for p in stub_grid()] == [0, 1, 2, 3, 4, 5]

    def test_the_whole_periods_are_anchored_on_the_reporting_date(self) -> None:
        # Not on the deal anniversary: period ends fall where the accounts do.
        assert [p.end for p in stub_grid().whole_periods] == [
            p.end for p in plain_grid()
        ]

    def test_years_counts_whole_periods_and_the_stub_is_extra(self) -> None:
        assert len(stub_grid(years=5).whole_periods) == 5
        assert len(stub_grid(years=5)) == 6

    def test_a_grid_without_a_stub_is_what_it_always_was(self) -> None:
        grid = PeriodGrid.build(date(2026, 1, 1), years=3)
        assert not grid.has_stub
        assert grid.stub is None
        assert [p.index for p in grid] == [1, 2, 3]

    def test_a_reporting_date_a_whole_period_out_describes_no_stub(self) -> None:
        grid = PeriodGrid.build(date(2026, 1, 1), years=3, stub_to=date(2027, 1, 1))
        assert not grid.has_stub
        assert [p.index for p in grid] == [1, 2, 3]

    def test_a_reporting_date_before_close_is_refused(self) -> None:
        with pytest.raises(ValueError, match="on or before"):
            PeriodGrid.build(CLOSE, years=3, stub_to=date(2026, 11, 15))

    def test_a_stub_longer_than_a_period_is_refused(self) -> None:
        with pytest.raises(ValueError, match="longer than one"):
            PeriodGrid.build(CLOSE, years=3, stub_to=date(2028, 1, 1))

    def test_a_quarterly_stub_is_measured_against_a_quarter(self) -> None:
        grid = PeriodGrid.build(
            CLOSE, years=1, frequency=Frequency.QUARTERLY, stub_to=YEAR_END
        )
        assert grid.has_stub
        assert len(grid.whole_periods) == 4
        assert grid[1].start == YEAR_END

    def test_a_stub_cannot_sit_anywhere_but_first(self) -> None:
        with pytest.raises(ValueError, match="first period"):
            PeriodGrid(
                periods=(
                    Period(1, date(2026, 1, 1), date(2027, 1, 1)),
                    Period(0, date(2027, 1, 1), date(2027, 3, 1)),
                ),
                frequency=Frequency.ANNUAL,
            )


class TestReadingAnAssumption:
    def test_the_stub_and_the_first_whole_period_share_a_year(self) -> None:
        grid = stub_grid()
        assert grid[0].driver_index == 0
        assert grid[1].driver_index == 0

    def test_the_rest_follow_on(self) -> None:
        assert [p.driver_index for p in stub_grid()] == [0, 0, 1, 2, 3, 4]

    def test_without_a_stub_it_is_the_identity_it_always_was(self) -> None:
        assert [p.driver_index for p in plain_grid()] == [0, 1, 2, 3, 4]


class TestLabels:
    def test_a_stub_is_labelled_as_one(self) -> None:
        assert stub_grid()[0].short_label == "Stub"
        assert stub_grid()[0].label.startswith("Stub to ")

    def test_a_whole_period_is_not(self) -> None:
        assert stub_grid()[1].short_label == "P1"


class TestTheOperatingCase:
    def stub_case(self) -> OperatingModel:
        return OperatingModel.project(stub_grid(), assumptions(), opening_revenue=1000)

    def plain_case(self) -> OperatingModel:
        return OperatingModel.project(plain_grid(), assumptions(), opening_revenue=1000)

    def test_the_whole_periods_are_untouched_by_the_stub_in_front_of_them(self) -> None:
        # The point of the driver-index mapping. If the stub consumed year one's
        # growth, every column here would be a year ahead of itself.
        stub = [p.revenue for p in self.stub_case()][1:]
        plain = [p.revenue for p in self.plain_case()]
        assert stub == plain

    def test_the_stub_trades_the_run_rate_for_the_part_of_the_year_it_owns(self) -> None:
        row = self.stub_case()[0]
        elapsed = row.period.year_fraction(DayCount.ACT_365F)
        assert is_close(row.revenue, money(1000) * elapsed, tolerance="1E-15")

    def test_growth_is_not_applied_inside_the_stub(self) -> None:
        # Six weeks of ownership does not advance the case by a year, so the
        # base the first whole period compounds from is the underwritten one.
        assert self.stub_case()[1].revenue == money(1000) * money("1.08")

    def test_the_stub_earns_its_share_of_a_year(self) -> None:
        row = self.stub_case()[0]
        assert row.ebitda == row.revenue * money("0.25")

    def test_capital_expenditure_scales_with_the_period(self) -> None:
        row = self.stub_case()[0]
        assert row.capital_expenditure == row.revenue * money("0.05")

    def test_working_capital_is_a_balance_and_does_not_shrink(self) -> None:
        # The error the separation exists to prevent: a working-capital balance
        # struck on six weeks of revenue is an eighth of the real one, and the
        # difference lands in the stub as a cash release that did not happen.
        row = self.stub_case()[0]
        assert row.net_working_capital == money(1000) * money("0.15")
        assert row.change_in_net_working_capital == ZERO

    def test_the_stub_does_not_release_a_slug_of_cash(self) -> None:
        row = self.stub_case()[0]
        # Everything in the stub's cash flow is a fraction of a year's; nothing
        # in it is a balance-sheet unwind.
        assert row.unlevered_free_cash_flow > 0
        assert row.unlevered_free_cash_flow < self.plain_case()[0].unlevered_free_cash_flow

    def test_the_exit_is_priced_on_the_last_whole_period(self) -> None:
        assert self.stub_case().exit_ebitda == self.plain_case().exit_ebitda


class TestTheDebtSchedule:
    def structure(self) -> CapitalStructure:
        return CapitalStructure.of(
            [
                Tranche.of(
                    "TLB",
                    TrancheKind.TERM_LOAN,
                    600,
                    cash_rate="0.06",
                    floating=False,
                    amortisation=Driver.constant("0.05", 6),
                    swept=True,
                    maturity=6,
                )
            ],
            day_count=DayCount.ACT_365F,
            minimum_cash=20,
        )

    def run(self, grid: PeriodGrid) -> DebtSchedule:
        model = OperatingModel.project(grid, assumptions(), opening_revenue=1000)
        return DebtSchedule.from_operating_model(
            self.structure(), model, opening_cash=25
        )

    def test_interest_in_the_stub_is_a_fraction_of_a_year(self) -> None:
        # Interest needed no help from the stub logic: it accrues on a day count
        # and prorates itself. Asserted against the accrual rather than against
        # a whole period's figure, because a whole period's balance falls a long
        # way and the stub's barely moves, so the two are not in proportion.
        row = self.run(stub_grid())[0]
        tranche = row.tranche("TLB")
        elapsed = row.period.year_fraction(DayCount.ACT_365F)
        average = (tranche.opening + tranche.closing) / money(2)
        assert is_close(
            tranche.cash_interest,
            money("0.06") * elapsed * average,
            tolerance="1E-9",
        )

    def test_the_stub_costs_a_fraction_of_what_a_whole_period_does(self) -> None:
        stub = self.run(stub_grid())[0]
        whole = self.run(plain_grid())[0]
        assert stub.cash_interest < whole.cash_interest / money(5)

    def test_the_contractual_instalment_is_scaled_to_the_stub(self) -> None:
        stub = self.run(stub_grid())[0]
        elapsed = stub.period.year_fraction(DayCount.ACT_365F)
        assert is_close(
            stub.mandatory_repayment, money(600) * money("0.05") * elapsed,
            tolerance="1E-12",
        )

    def test_a_whole_period_still_pays_a_whole_instalment(self) -> None:
        assert self.run(stub_grid())[1].mandatory_repayment == money(30)

    def test_the_base_rate_is_read_by_driver_index(self) -> None:
        structure = CapitalStructure.of(
            [Tranche.of("TLB", TrancheKind.TERM_LOAN, 600, cash_rate="0.02")],
            base_rate=Driver.of(["0.05", "0.04", "0.03", "0.02", "0.01"]),
            day_count=DayCount.ACT_365F,
        )
        model = OperatingModel.project(stub_grid(), assumptions(), opening_revenue=1000)
        schedule = DebtSchedule.from_operating_model(structure, model, opening_cash=25)
        # The stub and the first whole period are the same year, so the same rate.
        assert [p.base_rate for p in schedule] == [
            money("0.05"),
            money("0.05"),
            money("0.04"),
            money("0.03"),
            money("0.02"),
            money("0.01"),
        ]

    def test_maturity_is_counted_in_whole_periods(self) -> None:
        # A tranche maturing in period six matures at the end of the sixth whole
        # period, and the stub in front of it does not bring that forward.
        schedule = self.run(stub_grid())
        assert schedule[0].tranche("TLB").closing > 0
        assert schedule[-1].tranche("TLB").closing == ZERO

    def test_every_period_reconciles(self) -> None:
        for row in self.run(stub_grid()):
            assert row.reconciles()
            for tranche in row.tranches:
                assert tranche.reconciles()

    def test_an_event_beyond_the_last_whole_period_is_refused(self) -> None:
        from capstack.events import Draw, Recapitalisation

        model = OperatingModel.project(stub_grid(), assumptions(), opening_revenue=1000)
        with pytest.raises(ValueError, match="beyond"):
            DebtSchedule.from_operating_model(
                self.structure(),
                model,
                opening_cash=25,
                recapitalisations=[Recapitalisation.of(6, [Draw.of("TLB", 50)])],
            )


class TestCovenants:
    def test_a_stub_is_not_a_test_date(self) -> None:
        deal = load_deal(ASHCROFT)
        report = deal.test_covenants()
        stub = [o for o in report.observations if o.period.is_stub]
        assert stub
        assert all(not o.tested for o in stub)
        assert all("twelve months" in o.note for o in stub)

    def test_an_untested_stub_does_not_breach(self) -> None:
        report = load_deal(ASHCROFT).test_covenants()
        assert all(not o.breached for o in report.observations if o.period.is_stub)

    def test_the_whole_periods_are_tested(self) -> None:
        report = load_deal(ASHCROFT).test_covenants()
        assert any(o.tested for o in report.observations if not o.period.is_stub)


class TestTheDealFile:
    def test_a_first_period_end_builds_a_stub(self) -> None:
        deal = load_deal(ASHCROFT)
        assert deal.grid is not None
        assert deal.grid.has_stub
        assert deal.grid[0].end == date(2026, 12, 31)

    def test_a_deal_without_one_has_no_stub(self) -> None:
        deal = load_deal(MERIDIAN)
        assert deal.grid is not None
        assert not deal.grid.has_stub

    def test_a_first_period_end_that_is_not_a_date_is_refused(self) -> None:
        from capstack.spec import parse_deal

        with open(ASHCROFT) as handle:
            data = json.load(handle)
        data["projection"]["first_period_end"] = "the end of the year"
        with pytest.raises(DealSpecError, match="not a date"):
            parse_deal(data)

    def test_a_first_period_end_before_close_is_refused(self) -> None:
        from capstack.spec import parse_deal

        with open(ASHCROFT) as handle:
            data = json.load(handle)
        data["projection"]["first_period_end"] = "2026-01-01"
        with pytest.raises(DealSpecError, match="on or before"):
            parse_deal(data)


class TestCapitalisedCostsAcrossAStub:
    def test_the_stub_releases_nothing(self) -> None:
        # Nothing is written down between signing the fee letter and drawing
        # the money.
        row = load_deal(ASHCROFT).fee_schedule().tranche("Term Loan B")
        assert row.periods[0].charge == ZERO
        assert row.periods[0].opening == row.capitalised

    def test_the_release_runs_over_whole_periods(self) -> None:
        row = load_deal(ASHCROFT).fee_schedule().tranche("Term Loan B")
        assert all(p.charge > 0 for p in row.periods[1:])

    def test_the_balance_rolls_forward_through_the_stub(self) -> None:
        for period in load_deal(ASHCROFT).fee_schedule().tranche("Term Loan B"):
            assert period.reconciles()


class TestTheWholeDealEndToEnd:
    """Ashcroft, which closes on 16 November and has forty-five days of stub."""

    def test_it_runs(self) -> None:
        deal = load_deal(ASHCROFT)
        schedule = deal.schedule()
        assert len(schedule) == 6
        assert schedule.is_funded

    def test_the_delayed_draw_amortises_against_what_was_drawn(self) -> None:
        # The other half of this layer, exercised on the same deal.
        schedule = load_deal(ASHCROFT).schedule()
        drawn = schedule[2].tranche("Delayed draw facility")
        assert drawn.acquisition == money(34)
        assert schedule[3].tranche("Delayed draw facility").amortisation_basis == money(34)
        assert schedule[3].tranche("Delayed draw facility").mandatory_repayment > 0

    def test_the_exit_falls_on_a_reporting_date(self) -> None:
        deal = load_deal(ASHCROFT)
        assert deal.grid is not None
        assert deal.grid.end == date(2031, 12, 31)

    def test_the_stub_column_is_labelled(self, capsys: pytest.CaptureFixture[str]) -> None:
        main(["schedule", ASHCROFT])
        out = capsys.readouterr().out
        assert "Stub" in out
        assert "P0" not in out

    def test_the_stub_has_no_leverage_reading(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["schedule", ASHCROFT, "--json"])
        report = json.loads(capsys.readouterr().out)
        assert report["periods"][0]["leverage"] is None
        assert report["periods"][1]["leverage"] is not None

    def test_the_memo_renders(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["report", ASHCROFT]) == 0
        assert "Project Ashcroft" in capsys.readouterr().out

    def test_every_subcommand_runs_on_it(self, capsys: pytest.CaptureFixture[str]) -> None:
        for command in (
            "deal",
            "balance",
            "project",
            "schedule",
            "covenants",
            "exit",
            "acquisitions",
            "fees",
            "report",
        ):
            assert main([command, ASHCROFT]) == 0, command
        capsys.readouterr()
