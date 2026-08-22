from datetime import date
from decimal import Decimal

import pytest

from capstack.drivers import Driver
from capstack.money import ZERO, money
from capstack.operating import (
    OperatingAssumptions,
    OperatingModel,
    apply_carryforward,
)
from capstack.periods import Frequency, PeriodGrid


def grid(years: int = 5) -> PeriodGrid:
    return PeriodGrid.build(date(2026, 6, 30), years=years, frequency=Frequency.ANNUAL)


def flat_case(
    years: int = 5,
    *,
    growth: str = "0.08",
    margin: str = "0.20",
    da: str = "0.04",
    capex: str = "0.05",
    nwc: str = "0.15",
    tax: str = "0.25",
    opening_carryforward: str = "0",
) -> OperatingAssumptions:
    return OperatingAssumptions.of(
        revenue_growth=Driver.constant(growth, years),
        ebitda_margin=Driver.constant(margin, years),
        da_rate=Driver.constant(da, years),
        capex_rate=Driver.constant(capex, years),
        nwc_rate=Driver.constant(nwc, years),
        tax_rate=tax,
        opening_carryforward=opening_carryforward,
    )


class TestCarryforward:
    """The pool, walked through a full cycle by hand at a 25% rate and an 80% cap."""

    def test_a_profit_with_no_pool_is_taxed_in_full(self) -> None:
        r = apply_carryforward(money(200), ZERO, money("0.25"))
        assert r.loss_relief_used == ZERO
        assert r.taxable_after_relief == money(200)
        assert r.cash_tax == money(50)
        assert r.closing_carryforward == ZERO

    def test_a_loss_pays_nothing_and_joins_the_pool(self) -> None:
        r = apply_carryforward(money(-30), money(60), money("0.25"))
        assert r.cash_tax == ZERO
        assert r.loss_relief_used == ZERO
        assert r.closing_carryforward == money(90)

    def test_the_cap_leaves_tax_payable_despite_a_large_pool(self) -> None:
        # 50 of profit against 100 of losses. Only 80% of the profit can be
        # sheltered, so 10 remains taxable and 2.50 is payable.
        r = apply_carryforward(money(50), money(100), money("0.25"))
        assert r.loss_relief_used == money(40)
        assert r.taxable_after_relief == money(10)
        assert r.cash_tax == money("2.50")
        assert r.closing_carryforward == money(60)

    def test_a_pool_smaller_than_the_cap_is_used_in_full(self) -> None:
        r = apply_carryforward(money(200), money(90), money("0.25"))
        assert r.loss_relief_used == money(90)
        assert r.taxable_after_relief == money(110)
        assert r.cash_tax == money("27.50")
        assert r.closing_carryforward == ZERO

    def test_without_a_cap_the_pool_shelters_everything_it_can(self) -> None:
        r = apply_carryforward(money(50), money(100), money("0.25"), usage_limit=money(1))
        assert r.loss_relief_used == money(50)
        assert r.cash_tax == ZERO
        assert r.closing_carryforward == money(50)

    def test_break_even_pays_nothing(self) -> None:
        r = apply_carryforward(ZERO, money(10), money("0.25"))
        assert r.cash_tax == ZERO
        assert r.closing_carryforward == money(10)

    def test_a_full_cycle(self) -> None:
        # Accumulate, shelter against the cap, exhaust, then pay in full.
        pool = money(100)
        steps = [
            (money(50), money("2.50"), money(60)),
            (money(-30), ZERO, money(90)),
            (money(200), money("27.50"), ZERO),
            (money(100), money(25), ZERO),
        ]
        for taxable, expected_tax, expected_pool in steps:
            r = apply_carryforward(taxable, pool, money("0.25"))
            assert r.cash_tax == expected_tax
            assert r.closing_carryforward == expected_pool
            pool = r.closing_carryforward

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"tax_rate": money("1.5")}, "tax rate"),
            ({"tax_rate": money("-0.1")}, "tax rate"),
            ({"usage_limit": money("1.5")}, "usage limit"),
            ({"opening_carryforward": money(-1)}, "must not be negative"),
        ],
    )
    def test_rejects_nonsense(self, kwargs: dict[str, Decimal], message: str) -> None:
        args = {
            "taxable_income": money(100),
            "opening_carryforward": ZERO,
            "tax_rate": money("0.25"),
            **kwargs,
        }
        limit = args.pop("usage_limit", None)
        with pytest.raises(ValueError, match=message):
            if limit is None:
                apply_carryforward(
                    args["taxable_income"], args["opening_carryforward"], args["tax_rate"]
                )
            else:
                apply_carryforward(
                    args["taxable_income"],
                    args["opening_carryforward"],
                    args["tax_rate"],
                    usage_limit=limit,
                )


