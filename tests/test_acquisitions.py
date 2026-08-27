"""Buying a business during the hold, and what it does to the entry multiple."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from capstack.debt import CapitalStructure, DebtSchedule, Tranche, TrancheKind
from capstack.drivers import Driver
from capstack.events import AddOn, AddOnError, BlendedEntry, Draw
from capstack.money import ZERO, is_close, money
from capstack.operating import (
    AcquiredStream,
    OperatingAssumptions,
    OperatingModel,
)
from capstack.periods import Frequency, PeriodGrid

CLOSE = date(2026, 6, 30)


def grid(years: int = 5) -> PeriodGrid:
    return PeriodGrid.build(CLOSE, years=years, frequency=Frequency.ANNUAL)


def assumptions(
    *,
    growth: str = "0.05",
    margin: str = "0.20",
    periods: int = 5,
) -> OperatingAssumptions:
    return OperatingAssumptions.of(
        revenue_growth=Driver.constant(growth, periods),
        ebitda_margin=Driver.constant(margin, periods),
        da_rate=Driver.constant("0.03", periods),
        capex_rate=Driver.constant("0.03", periods),
        nwc_rate=Driver.constant("0.08", periods),
        tax_rate="0.25",
    )


def structure(minimum_cash: int = 20) -> CapitalStructure:
    return CapitalStructure.of(
        [
            Tranche.of("TLB", TrancheKind.TERM_LOAN, 400, cash_rate="0.06", floating=False),
            Tranche.of(
                "Acquisition facility",
                TrancheKind.TERM_LOAN,
                0,
                cash_rate="0.07",
                floating=False,
            ),
        ],
        minimum_cash=minimum_cash,
    )


def run(
    purchases: list[AddOn] | None = None,
    *,
    flows: int = 90,
    opening_cash: int = 80,
    minimum_cash: int = 20,
) -> DebtSchedule:
    periods = list(grid())
    return DebtSchedule.run(
        structure(minimum_cash),
        periods,
        [money(flows)] * len(periods),
        opening_cash=opening_cash,
        acquisitions=purchases or [],
    )


# --------------------------------------------------------------------------
# The purchase, priced on its own
# --------------------------------------------------------------------------


class TestThePurchase:
    def test_the_price_is_the_multiple_applied_to_the_earnings(self) -> None:
        add_on = AddOn.of(2, ebitda=12, multiple="6.5")
        assert add_on.enterprise_value == money(78)

    def test_fees_are_quoted_on_enterprise_value(self) -> None:
        add_on = AddOn.of(2, ebitda=12, multiple="6.5", fee_rate="0.02")
        assert add_on.fees == money("1.56")
        assert add_on.uses == money("79.56")

    def test_integration_cost_joins_the_uses_rather_than_the_earnings(self) -> None:
        add_on = AddOn.of(2, ebitda=12, multiple="6.5", integration_cost=4)
        assert add_on.uses == money(82)
        # It never reaches the multiple, which is the point of putting it here:
        # a one-off charged to EBITDA would be valued at the exit multiple.
        assert add_on.enterprise_value == money(78)

    def test_cash_is_the_plug_for_whatever_the_draws_do_not_raise(self) -> None:
        add_on = AddOn.of(
            2,
            ebitda=12,
            multiple="6.5",
            draws=[Draw.of("Acquisition facility", 60, issue_price="0.99")],
        )
        assert add_on.debt_proceeds == money("59.4")
        assert add_on.from_cash == money("18.6")

    def test_over_funding_leaves_the_surplus_on_the_balance_sheet(self) -> None:
        add_on = AddOn.of(
            2, ebitda=10, multiple=5, draws=[Draw.of("Acquisition facility", 70)]
        )
        assert add_on.from_cash == money(-20)

    def test_capital_deployed_counts_the_cost_of_the_debt_raised(self) -> None:
        add_on = AddOn.of(
            2,
            ebitda=10,
            multiple=5,
            draws=[
                Draw.of(
                    "Acquisition facility", 40, issue_price="0.98", financing_fee_rate="0.02"
                )
            ],
        )
        # 50 of price, plus 0.8 of discount and 0.8 of financing fee.
        assert add_on.total_cost == money("51.6")

    def test_the_synergised_multiple_credits_earnings_not_yet_earned(self) -> None:
        add_on = AddOn.of(2, ebitda=10, multiple=7, synergies=4)
        assert add_on.enterprise_value == money(70)
        assert add_on.synergised_multiple == money(5)

    def test_a_stated_revenue_fixes_the_margin(self) -> None:
        add_on = AddOn.of(2, ebitda=12, multiple=6, revenue=80)
        assert add_on.implied_margin == money("0.15")

    def test_an_unstated_revenue_carries_the_platform_margin(self) -> None:
        stream = AddOn.of(2, ebitda=12, multiple=6).stream(money("0.20"))
        assert stream.margin == money("0.20")
        assert stream.revenue == money(60)

    def test_earnings_of_nothing_are_not_bought_at_a_multiple(self) -> None:
        with pytest.raises(AddOnError, match="purchase of losses"):
            AddOn.of(2, ebitda=0, multiple=6)

    def test_a_multiple_of_nothing_is_not_a_price(self) -> None:
        with pytest.raises(AddOnError, match="not a price"):
            AddOn.of(2, ebitda=10, multiple=0)

    def test_revenue_below_earnings_is_a_margin_above_one(self) -> None:
        with pytest.raises(AddOnError, match="margin above one"):
            AddOn.of(2, ebitda=12, multiple=6, revenue=10)

    def test_periods_are_numbered_from_one(self) -> None:
        with pytest.raises(AddOnError, match="numbered from one"):
            AddOn.of(0, ebitda=10, multiple=6)

    def test_a_dis_synergy_belongs_in_the_earnings(self) -> None:
        with pytest.raises(AddOnError, match="dis-synergy"):
            AddOn.of(2, ebitda=10, multiple=6, synergies=-1)

    def test_synergies_phase_in_over_at_least_one_period(self) -> None:
        with pytest.raises(AddOnError, match="at least one period"):
            AddOn.of(2, ebitda=10, multiple=6, synergies=2, synergy_phase_in=0)

    def test_the_same_tranche_twice_is_refused(self) -> None:
        with pytest.raises(AddOnError, match="drawn once"):
            AddOn.of(
                2,
                ebitda=10,
                multiple=6,
                draws=[Draw.of("TLB", 20), Draw.of("TLB", 10)],
            )

    def test_a_purchase_needs_a_label(self) -> None:
        with pytest.raises(AddOnError, match="needs a label"):
            AddOn.of(2, ebitda=10, multiple=6, label="   ")

    def test_a_platform_earning_nothing_cannot_carry_an_unstated_revenue(self) -> None:
        with pytest.raises(AddOnError, match="no margin to carry"):
            AddOn.of(2, ebitda=10, multiple=6).stream(ZERO)


# --------------------------------------------------------------------------
# The earnings, once they are inside the case
# --------------------------------------------------------------------------


class TestTheAcquiredStream:
    def test_nothing_is_contributed_before_the_purchase_closes(self) -> None:
        stream = AcquiredStream.of(3, revenue=100, margin="0.2")
        growth = Driver.constant("0.05", 5)
        assert stream.revenue_at(0, growth) == ZERO
        assert stream.revenue_at(1, growth) == ZERO
        assert stream.revenue_at(2, growth) == ZERO

    def test_the_first_full_period_carries_one_period_of_growth(self) -> None:
        stream = AcquiredStream.of(2, revenue=100, margin="0.2")
        growth = Driver.constant("0.05", 5)
        assert stream.revenue_at(2, growth) == money(105)
        assert stream.revenue_at(3, growth) == money("110.25")

    def test_an_override_growth_rate_is_used_instead_of_the_platforms(self) -> None:
        stream = AcquiredStream.of(
            1, revenue=100, margin="0.2", growth=Driver.constant("0.10", 5)
        )
        assert stream.revenue_at(1, Driver.constant("0.05", 5)) == money(110)

    def test_synergies_phase_straight_line_and_then_hold(self) -> None:
        stream = AcquiredStream.of(
            1, revenue=100, margin="0.2", synergies=8, synergy_phase_in=4
        )
        assert stream.synergies_at(1) == money(2)
        assert stream.synergies_at(2) == money(4)
        assert stream.synergies_at(3) == money(6)
        assert stream.synergies_at(4) == money(8)
        # Held, not compounded.
        assert stream.synergies_at(5) == money(8)

    def test_a_single_period_phase_lands_in_full_immediately(self) -> None:
        stream = AcquiredStream.of(1, revenue=100, margin="0.2", synergies=5)
        assert stream.synergies_at(0) == ZERO
        assert stream.synergies_at(1) == money(5)

    def test_the_contribution_is_the_revenue_at_margin_plus_the_synergy(self) -> None:
        stream = AcquiredStream.of(
            1, revenue=100, margin="0.25", synergies=4, synergy_phase_in=2
        )
        growth = Driver.constant("0.10", 5)
        # 110 of revenue at a quarter, plus half of four.
        assert stream.ebitda_at(1, growth) == money("29.5")

    def test_a_stream_that_earns_nothing_is_refused(self) -> None:
        with pytest.raises(ValueError, match="has to earn something"):
            AcquiredStream.of(1, revenue=0, margin="0.2")

    def test_a_margin_above_one_is_refused(self) -> None:
        with pytest.raises(ValueError, match="not a share of revenue"):
            AcquiredStream.of(1, revenue=100, margin="1.5")


class TestTheOperatingCase:
    def test_a_case_with_no_purchases_is_unchanged(self) -> None:
        plain = OperatingModel.project(grid(), assumptions(), opening_revenue=400)
        same = OperatingModel.project(
            grid(), assumptions(), opening_revenue=400, acquisitions=()
        )
        assert [p.revenue for p in plain] == [p.revenue for p in same]
        assert not plain.has_acquisitions

    def test_the_organic_base_never_absorbs_the_acquired_revenue(self) -> None:
        """The bug this guards against compounds bought revenue twice."""
        plain = OperatingModel.project(grid(), assumptions(), opening_revenue=400)
        with_add_on = OperatingModel.project(
            grid(),
            assumptions(),
            opening_revenue=400,
            acquisitions=[AcquiredStream.of(1, revenue=100, margin="0.2")],
        )
        for base, combined in zip(plain, with_add_on):
            assert combined.organic_revenue == base.revenue

    def test_acquired_revenue_appears_only_from_the_period_after(self) -> None:
        model = OperatingModel.project(
            grid(),
            assumptions(),
            opening_revenue=400,
            acquisitions=[AcquiredStream.of(2, revenue=100, margin="0.2")],
        )
        assert model[0].acquired_revenue == ZERO
        assert model[1].acquired_revenue == ZERO
        assert model[2].acquired_revenue == money(105)

    def test_the_combined_revenue_is_the_sum_of_both_streams(self) -> None:
        model = OperatingModel.project(
            grid(),
            assumptions(),
            opening_revenue=400,
            acquisitions=[AcquiredStream.of(1, revenue=100, margin="0.3")],
        )
        row = model[1]
        assert row.revenue == row.organic_revenue + row.acquired_revenue
        assert row.ebitda == row.organic_ebitda + row.acquired_ebitda
        # 105 of acquired revenue at three tenths.
        assert row.acquired_ebitda == money("31.5")

    def test_the_blended_margin_sits_between_the_two(self) -> None:
        model = OperatingModel.project(
            grid(),
            assumptions(margin="0.20"),
            opening_revenue=400,
            acquisitions=[AcquiredStream.of(1, revenue=200, margin="0.40")],
        )
        assert money("0.20") < model[1].ebitda_margin < money("0.40")

    def test_capital_intensity_is_struck_against_the_combined_revenue(self) -> None:
        model = OperatingModel.project(
            grid(),
            assumptions(),
            opening_revenue=400,
            acquisitions=[AcquiredStream.of(1, revenue=100, margin="0.2")],
        )
        row = model[1]
        assert row.capital_expenditure == row.revenue * money("0.03")
        assert row.net_working_capital == row.revenue * money("0.08")

    def test_the_exit_earnings_split_between_bought_and_built(self) -> None:
        model = OperatingModel.project(
            grid(),
            assumptions(),
            opening_revenue=400,
            acquisitions=[AcquiredStream.of(1, revenue=100, margin="0.2")],
        )
        assert model.has_acquisitions
        assert (
            model.exit_acquired_ebitda + model.organic_exit_ebitda == model.exit_ebitda
        )
        assert model.exit_acquired_ebitda > 0

    def test_a_purchase_beyond_the_case_is_refused(self) -> None:
        with pytest.raises(ValueError, match="beyond the 5 periods"):
            OperatingModel.project(
                grid(),
                assumptions(),
                opening_revenue=400,
                acquisitions=[AcquiredStream.of(6, revenue=100, margin="0.2")],
            )

    def test_two_purchases_both_contribute(self) -> None:
        model = OperatingModel.project(
            grid(),
            assumptions(),
            opening_revenue=400,
            acquisitions=[
                AcquiredStream.of(1, revenue=100, margin="0.2", label="First"),
                AcquiredStream.of(3, revenue=50, margin="0.2", label="Second"),
            ],
        )
        assert model[2].acquired_revenue == money("110.25")
        # 100 grown three periods, plus 50 grown one.
        assert model[3].acquired_revenue == money("115.7625") + money("52.5")


# --------------------------------------------------------------------------
# The funding, once it reaches the schedule
# --------------------------------------------------------------------------


class TestTheSchedule:
    def test_a_schedule_with_no_purchases_is_unchanged(self) -> None:
        assert run().periods == run([]).periods

    def test_the_draw_lands_on_the_tranche_at_the_period_end(self) -> None:
        schedule = run(
            [
                AddOn.of(
                    2,
                    ebitda=10,
                    multiple=5,
                    draws=[Draw.of("Acquisition facility", 40)],
                )
            ]
        )
        assert schedule[1].tranche("Acquisition facility").acquisition == money(40)
        assert schedule[1].tranche("Acquisition facility").closing == money(40)
        assert schedule[0].tranche("Acquisition facility").closing == ZERO

    def test_the_following_period_opens_on_the_larger_balance(self) -> None:
        schedule = run(
            [
                AddOn.of(
                    2,
                    ebitda=10,
                    multiple=5,
                    draws=[Draw.of("Acquisition facility", 40)],
                )
            ]
        )
        assert schedule[2].tranche("Acquisition facility").opening == money(40)
        assert schedule[2].tranche("Acquisition facility").cash_interest > 0

    def test_cash_leaves_the_balance_sheet_for_the_unfunded_part(self) -> None:
        add_on = AddOn.of(
            2, ebitda=10, multiple=5, draws=[Draw.of("Acquisition facility", 40)]
        )
        with_purchase = run([add_on])
        without = run()
        assert add_on.from_cash == money(10)
        assert (
            without[1].closing_cash - with_purchase[1].closing_cash == money(10)
        )
        assert with_purchase[1].acquisition_from_cash == money(10)
        assert with_purchase[1].acquisition_spend == money(50)

    def test_every_row_still_reconciles(self) -> None:
        schedule = run(
            [
                AddOn.of(
                    2,
                    ebitda=10,
                    multiple=5,
                    draws=[Draw.of("Acquisition facility", 40)],
                ),
                AddOn.of(
                    3,
                    ebitda=8,
                    multiple=6,
                    draws=[Draw.of("Acquisition facility", 45)],
                ),
            ]
        )
        for row in schedule:
            assert row.reconciles()
            for tranche in row.tranches:
                assert tranche.reconciles()

    def test_the_two_kinds_of_draw_are_reported_apart(self) -> None:
        schedule = run(
            [
                AddOn.of(
                    2,
                    ebitda=10,
                    multiple=5,
                    draws=[Draw.of("Acquisition facility", 40)],
                )
            ]
        )
        row = schedule[1]
        assert row.acquisition_debt == money(40)
        assert row.recapitalisation == ZERO
        assert row.incremental_draw == money(40)

    def test_the_totals_add_across_the_hold(self) -> None:
        schedule = run(
            [
                AddOn.of(
                    1,
                    ebitda=10,
                    multiple=5,
                    draws=[Draw.of("Acquisition facility", 40)],
                ),
                AddOn.of(
                    3,
                    ebitda=8,
                    multiple=6,
                    draws=[Draw.of("Acquisition facility", 45)],
                ),
            ]
        )
        assert schedule.total_acquisition_debt == money(85)
        assert schedule.total_acquisition_spend == money(98)
        assert schedule.total_acquisition_from_cash == money(13)

    def test_a_purchase_the_balance_sheet_cannot_fund_is_refused(self) -> None:
        with pytest.raises(AddOnError, match="which leaves only"):
            run([AddOn.of(2, ebitda=40, multiple=6)], opening_cash=30, minimum_cash=20)

    def test_a_purchase_in_the_final_period_buys_nothing(self) -> None:
        with pytest.raises(AddOnError, match="nothing it buys is ever earned"):
            run([AddOn.of(5, ebitda=10, multiple=5)])

    def test_a_purchase_beyond_the_schedule_is_refused(self) -> None:
        with pytest.raises(AddOnError, match="nothing it buys is ever earned"):
            run([AddOn.of(9, ebitda=10, multiple=5)])

    def test_two_purchases_in_one_period_are_refused(self) -> None:
        with pytest.raises(AddOnError, match="combine them into one"):
            run(
                [
                    AddOn.of(2, ebitda=10, multiple=5, label="One"),
                    AddOn.of(2, ebitda=8, multiple=5, label="Two"),
                ]
            )

    def test_a_draw_on_an_unknown_tranche_is_refused(self) -> None:
        with pytest.raises(AddOnError, match="no tranche named"):
            run(
                [
                    AddOn.of(
                        2, ebitda=10, multiple=5, draws=[Draw.of("Mezzanine", 20)]
                    )
                ]
            )

    def test_a_purchase_is_checked_before_the_schedule_runs(self) -> None:
        """The message names the structure, which only a pre-flight check can."""
        with pytest.raises(AddOnError, match="'Acquisition facility', 'TLB'"):
            run(
                [
                    AddOn.of(
                        2, ebitda=10, multiple=5, draws=[Draw.of("Notes", 20)]
                    )
                ]
            )

    def test_over_funding_leaves_the_business_with_more_cash(self) -> None:
        add_on = AddOn.of(
            2, ebitda=10, multiple=5, draws=[Draw.of("Acquisition facility", 70)]
        )
        with_purchase = run([add_on])
        without = run()
        assert add_on.from_cash == money(-20)
        assert is_close(
            with_purchase[1].closing_cash - without[1].closing_cash, money(20)
        )


# --------------------------------------------------------------------------
# The blended entry
# --------------------------------------------------------------------------


class TestTheBlendedEntry:
    def entry(self, *add_ons: AddOn) -> BlendedEntry:
        return BlendedEntry(
            platform_enterprise_value=money(1000),
            platform_ebitda=money(100),
            add_ons=add_ons,
        )

    def test_a_platform_with_no_add_ons_blends_to_its_own_multiple(self) -> None:
        entry = self.entry()
        assert entry.platform_multiple == money(10)
        assert entry.blended_multiple == money(10)
        assert entry.arbitrage == ZERO
        assert entry.acquired_share == ZERO

    def test_buying_below_the_platform_multiple_blends_it_down(self) -> None:
        entry = self.entry(AddOn.of(1, ebitda=50, multiple=6))
        # 1300 of enterprise value over 150 of earnings.
        assert entry.blended_multiple == money(1300) / money(150)
        assert entry.arbitrage == money(10) - money(1300) / money(150)
        assert entry.arbitrage > 0

    def test_buying_above_the_platform_multiple_blends_it_up(self) -> None:
        entry = self.entry(AddOn.of(1, ebitda=50, multiple=14))
        assert entry.blended_multiple > money(10)
        assert entry.arbitrage < 0

    def test_synergies_are_held_out_of_one_reading_and_credited_in_another(
        self,
    ) -> None:
        entry = self.entry(AddOn.of(1, ebitda=50, multiple=6, synergies=10))
        assert entry.blended_multiple == money(1300) / money(150)
        assert entry.synergised_multiple == money(1300) / money(160)
        assert entry.synergised_multiple < entry.blended_multiple

    def test_fees_stay_out_of_the_multiple_and_inside_the_capital(self) -> None:
        entry = self.entry(AddOn.of(1, ebitda=50, multiple=6, fee_rate="0.02"))
        assert entry.enterprise_value == money(1300)
        assert entry.capital_deployed == money(1306)
        assert entry.all_in_multiple > entry.blended_multiple

    def test_the_acquired_share_is_earnings_bought_over_earnings_held(self) -> None:
        entry = self.entry(
            AddOn.of(1, ebitda=30, multiple=6), AddOn.of(2, ebitda=20, multiple=7)
        )
        assert entry.acquired_ebitda == money(50)
        assert entry.acquired_share == money(50) / money(150)
        assert len(entry) == 2

    def test_a_platform_earning_nothing_has_no_multiple_to_blend(self) -> None:
        with pytest.raises(AddOnError, match="earnings the platform was priced on"):
            BlendedEntry(platform_enterprise_value=money(100), platform_ebitda=ZERO)


# --------------------------------------------------------------------------
# A programme worked through by hand
# --------------------------------------------------------------------------


class TestAProgrammeComputedByHand:
    """One platform and two purchases, every figure re-derived independently.

    The point of doing it by hand is that the engine and the check share no
    code: the expected values below are arithmetic anyone can repeat on paper,
    which is the only way a test of a financial model proves anything.
    """

    def case(self) -> tuple[OperatingModel, list[AddOn]]:
        purchases = [
            AddOn.of(
                1,
                ebitda=10,
                multiple=6,
                revenue=50,
                synergies=2,
                synergy_phase_in=2,
                fee_rate="0.02",
                integration_cost=1,
                draws=[Draw.of("Acquisition facility", 50, issue_price="0.98")],
            ),
            AddOn.of(
                3,
                ebitda=8,
                multiple="7.5",
                revenue=40,
                draws=[Draw.of("Acquisition facility", 55)],
            ),
        ]
        model = OperatingModel.project(
            grid(),
            assumptions(growth="0.10", margin="0.25"),
            opening_revenue=200,
            acquisitions=[a.stream(money("0.25")) for a in purchases],
        )
        return model, purchases

    def test_the_first_purchase_costs_what_the_paper_says(self) -> None:
        _, purchases = self.case()
        first = purchases[0]
        assert first.enterprise_value == money(60)  # 10 x 6
        assert first.fees == money("1.2")  # 2% of 60
        assert first.uses == money("62.2")  # + 1 of integration
        assert first.debt_proceeds == money(49)  # 50 at 98
        assert first.from_cash == money("13.2")
        assert first.discount == money(1)
        assert first.total_cost == money("63.2")

    def test_the_organic_revenue_is_the_platform_compounded(self) -> None:
        model, _ = self.case()
        # 200 growing at a tenth, five times.
        expected = [money(220), money(242), money("266.2"), money("292.82"), money("322.102")]
        assert [p.organic_revenue for p in model] == expected

    def test_the_acquired_revenue_is_each_purchase_compounded_from_its_close(
        self,
    ) -> None:
        model, _ = self.case()
        # The first closes at the end of period one: 50 grown once, twice, ...
        # The second closes at the end of period three: 40 grown once from there.
        assert model[0].acquired_revenue == ZERO
        assert model[1].acquired_revenue == money(55)
        assert model[2].acquired_revenue == money("60.5")
        assert model[3].acquired_revenue == money("66.55") + money(44)
        assert model[4].acquired_revenue == money("73.205") + money("48.4")

    def test_the_acquired_earnings_carry_their_own_margin_and_synergy(self) -> None:
        """Bought at a fifth, not the platform's quarter, because revenue was stated."""
        model, _ = self.case()
        # Period two: 55 of revenue at a fifth, plus half of two.
        assert model[1].acquired_ebitda == money(12)
        # Period three: 60.5 at a fifth, plus the full two.
        assert model[2].acquired_ebitda == money("14.1")

    def test_the_total_is_the_platform_plus_what_was_bought(self) -> None:
        model, _ = self.case()
        for row in model:
            assert row.revenue == row.organic_revenue + row.acquired_revenue
            assert is_close(
                row.ebitda,
                row.organic_revenue * money("0.25") + row.acquired_ebitda,
            )

    def test_the_blended_multiple_re_derived_by_hand(self) -> None:
        _, purchases = self.case()
        entry = BlendedEntry(
            platform_enterprise_value=money(900),
            platform_ebitda=money(100),
            add_ons=tuple(purchases),
        )
        # 900 + 60 + 60 of enterprise value; 100 + 10 + 8 of earnings.
        assert entry.enterprise_value == money(1020)
        assert entry.ebitda == money(118)
        assert entry.blended_multiple == money(1020) / money(118)
        # Bought at 9.00x, blended to 8.64x: 0.36 turns of arbitrage.
        assert entry.arbitrage == money(9) - money(1020) / money(118)
        assert entry.synergised_multiple == money(1020) / money(120)
        # 900 of platform, plus 63.2 and 60 of deployed capital.
        assert entry.capital_deployed == money("1023.2")

    def test_the_schedule_funds_both_purchases_and_still_reconciles(self) -> None:
        model, purchases = self.case()
        schedule = DebtSchedule.from_operating_model(
            structure(minimum_cash=10),
            model,
            opening_cash=40,
            acquisitions=purchases,
        )
        assert schedule.total_acquisition_debt == money(105)
        assert schedule[0].acquisition_from_cash == money("13.2")
        assert schedule[2].acquisition_from_cash == money(5)  # 60 of uses, 55 raised
        for row in schedule:
            assert row.reconciles()
            for tranche in row.tranches:
                assert tranche.reconciles()

    def test_the_leverage_reads_worse_at_the_boundary_than_after_it(self) -> None:
        """The debt arrives a period before the earnings it bought."""
        model, purchases = self.case()
        schedule = DebtSchedule.from_operating_model(
            structure(minimum_cash=10),
            model,
            opening_cash=40,
            acquisitions=purchases,
        )
        landed = schedule.acquisitions[0]
        assert landed.turns_added is not None
        assert landed.turns_added > 0
        assert landed.debt_funded_share > money("0.7")
