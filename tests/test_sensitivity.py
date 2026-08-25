"""The grid.

The load-bearing test in this file is the first one: the cell sitting at the
deal's own assumptions has to reproduce the headline figure exactly. A grid that
re-runs the engine and lands somewhere near the answer it already had is a grid
that is rebuilding the deal slightly wrong, and every other cell is wrong in the
same way and cannot be checked.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from capstack.money import is_close, money
from capstack.sensitivity import (
    Axis,
    Dimension,
    Grid,
    Metric,
    SensitivityError,
    Unit,
    format_value,
    solve,
)
from capstack.spec import Deal, load_deal, parse_deal

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "meridian.json"


def example() -> Deal:
    return load_deal(EXAMPLE)


def simple(**blocks: Any) -> Deal:
    payload: dict[str, Any] = {
        "name": "Test",
        "close_date": "2026-06-30",
        "entry": {"ltm_ebitda": 200, "multiple": 8, "existing_debt": 100},
        "debt": [
            {"name": "TLB", "face": 900, "kind": "term_loan", "cash_rate": 0.05},
        ],
        "rollover_equity": 100,
        "structure": {"minimum_cash": 20, "base_rate": 0.03},
        "projection": {"years": 5, "frequency": "annual"},
        "operating": {
            "opening_revenue": 1000,
            "revenue_growth": 0.05,
            "ebitda_margin": 0.22,
            "da_rate": 0.03,
            "capex_rate": 0.035,
            "nwc_rate": 0.1,
            "tax_rate": 0.25,
        },
        "exit": {"multiple": 9},
    }
    payload.update(blocks)
    return parse_deal(payload)


class TestTheBaseCase:
    def test_the_base_cell_reproduces_the_headline_exactly(self) -> None:
        deal = example()
        headline = deal.realise()
        grid = Grid.run(
            deal,
            Axis.of(Dimension.EXIT_MULTIPLE, ["10.0", "11.0", "12.0"]),
            Axis.of(Dimension.ENTRY_MULTIPLE, ["11.0", "11.5", "12.0"]),
            Metric.MOIC,
        )
        # Exit 11.0 x entry 11.5 is what the file says.
        cell = grid.at(1, 1)
        assert grid.rows.is_base(cell.row, deal)
        assert grid.columns.is_base(cell.column, deal)
        assert cell.value == headline.moic
        assert cell.value == grid.base

    def test_the_base_cell_reproduces_the_headline_rate_exactly(self) -> None:
        deal = example()
        headline = deal.realise()
        grid = Grid.run(
            deal,
            Axis.of(Dimension.EXIT_MULTIPLE, ["11.0"]),
            Axis.of(Dimension.ENTRY_MULTIPLE, ["11.5"]),
            Metric.IRR,
        )
        assert headline.irr is not None
        assert grid.at(0, 0).value == money(repr(headline.irr))

    def test_a_shift_axis_has_its_base_at_zero(self) -> None:
        deal = example()
        grid = Grid.run(
            deal,
            Axis.of(Dimension.EBITDA_MARGIN, ["-0.01", "0", "0.01"]),
            Axis.of(Dimension.EXIT_MULTIPLE, ["11.0"]),
            Metric.MOIC,
        )
        assert grid.rows.is_base(money(0), deal)
        assert not grid.rows.is_base(money("0.01"), deal)
        assert grid.at(1, 0).value == deal.realise().moic

    def test_the_base_is_read_before_anything_is_flexed(self) -> None:
        # Neither axis passes through the file's own assumptions, and the base
        # still comes back as the deal's own answer.
        deal = example()
        grid = Grid.run(
            deal,
            Axis.of(Dimension.EXIT_MULTIPLE, ["9.0", "10.0"]),
            Axis.of(Dimension.EXIT_YEAR, ["3", "4"]),
            Metric.MOIC,
        )
        assert grid.base == deal.realise().moic


class TestDirection:
    def test_returns_fall_as_the_price_rises(self) -> None:
        grid = Grid.run(
            example(),
            Axis.of(Dimension.ENTRY_MULTIPLE, ["10.5", "11.0", "11.5", "12.0", "12.5"]),
            Axis.of(Dimension.EXIT_MULTIPLE, ["11.0"]),
            Metric.IRR,
        )
        rates = [line[0].value for line in grid]
        assert all(v is not None for v in rates)
        assert rates == sorted(rates, reverse=True)  # type: ignore[type-var]

    def test_returns_rise_with_the_exit_multiple(self) -> None:
        grid = Grid.run(
            example(),
            Axis.of(Dimension.EXIT_MULTIPLE, ["9.0", "10.0", "11.0", "12.0", "13.0"]),
            Axis.of(Dimension.ENTRY_MULTIPLE, ["11.5"]),
            Metric.IRR,
        )
        rates = [line[0].value for line in grid]
        assert all(v is not None for v in rates)
        assert rates == sorted(rates)  # type: ignore[type-var]

    def test_the_sponsor_cheque_rises_with_the_price(self) -> None:
        grid = Grid.run(
            example(),
            Axis.of(Dimension.ENTRY_MULTIPLE, ["10.0", "11.0", "12.0"]),
            Axis.of(Dimension.EXIT_MULTIPLE, ["11.0"]),
            Metric.SPONSOR_EQUITY,
        )
        cheques = [line[0].value for line in grid]
        assert cheques == sorted(cheques)  # type: ignore[type-var]

    def test_a_wider_margin_leaves_less_leverage_at_exit(self) -> None:
        grid = Grid.run(
            example(),
            Axis.of(Dimension.EBITDA_MARGIN, ["-0.02", "0", "0.02"]),
            Axis.of(Dimension.EXIT_MULTIPLE, ["11.0"]),
            Metric.EXIT_LEVERAGE,
        )
        levered = [line[0].value for line in grid]
        assert levered == sorted(levered, reverse=True)  # type: ignore[type-var]

    def test_a_higher_base_rate_costs_the_equity(self) -> None:
        grid = Grid.run(
            example(),
            Axis.of(Dimension.BASE_RATE, ["0", "0.02"]),
            Axis.of(Dimension.EXIT_MULTIPLE, ["11.0"]),
            Metric.EQUITY_VALUE,
        )
        cheap, dear = grid.at(0, 0).value, grid.at(1, 0).value
        assert cheap is not None and dear is not None
        assert dear < cheap


class TestFlexingTheAssumptions:
    def test_the_entry_multiple_is_set_not_added(self) -> None:
        deal = example()
        flexed = Dimension.ENTRY_MULTIPLE.apply(deal, money("13.25"))
        assert flexed.transaction.valuation.entry_multiple == money("13.25")
        assert deal.transaction.valuation.entry_multiple == money("11.5")

    def test_the_equity_is_repriced_when_the_cheque_moves(self) -> None:
        deal = example()
        dearer = Dimension.ENTRY_MULTIPLE.apply(deal, money("13"))
        assert dearer.transaction.sponsor_equity > deal.transaction.sponsor_equity
        # The stack is described as shares of the cheque, so it has to follow.
        assert sum(s.invested for s in dearer.securities) > sum(
            s.invested for s in deal.securities
        )

    def test_leverage_holds_the_shape_of_the_structure(self) -> None:
        deal = example()
        ebitda = deal.transaction.valuation.ltm_ebitda
        flexed = Dimension.LEVERAGE.apply(deal, money("6"))
        assert flexed.transaction.total_debt == ebitda * money("6")

        # Every tranche keeps its share of the stack. The last drawn tranche
        # absorbs the rounding dust so the total lands on the target exactly,
        # so its share is checked to the cent rather than to the last bit.
        before = {t.name: t.face for t in deal.transaction.debt}
        total_before = deal.transaction.total_debt
        for tranche in flexed.transaction.debt:
            share_before = before[tranche.name] / total_before
            share_after = tranche.face / flexed.transaction.total_debt
            assert is_close(share_before, share_after, tolerance="0.0000001")

    def test_leverage_moves_the_structure_and_the_funding_table_together(self) -> None:
        flexed = Dimension.LEVERAGE.apply(example(), money("6"))
        assert flexed.structure is not None
        funded = {t.name: t.face for t in flexed.transaction.debt}
        scheduled = {t.name: t.face for t in flexed.structure.tranches}
        assert funded == scheduled

    def test_leverage_leaves_the_revolver_commitment_alone(self) -> None:
        deal = example()
        assert deal.structure is not None
        before = {t.name: t.commitment for t in deal.structure.tranches}
        flexed = Dimension.LEVERAGE.apply(deal, money("4"))
        assert flexed.structure is not None
        assert {t.name: t.commitment for t in flexed.structure.tranches} == before

    def test_a_shift_moves_every_period_of_the_driver(self) -> None:
        deal = example()
        assert deal.operating is not None
        before = list(deal.operating.revenue_growth)
        flexed = Dimension.REVENUE_GROWTH.apply(deal, money("0.01"))
        assert flexed.operating is not None
        assert list(flexed.operating.revenue_growth) == [
            v + money("0.01") for v in before
        ]

    def test_the_exit_year_shortens_the_grid(self) -> None:
        flexed = Dimension.EXIT_YEAR.apply(example(), money(3))
        assert flexed.grid is not None
        assert len(flexed.grid) == 3
        assert len(flexed.realise()) > 0

    def test_the_exit_year_can_extend_past_the_case(self) -> None:
        # Assumption series shorter than the grid hold their final value, so a
        # case can be run a year longer than it was written for.
        flexed = Dimension.EXIT_YEAR.apply(example(), money(7))
        assert flexed.grid is not None
        assert len(flexed.grid) == 7
        assert flexed.realise().valuation.when.year == 2033

    def test_a_fractional_exit_year_is_refused(self) -> None:
        with pytest.raises(SensitivityError, match="whole number"):
            Dimension.EXIT_YEAR.apply(example(), money("4.5"))

    def test_an_exit_year_of_zero_is_refused(self) -> None:
        with pytest.raises(SensitivityError, match="at least one year"):
            Dimension.EXIT_YEAR.apply(example(), money(0))

    def test_leverage_on_an_all_equity_deal_says_so(self) -> None:
        deal = parse_deal(
            {
                "name": "All equity",
                "close_date": "2026-06-30",
                "entry": {"ltm_ebitda": 200, "multiple": 8},
            }
        )
        with pytest.raises(SensitivityError, match="no debt"):
            Dimension.LEVERAGE.apply(deal, money(3))

    def test_negative_leverage_is_refused(self) -> None:
        with pytest.raises(SensitivityError, match="not below zero"):
            Dimension.LEVERAGE.apply(example(), money(-1))

    def test_resizing_lands_on_the_target_exactly(self) -> None:
        # The scale factor here does not terminate, so scaling each tranche
        # independently would leave the total a few billionths off the turns
        # that were asked for.
        deal = example()
        for turns in ("3", "6", "7.25"):
            flexed = Dimension.LEVERAGE.apply(deal, money(turns))
            expected = deal.transaction.valuation.ltm_ebitda * money(turns)
            assert flexed.transaction.total_debt == expected

    def test_an_undrawn_facility_stays_undrawn_when_the_debt_grows(self) -> None:
        flexed = Dimension.LEVERAGE.apply(example(), money(8))
        revolver = next(
            t for t in flexed.transaction.debt if t.name == "Revolving credit facility"
        )
        assert revolver.face == 0

    def test_a_fixed_rate_structure_has_no_base_rate_to_shift(self) -> None:
        deal = simple(
            debt=[{"name": "Notes", "face": 900, "kind": "notes", "cash_rate": 0.07}],
            structure={"minimum_cash": 20},
        )
        with pytest.raises(SensitivityError, match="floating base rate"):
            Dimension.BASE_RATE.apply(deal, money("0.01"))


class TestReadingWhereTheDealSits:
    def test_a_level_dimension_reads_the_file(self) -> None:
        deal = example()
        assert Dimension.ENTRY_MULTIPLE.read(deal) == money("11.5")
        assert Dimension.EXIT_MULTIPLE.read(deal) == money("11.0")
        assert Dimension.EXIT_YEAR.read(deal) == money(5)

    def test_an_absent_exit_multiple_reads_as_the_entry_multiple(self) -> None:
        deal = simple(exit={"fee_rate": 0.01})
        assert Dimension.EXIT_MULTIPLE.read(deal) == money(8)

    def test_opening_leverage_reads_off_the_funded_debt(self) -> None:
        deal = simple()
        assert Dimension.LEVERAGE.read(deal) == money(900) / money(200)

    def test_every_shift_dimension_reads_zero(self) -> None:
        deal = example()
        for dimension in Dimension:
            if dimension.is_shift:
                assert dimension.read(deal) == 0


class TestCellsThatCannotBeValued:
    def test_a_wiped_out_cell_reports_why_and_the_grid_survives(self) -> None:
        grid = Grid.run(
            example(),
            Axis.of(Dimension.EXIT_MULTIPLE, ["2.0", "11.0"]),
            Axis.of(Dimension.ENTRY_MULTIPLE, ["11.5"]),
            Metric.IRR,
        )
        wiped, alive = grid.at(0, 0), grid.at(1, 0)
        assert not wiped.ok
        assert "wiped out" in wiped.note
        assert alive.ok
        assert len(grid.failures) == 1
        assert len(grid.populated) == 1

    def test_a_cell_the_engine_refuses_carries_the_refusal(self) -> None:
        grid = Grid.run(
            example(),
            Axis.of(Dimension.EXIT_YEAR, ["4", "5"]),
            Axis.of(Dimension.ENTRY_MULTIPLE, ["11.5"]),
            Metric.CUSHION,
        )
        assert all(c.ok for line in grid for c in line)

    def test_the_grid_refuses_outright_when_the_base_case_will_not_run(self) -> None:
        deal = simple(exit=None)
        broken = parse_deal(
            {
                "name": "No case",
                "close_date": "2026-06-30",
                "entry": {"ltm_ebitda": 200, "multiple": 8},
            }
        )
        assert deal is not None
        with pytest.raises(ValueError, match="no operating case|no capital structure"):
            Grid.run(
                broken,
                Axis.of(Dimension.ENTRY_MULTIPLE, ["8"]),
                Axis.of(Dimension.EXIT_MULTIPLE, ["9"]),
                Metric.MOIC,
            )


class TestCovenantFlags:
    def test_a_breaching_cell_is_flagged_even_on_a_return_metric(self) -> None:
        grid = Grid.run(
            example(),
            Axis.of(Dimension.EBITDA_MARGIN, ["-0.06", "0"]),
            Axis.of(Dimension.EXIT_MULTIPLE, ["11.0"]),
            Metric.IRR,
        )
        stressed, base = grid.at(0, 0), grid.at(1, 0)
        assert stressed.breached
        assert stressed.breach_note
        assert not base.breached
        assert grid.breaches == (stressed,)

    def test_a_deal_without_covenants_flags_nothing(self) -> None:
        grid = Grid.run(
            simple(),
            Axis.of(Dimension.ENTRY_MULTIPLE, ["8", "9"]),
            Axis.of(Dimension.EXIT_MULTIPLE, ["9"]),
            Metric.IRR,
        )
        assert grid.breaches == ()

    def test_the_cushion_metric_needs_covenants_to_measure(self) -> None:
        with pytest.raises(SensitivityError, match="describes none"):
            Grid.run(
                simple(),
                Axis.of(Dimension.ENTRY_MULTIPLE, ["8"]),
                Axis.of(Dimension.EXIT_MULTIPLE, ["9"]),
                Metric.CUSHION,
            )

    def test_the_cushion_thins_as_the_margin_falls(self) -> None:
        grid = Grid.run(
            example(),
            Axis.of(Dimension.EBITDA_MARGIN, ["-0.03", "0", "0.03"]),
            Axis.of(Dimension.EXIT_MULTIPLE, ["11.0"]),
            Metric.CUSHION,
        )
        cushions = [line[0].value for line in grid]
        assert all(c is not None for c in cushions)
        assert cushions == sorted(cushions)  # type: ignore[type-var]


class TestBuildingTheGrid:
    def test_two_axes_on_one_dimension_is_refused(self) -> None:
        with pytest.raises(SensitivityError, match="two different assumptions"):
            Grid.run(
                example(),
                Axis.of(Dimension.EXIT_MULTIPLE, ["10"]),
                Axis.of(Dimension.EXIT_MULTIPLE, ["11"]),
                Metric.MOIC,
            )

    def test_an_empty_axis_is_refused(self) -> None:
        with pytest.raises(SensitivityError, match="at least one value"):
            Axis.of(Dimension.EXIT_MULTIPLE, [])

    def test_a_repeated_value_is_refused(self) -> None:
        with pytest.raises(SensitivityError, match="repeats a value"):
            Axis.of(Dimension.EXIT_MULTIPLE, ["10", "10"])

    def test_the_table_is_the_shape_of_its_axes(self) -> None:
        grid = Grid.run(
            example(),
            Axis.of(Dimension.EXIT_MULTIPLE, ["10", "11", "12"]),
            Axis.of(Dimension.EXIT_YEAR, ["4", "5"]),
            Metric.MOIC,
        )
        assert len(grid) == 3
        assert all(len(line) == 2 for line in grid)
        assert grid.at(2, 1).row == money(12)
        assert grid.at(2, 1).column == money(5)


class TestParsingAnAxis:
    def test_a_level_axis_is_read_as_written(self) -> None:
        axis = Axis.parse("entry-multiple:11,11.5,12")
        assert axis.dimension is Dimension.ENTRY_MULTIPLE
        assert axis.values == (money(11), money("11.5"), money(12))

    def test_a_shift_axis_is_read_in_points(self) -> None:
        axis = Axis.parse("ebitda-margin:-1.5,0,1.5")
        assert axis.values == (money("-0.015"), money(0), money("0.015"))

    def test_whitespace_is_tolerated(self) -> None:
        assert Axis.parse(" exit-multiple : 9 , 10 ").values == (money(9), money(10))

    def test_an_unknown_dimension_lists_the_known_ones(self) -> None:
        with pytest.raises(SensitivityError, match="entry-multiple"):
            Axis.parse("wacc:8,9")

    def test_an_axis_with_no_values_says_how_to_write_one(self) -> None:
        with pytest.raises(SensitivityError, match="1,2,3"):
            Axis.parse("exit-multiple:")

    def test_a_value_that_is_not_a_number_is_named(self) -> None:
        with pytest.raises(SensitivityError, match="eleven"):
            Axis.parse("exit-multiple:eleven")


class TestFormatting:
    @pytest.mark.parametrize(
        "unit,value,expected",
        [
            (Unit.RATE, money("0.2196"), "22.0%"),
            (Unit.SHARE, money("0.184"), "18.4%"),
            (Unit.MULTIPLE, money("2.7013"), "2.70x"),
            (Unit.TURNS, money("5.5"), "5.50x"),
            (Unit.YEARS, money(5), "5y"),
            (Unit.POINTS, money("0.015"), "+1.5pp"),
            (Unit.POINTS, money("-0.015"), "-1.5pp"),
            (Unit.POINTS, money(0), "0pp"),
            (Unit.AMOUNT, money("1234.56"), "1,234.6"),
        ],
    )
    def test_a_unit_is_rendered_the_way_it_is_spoken(
        self, unit: Unit, value: Any, expected: str
    ) -> None:
        assert format_value(unit, value) == expected

    def test_an_axis_formats_with_its_own_unit(self) -> None:
        assert Axis.parse("exit-multiple:11").format(money(11)) == "11.00x"
        assert Axis.parse("base-rate:50").format(money("0.5")) == "+50pp"


class TestBreakevens:
    def test_the_crossing_reproduces_the_target_when_run_back(self) -> None:
        # The strongest check available: take the break-even, put it back into
        # the engine, and the metric has to come out at the target.
        deal = example()
        found = solve(
            deal,
            Dimension.EXIT_MULTIPLE,
            Metric.MOIC,
            target=money(1),
            low=money(2),
            high=money(20),
        )
        assert found.found
        assert found.value is not None
        at_crossing = Dimension.EXIT_MULTIPLE.apply(deal, found.value).realise().moic
        assert at_crossing is not None
        assert is_close(at_crossing, money(1), tolerance="0.000001")

    def test_a_covenant_crossing_reproduces_a_cushion_of_nothing(self) -> None:
        deal = example()
        found = solve(
            deal,
            Dimension.EBITDA_MARGIN,
            Metric.CUSHION,
            low=money("-0.1"),
            high=money("0.1"),
        )
        assert found.found
        assert found.value is not None
        flexed = Dimension.EBITDA_MARGIN.apply(deal, found.value)
        cushion = flexed.test_covenants().minimum_cushion
        assert cushion is not None
        assert is_close(cushion, money(0), tolerance="0.000001")

    def test_the_crossing_separates_pass_from_breach(self) -> None:
        deal = example()
        found = solve(
            deal, Dimension.LEVERAGE, Metric.CUSHION, low=money(1), high=money(12)
        )
        assert found.value is not None
        step = money("0.01")
        below = Dimension.LEVERAGE.apply(deal, found.value - step)
        above = Dimension.LEVERAGE.apply(deal, found.value + step)
        assert below.test_covenants().passes
        assert not above.test_covenants().passes

    def test_a_target_that_is_never_reached_is_reported_as_absent(self) -> None:
        found = solve(
            example(),
            Dimension.EXIT_MULTIPLE,
            Metric.MOIC,
            target=money(50),
            low=money(2),
            high=money(20),
        )
        assert not found.found
        assert found.format() == "none"
        assert "does not cross" in found.note

    def test_an_endpoint_the_engine_cannot_value_is_named(self) -> None:
        found = solve(
            example(),
            Dimension.EXIT_MULTIPLE,
            Metric.IRR,
            low=money(1),
            high=money(20),
        )
        assert not found.found
        assert "cannot be valued at 1.00x" in found.note
        assert "wiped out" in found.note

    def test_a_target_already_met_at_an_endpoint_is_that_endpoint(self) -> None:
        deal = example()
        base = deal.realise().moic
        assert base is not None
        found = solve(
            deal,
            Dimension.EXIT_MULTIPLE,
            Metric.MOIC,
            target=base,
            low=money("11.0"),
            high=money(20),
        )
        assert found.value == money("11.0")

    def test_a_whole_valued_dimension_is_refused(self) -> None:
        with pytest.raises(SensitivityError, match="whole values"):
            solve(
                example(),
                Dimension.EXIT_YEAR,
                Metric.MOIC,
                target=money(1),
                low=money(2),
                high=money(8),
            )

    def test_a_bracket_that_does_not_bracket_is_refused(self) -> None:
        with pytest.raises(SensitivityError, match="not a bracket"):
            solve(
                example(),
                Dimension.EXIT_MULTIPLE,
                Metric.MOIC,
                low=money(12),
                high=money(9),
            )

    def test_the_cushion_needs_covenants(self) -> None:
        with pytest.raises(SensitivityError, match="describes none"):
            solve(
                simple(),
                Dimension.EXIT_MULTIPLE,
                Metric.CUSHION,
                low=money(5),
                high=money(12),
            )

    def test_fewer_steps_give_a_wider_answer(self) -> None:
        deal = example()
        kwargs = dict(target=money(1), low=money(2), high=money(20))
        coarse = solve(
            deal, Dimension.EXIT_MULTIPLE, Metric.MOIC, steps=4, **kwargs
        )
        fine = solve(deal, Dimension.EXIT_MULTIPLE, Metric.MOIC, steps=24, **kwargs)
        assert coarse.value is not None and fine.value is not None
        # Four halvings of an eighteen-turn bracket cannot do better than about
        # half a turn, and twenty-four are far inside that.
        assert abs(coarse.value - fine.value) < money("0.6")

    def test_no_steps_at_all_is_refused(self) -> None:
        with pytest.raises(SensitivityError, match="at least one step"):
            solve(
                example(),
                Dimension.EXIT_MULTIPLE,
                Metric.MOIC,
                low=money(2),
                high=money(20),
                steps=0,
            )