class TestWorkedCase:
    """1,000 of revenue growing 8%, 20% margin, 4% D&A, 5% capex, 15% working
    capital, taxed at 25%. Computed by hand:

        Period 1   revenue 1,080.00   EBITDA 216.00   D&A 43.20   EBIT 172.80
                   tax 43.20   NOPAT 129.60   capex 54.00   NWC 162.00 (+12.00)
                   UFCF = 129.60 + 43.20 - 54.00 - 12.00 = 106.80

        Period 2   revenue 1,166.40   EBITDA 233.28   D&A 46.656  EBIT 186.624
                   tax 46.656  NOPAT 139.968  capex 58.32  NWC 174.96 (+12.96)
                   UFCF = 139.968 + 46.656 - 58.32 - 12.96 = 115.344
    """

    @pytest.fixture()
    def model(self) -> OperatingModel:
        return OperatingModel.project(grid(), flat_case(), opening_revenue=1000)

    def test_opening_working_capital_is_derived_from_opening_revenue(
        self, model: OperatingModel
    ) -> None:
        assert model.opening_net_working_capital == money(150)

    def test_period_one(self, model: OperatingModel) -> None:
        p = model[0]
        assert p.revenue == money("1080.00")
        assert p.ebitda == money("216.00")
        assert p.depreciation_and_amortisation == money("43.20")
        assert p.ebit == money("172.80")
        assert p.tax.cash_tax == money("43.20")
        assert p.nopat == money("129.60")
        assert p.capital_expenditure == money("54.00")
        assert p.net_working_capital == money("162.00")
        assert p.change_in_net_working_capital == money("12.00")
        assert p.unlevered_free_cash_flow == money("106.80")

    def test_period_two(self, model: OperatingModel) -> None:
        p = model[1]
        assert p.revenue == money("1166.40")
        assert p.ebitda == money("233.28")
        assert p.ebit == money("186.624")
        assert p.tax.cash_tax == money("46.656")
        assert p.capital_expenditure == money("58.32")
        assert p.change_in_net_working_capital == money("12.96")
        assert p.unlevered_free_cash_flow == money("115.344")

    def test_final_period_revenue_compounds(self, model: OperatingModel) -> None:
        # 1,000 x 1.08^5
        assert model[-1].revenue == money(1000) * money("1.08") ** 5
        assert round(float(model.exit_ebitda), 4) == 293.8656

    def test_margin_is_recovered_from_the_output(self, model: OperatingModel) -> None:
        assert all(p.ebitda_margin == money("0.20") for p in model)

    def test_the_grid_is_carried_through(self, model: OperatingModel) -> None:
        assert len(model) == 5
        assert [p.index for p in model] == [1, 2, 3, 4, 5]
        assert model[-1].period.end == date(2031, 6, 30)


class TestIdentities:
    def test_depreciation_reaches_cash_flow_only_through_tax(self) -> None:
        # NOPAT + D&A collapses to EBITDA - cash tax, so unlevered free cash
        # flow must equal EBITDA less tax, capex and the working-capital
        # movement. If this identity ever breaks, the add-back is wrong.
        model = OperatingModel.project(grid(), flat_case(), opening_revenue=1000)
        for p in model:
            assert p.unlevered_free_cash_flow == (
                p.ebitda
                - p.tax.cash_tax
                - p.capital_expenditure
                - p.change_in_net_working_capital
            )

    def test_a_higher_depreciation_rate_raises_cash_flow_via_the_shield(self) -> None:
        # More depreciation means less taxable income and so less cash tax,
        # with no other cash effect.
        low = OperatingModel.project(grid(), flat_case(da="0.04"), opening_revenue=1000)
        high = OperatingModel.project(grid(), flat_case(da="0.10"), opening_revenue=1000)
        assert high[0].unlevered_free_cash_flow > low[0].unlevered_free_cash_flow
        shield = (high[0].ebit - low[0].ebit) * money("-0.25")
        assert (
            high[0].unlevered_free_cash_flow - low[0].unlevered_free_cash_flow == shield
        )

    def test_working_capital_movements_sum_to_the_change_in_the_balance(self) -> None:
        model = OperatingModel.project(grid(), flat_case(), opening_revenue=1000)
        assert model.working_capital_absorbed == (
            model[-1].net_working_capital - model.opening_net_working_capital
        )


class TestWorkingCapital:
    def test_a_growing_business_consumes_cash(self) -> None:
        model = OperatingModel.project(
            grid(), flat_case(growth="0.08"), opening_revenue=1000
        )
        assert all(p.change_in_net_working_capital > 0 for p in model)
        assert model.working_capital_absorbed > 0

    def test_a_shrinking_business_releases_cash(self) -> None:
        model = OperatingModel.project(
            grid(), flat_case(growth="-0.10"), opening_revenue=1000
        )
        assert all(p.change_in_net_working_capital < 0 for p in model)
        assert model.working_capital_absorbed < 0

    def test_a_flat_business_moves_no_working_capital(self) -> None:
        model = OperatingModel.project(
            grid(), flat_case(growth="0"), opening_revenue=1000
        )
        assert all(p.change_in_net_working_capital == ZERO for p in model)

    def test_the_ratio_can_be_steady_while_cash_is_consumed(self) -> None:
        # The point of the whole exercise: a constant 15% of revenue still
        # absorbs cash every period when revenue is rising.
        model = OperatingModel.project(grid(), flat_case(), opening_revenue=1000)
        ratios = {p.net_working_capital / p.revenue for p in model}
        assert ratios == {money("0.15")}
        assert model.working_capital_absorbed > 0

    def test_an_explicit_opening_balance_is_used(self) -> None:
        model = OperatingModel.project(
            grid(), flat_case(), opening_revenue=1000, opening_net_working_capital=0
        )
        assert model.opening_net_working_capital == ZERO
        # The whole balance now lands in period one.
        assert model[0].change_in_net_working_capital == money("162.00")

    def test_a_higher_intensity_business_converts_less_of_its_ebitda(self) -> None:
        light = OperatingModel.project(
            grid(), flat_case(capex="0.03"), opening_revenue=1000
        )
        heavy = OperatingModel.project(
            grid(), flat_case(capex="0.09"), opening_revenue=1000
        )
        assert heavy[0].cash_conversion < light[0].cash_conversion


class TestLossMaking:
    def test_a_loss_year_pays_no_tax_and_builds_the_pool(self) -> None:
        # Margin below the depreciation rate makes EBIT negative.
        model = OperatingModel.project(
            grid(), flat_case(margin="0.02", da="0.04"), opening_revenue=1000
        )
        assert all(p.ebit < 0 for p in model)
        assert model.total_cash_tax == ZERO
        assert model.closing_carryforward > 0

    def test_an_opening_pool_shelters_early_years(self) -> None:
        without = OperatingModel.project(grid(), flat_case(), opening_revenue=1000)
        with_pool = OperatingModel.project(
            grid(), flat_case(opening_carryforward="500"), opening_revenue=1000
        )
        assert with_pool.total_cash_tax < without.total_cash_tax
        assert with_pool.total_unlevered_free_cash_flow > without.total_unlevered_free_cash_flow

    def test_the_cap_means_a_sheltered_year_still_pays_something(self) -> None:
        model = OperatingModel.project(
            grid(), flat_case(opening_carryforward="10000"), opening_revenue=1000
        )
        # The pool dwarfs the profits, but 20% of each year is still taxable.
        assert model[0].tax.cash_tax > ZERO
        assert model[0].tax.cash_tax == model[0].ebit * money("0.20") * money("0.25")

    def test_a_recovering_business_draws_down_its_pool(self) -> None:
        # Two loss years, then three profitable ones.
        years = 5
        assumptions = OperatingAssumptions.of(
            revenue_growth=Driver.constant("0.05", years),
            ebitda_margin=Driver.of(["0.01", "0.02", "0.12", "0.18", "0.22"]),
            da_rate=Driver.constant("0.05", years),
            capex_rate=Driver.constant("0.04", years),
            nwc_rate=Driver.constant("0.12", years),
            tax_rate="0.25",
        )
        model = OperatingModel.project(grid(years), assumptions, opening_revenue=1000)
        pools = [p.tax.closing_carryforward for p in model]

        # Losses accumulate across the first two years...
        assert pools[0] > 0
        assert pools[1] > pools[0]
        # ...then the pool is drawn down and exhausted, and stays exhausted.
        assert pools[2] < pools[1]
        assert pools[3] == ZERO
        assert pools[4] == ZERO
        assert pools == sorted(pools[:2]) + sorted(pools[2:], reverse=True)

        # No tax while loss-making; tax once the pool runs out.
        assert model[0].tax.cash_tax == ZERO
        assert model[1].tax.cash_tax == ZERO
        assert model[2].tax.cash_tax > ZERO  # the 80% cap bites before the pool empties
        assert model[-1].tax.cash_tax > ZERO
        assert model[2].tax.loss_relief_used > ZERO
        assert model[4].tax.loss_relief_used == ZERO


class TestDriverShapes:
    def test_a_tapering_growth_case(self) -> None:
        years = 5
        assumptions = OperatingAssumptions.of(
            revenue_growth=Driver.ramp("0.09", "0.03", years),
            ebitda_margin=Driver.ramp("0.18", "0.22", years),
            da_rate=Driver.constant("0.04", years),
            capex_rate=Driver.constant("0.05", years),
            nwc_rate=Driver.constant("0.15", years),
            tax_rate="0.25",
        )
        model = OperatingModel.project(grid(years), assumptions, opening_revenue=1000)
        assert model[0].revenue == money(1090)
        assert model[0].ebitda_margin == money("0.18")
        assert model[-1].ebitda_margin == money("0.22")
        # Growth slows but revenue still rises every year.
        revenues = [p.revenue for p in model]
        assert revenues == sorted(revenues)

    def test_a_short_driver_holds_its_final_value(self) -> None:
        years = 5
        assumptions = OperatingAssumptions.of(
            revenue_growth=Driver.of(["0.10", "0.08"]),  # only two years supplied
            ebitda_margin=Driver.constant("0.20", years),
            da_rate=Driver.constant("0.04", years),
            capex_rate=Driver.constant("0.05", years),
            nwc_rate=Driver.constant("0.15", years),
            tax_rate="0.25",
        )
        model = OperatingModel.project(grid(years), assumptions, opening_revenue=1000)
        assert model[0].revenue == money(1100)
        assert model[2].revenue == model[1].revenue * money("1.08")


class TestValidation:
    def test_negative_opening_revenue_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="opening revenue"):
            OperatingModel.project(grid(), flat_case(), opening_revenue=-1)

    def test_a_tax_rate_above_one_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="tax rate"):
            flat_case(tax="1.5")

    def test_a_negative_opening_pool_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="opening carryforward"):
            flat_case(opening_carryforward="-1")

    def test_zero_revenue_does_not_divide_by_zero(self) -> None:
        model = OperatingModel.project(grid(), flat_case(), opening_revenue=0)
        assert model[0].revenue == ZERO
        assert model[0].ebitda_margin == ZERO
        assert model[0].cash_conversion == ZERO


class TestAggregates:
    def test_totals(self) -> None:
        model = OperatingModel.project(grid(), flat_case(), opening_revenue=1000)
        assert model.total_unlevered_free_cash_flow == sum(
            (p.unlevered_free_cash_flow for p in model), ZERO
        )
        assert model.total_cash_tax == sum((p.tax.cash_tax for p in model), ZERO)
        assert model.total_capital_expenditure == sum(
            (p.capital_expenditure for p in model), ZERO
        )

    def test_entry_and_exit_ebitda(self) -> None:
        model = OperatingModel.project(grid(), flat_case(), opening_revenue=1000)
        assert model.entry_ebitda == model[0].ebitda
        assert model.exit_ebitda == model[-1].ebitda
        assert model.exit_ebitda > model.entry_ebitda
