import math
from datetime import date
from decimal import Decimal

import pytest

from capstack.daycount import DayCount
from capstack.money import money
from capstack.returns import (
    AmbiguousIRR,
    CashFlow,
    CashFlowStream,
    NoSignChange,
    brent_root,
    cagr,
    irr_periodic,
    moic,
    npv_periodic,
)


def flows(*amounts: float) -> list[Decimal]:
    return [money(a) for a in amounts]


class TestBrentRoot:
    def test_finds_a_polynomial_root(self) -> None:
        # x^2 - 2 on [0, 2] has its root at sqrt(2).
        root = brent_root(lambda x: x * x - 2.0, 0.0, 2.0)
        assert abs(root - math.sqrt(2)) < 1e-12

    def test_finds_a_transcendental_root(self) -> None:
        root = brent_root(lambda x: math.cos(x) - x, 0.0, 1.0)
        assert abs(root - 0.7390851332151607) < 1e-12

    def test_endpoint_that_is_already_a_root(self) -> None:
        assert brent_root(lambda x: x - 1.0, 1.0, 5.0) == 1.0
        assert brent_root(lambda x: x - 5.0, 1.0, 5.0) == 5.0

    def test_interval_that_does_not_bracket_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="bracket"):
            brent_root(lambda x: x * x + 1.0, -1.0, 1.0)

    def test_converges_on_a_hostile_function(self) -> None:
        # A near-flat region either side of the root defeats a fixed secant
        # iteration; Brent falls back to bisection and still lands.
        root = brent_root(lambda x: (x - 0.3) ** 3, -1.0, 2.0)
        assert abs(root - 0.3) < 1e-4


class TestNPVPeriodic:
    def test_zero_rate_is_a_plain_sum(self) -> None:
        assert npv_periodic(0, flows(-100, 50, 50, 50)) == money(50)

    def test_discounting_one_period(self) -> None:
        assert npv_periodic("0.10", flows(0, 110)) == money(100)

    def test_present_value_of_a_level_perpetuity_prefix(self) -> None:
        # Ten years of 100 at 10% discounts to 100 * (1 - 1.1^-10)/0.1.
        value = npv_periodic("0.10", flows(0, *([100] * 10)))
        expected = Decimal(100) * (1 - Decimal("1.1") ** -10) / Decimal("0.1")
        assert abs(value - expected) < Decimal("1e-20")

    def test_rate_at_or_below_minus_one_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="-100%"):
            npv_periodic("-1", flows(-100, 200))


class TestIRRAgainstClosedForm:
    def test_two_flows_match_the_closed_form(self) -> None:
        # Doubling over five periods: the rate is exactly 2^(1/5) - 1.
        r = irr_periodic(flows(-100, 0, 0, 0, 0, 200))
        assert abs(r - (2 ** (1 / 5) - 1)) < 1e-12

    def test_par_bond_returns_its_coupon(self) -> None:
        # A bond bought at par paying a 15% coupon yields exactly 15%.
        amounts = flows(-1000, *([150] * 9), 1150)
        assert abs(irr_periodic(amounts) - 0.15) < 1e-12

    def test_par_bond_at_a_second_coupon(self) -> None:
        amounts = flows(-1000, *([73.5] * 6), 1073.5)
        assert abs(irr_periodic(amounts) - 0.0735) < 1e-12

    def test_single_period_doubling_is_one_hundred_percent(self) -> None:
        assert abs(irr_periodic(flows(-100, 200)) - 1.0) < 1e-12

    def test_a_total_loss_of_half_the_money(self) -> None:
        assert abs(irr_periodic(flows(-1000, 500)) - (-0.5)) < 1e-12

    def test_a_deal_that_loses_money_has_a_negative_rate(self) -> None:
        r = irr_periodic(flows(-1000, 0, 0, 0, 0, 600))
        assert r < 0
        assert abs(r - ((600 / 1000) ** (1 / 5) - 1)) < 1e-12

    def test_present_value_at_the_solved_rate_is_zero(self) -> None:
        amounts = flows(-500, 80, 120, 140, 160, 300)
        r = irr_periodic(amounts)
        assert abs(npv_periodic(money(r), amounts)) < Decimal("1e-10")


class TestIRRInvariants:
    def test_scaling_every_flow_leaves_the_rate_unchanged(self) -> None:
        base = flows(-1000, 200, 300, 400, 500)
        scaled = flows(-1_000_000, 200_000, 300_000, 400_000, 500_000)
        assert abs(irr_periodic(base) - irr_periodic(scaled)) < 1e-12

    def test_flipping_every_sign_leaves_the_rate_unchanged(self) -> None:
        # NPV(r) = 0 and -NPV(r) = 0 have the same roots. Borrowing at a rate
        # is lending at the same rate seen from the other side.
        lender = flows(-1000, 150, 150, 1150)
        borrower = flows(1000, -150, -150, -1150)
        assert abs(irr_periodic(lender) - irr_periodic(borrower)) < 1e-12

    def test_interim_distributions_raise_the_rate(self) -> None:
        # Same total received, but sooner.
        late = flows(-1000, 0, 0, 1500)
        early = flows(-1000, 250, 250, 1000)
        assert irr_periodic(early) > irr_periodic(late)


class TestIRRRefusals:
    def test_all_outflows_has_no_rate(self) -> None:
        with pytest.raises(NoSignChange):
            irr_periodic(flows(-100, -100, -100))

    def test_all_inflows_has_no_rate(self) -> None:
        with pytest.raises(NoSignChange):
            irr_periodic(flows(100, 100, 100))

    def test_all_zeros_has_no_rate(self) -> None:
        with pytest.raises(NoSignChange):
            irr_periodic(flows(0, 0, 0))

    def test_a_single_flow_has_no_rate(self) -> None:
        with pytest.raises(NoSignChange):
            irr_periodic(flows(-100))

    def test_two_sign_changes_give_two_rates_and_a_refusal(self) -> None:
        # -1000 + 2500x - 1560x^2 = 0 with x = 1/(1+r) factors to roots at
        # x = 5/6 and x = 10/13, that is 20% and 30%. Both are the IRR.
        with pytest.raises(AmbiguousIRR) as caught:
            irr_periodic(flows(-1000, 2500, -1560))
        roots = caught.value.roots
        assert len(roots) == 2
        assert abs(roots[0] - 0.20) < 1e-9
        assert abs(roots[1] - 0.30) < 1e-9

    def test_the_ambiguity_message_names_both_rates(self) -> None:
        with pytest.raises(AmbiguousIRR, match="20.000000%"):
            irr_periodic(flows(-1000, 2500, -1560))

    def test_a_sign_change_that_is_only_a_zero_does_not_count(self) -> None:
        # Zeros are skipped, so this is still a pure outflow stream.
        with pytest.raises(NoSignChange):
            irr_periodic(flows(-100, 0, -100))

    def test_a_follow_on_cheque_still_resolves_when_it_leaves_one_root(self) -> None:
        # Out, back, out again, but the final inflow dominates: one root.
        r = irr_periodic(flows(-1000, 300, -200, 400, 900))
        assert 0.0 < r < 0.5


class TestMoic:
    def test_simple_double(self) -> None:
        assert moic(flows(-100, 200)) == money(2)

    def test_interim_distributions_count(self) -> None:
        assert moic(flows(-1000, 250, 250, 1000)) == money("1.5")

    def test_multiple_contributions(self) -> None:
        assert moic(flows(-500, -500, 2000)) == money(2)

    def test_a_loss_is_below_one(self) -> None:
        assert moic(flows(-1000, 400)) == money("0.4")

    def test_no_capital_invested_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="no capital"):
            moic(flows(100, 200))

    def test_moic_ignores_timing_where_irr_does_not(self) -> None:
        early = flows(-1000, 500, 0, 1000)
        late = flows(-1000, 0, 500, 1000)
        assert moic(early) == moic(late)
        assert irr_periodic(early) > irr_periodic(late)


class TestCagr:
    def test_doubling_over_five_years(self) -> None:
        assert abs(cagr(money(100), money(200), 5) - (2 ** (1 / 5) - 1)) < 1e-12

    def test_flat_is_zero(self) -> None:
        assert abs(cagr(money(100), money(100), 3)) < 1e-15

    def test_decline_is_negative(self) -> None:
        assert cagr(money(100), money(50), 4) < 0

    def test_zero_years_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            cagr(money(100), money(200), 0)

    def test_zero_opening_value_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="opening"):
            cagr(money(0), money(200), 5)

    def test_negative_closing_value_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="closing"):
            cagr(money(100), money(-1), 5)


class TestCashFlowStream:
    def test_flows_are_held_in_date_order(self) -> None:
        stream = CashFlowStream.of(
            [(date(2030, 1, 1), 200), (date(2026, 1, 1), -100)]
        )
        assert [f.when for f in stream] == [date(2026, 1, 1), date(2030, 1, 1)]

    def test_empty_stream_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            CashFlowStream(flows=())

    def test_total_and_bounds(self) -> None:
        stream = CashFlowStream.of(
            [(date(2026, 1, 1), -100), (date(2027, 1, 1), 30), (date(2031, 1, 1), 120)]
        )
        assert stream.total == money(50)
        assert stream.start == date(2026, 1, 1)
        assert stream.end == date(2031, 1, 1)
        assert len(stream) == 3

    def test_labels_survive(self) -> None:
        stream = CashFlowStream.of(
            [
                CashFlow.of(date(2026, 1, 1), -100, "sponsor equity"),
                CashFlow.of(date(2031, 1, 1), 250, "exit proceeds"),
            ]
        )
        assert [f.label for f in stream] == ["sponsor equity", "exit proceeds"]

    def test_xirr_on_exact_year_spacing_matches_the_periodic_rate(self) -> None:
        # Under 30/360 every anniversary is exactly one year, so the dated
        # answer must equal the evenly spaced one to solver tolerance.
        dates = [date(2026, 1, 1), date(2027, 1, 1), date(2028, 1, 1), date(2029, 1, 1)]
        amounts = [-1000, 150, 150, 1150]
        stream = CashFlowStream.of(
            list(zip(dates, amounts)), convention=DayCount.THIRTY_360_US
        )
        assert abs(stream.xirr() - irr_periodic(flows(*amounts))) < 1e-9

    def test_a_leap_day_in_the_window_lowers_the_annualised_rate(self) -> None:
        # The same nominal flows on the same anniversaries take 1096 actual
        # days rather than 1095 when a 29 February falls inside the hold, so
        # ACT/365F annualises them slightly lower. Small, but not nothing, and
        # it is the reason the convention has to be stated.
        dates = [date(2026, 1, 1), date(2027, 1, 1), date(2028, 1, 1), date(2029, 1, 1)]
        amounts = [-1000, 150, 150, 1150]
        actual = CashFlowStream.of(list(zip(dates, amounts))).xirr()
        thirty_360 = CashFlowStream.of(
            list(zip(dates, amounts)), convention=DayCount.THIRTY_360_US
        ).xirr()
        assert actual < thirty_360
        assert 0.0001 < thirty_360 - actual < 0.0002

    def test_xirr_is_annualised_not_per_period(self) -> None:
        # Doubling in six months is a 300% annual rate, not 100%.
        stream = CashFlowStream.of(
            [(date(2026, 1, 1), -100), (date(2026, 7, 2), 200)]
        )
        assert abs(stream.xirr() - 3.0) < 0.05

    def test_xirr_handles_irregular_spacing(self) -> None:
        stream = CashFlowStream.of(
            [
                (date(2026, 3, 17), -2_500_000),
                (date(2027, 11, 2), 400_000),
                (date(2029, 6, 30), 700_000),
                (date(2031, 8, 14), 3_100_000),
            ]
        )
        r = stream.xirr()
        # Verified by discounting back at the solved rate.
        assert abs(stream.npv(money(r))) < Decimal("0.01")

    def test_npv_is_zero_at_the_internal_rate(self) -> None:
        stream = CashFlowStream.of(
            [(date(2026, 1, 1), -1000), (date(2029, 7, 1), 1800)]
        )
        assert abs(stream.npv(money(stream.xirr()))) < Decimal("1e-6")

    def test_npv_discounted_to_an_earlier_date_is_smaller(self) -> None:
        stream = CashFlowStream.of([(date(2027, 1, 1), 1000)])
        at_flow = stream.npv("0.10", as_of=date(2027, 1, 1))
        a_year_early = stream.npv("0.10", as_of=date(2026, 1, 1))
        assert at_flow == money(1000)
        assert abs(a_year_early - money("909.0909090909")) < Decimal("1e-9")

    def test_moic_on_a_stream(self) -> None:
        stream = CashFlowStream.of(
            [(date(2026, 1, 1), -1000), (date(2031, 1, 1), 2600)]
        )
        assert stream.moic() == money("2.6")

    def test_holding_period(self) -> None:
        stream = CashFlowStream.of(
            [(date(2026, 1, 1), -1000), (date(2031, 1, 1), 2600)]
        )
        assert abs(stream.holding_period_years() - Decimal("5.0027")) < Decimal("0.001")

    def test_convention_changes_the_annualisation(self) -> None:
        pair = [(date(2026, 1, 1), -1000), (date(2031, 1, 1), 2000)]
        act365 = CashFlowStream.of(pair, convention=DayCount.ACT_365F).xirr()
        thirty = CashFlowStream.of(pair, convention=DayCount.THIRTY_360_US).xirr()
        assert act365 != thirty
        assert abs(act365 - thirty) < 0.001

    def test_single_dated_flow_has_no_rate(self) -> None:
        stream = CashFlowStream.of([(date(2026, 1, 1), -1000)])
        with pytest.raises(NoSignChange):
            stream.xirr()


class TestSponsorCase:
    """The shape an LBO actually produces: one cheque out, one exit back."""

    def test_a_five_year_hold_at_a_target_multiple(self) -> None:
        stream = CashFlowStream.of(
            [
                CashFlow.of(date(2026, 6, 30), -420_000_000, "sponsor equity"),
                CashFlow.of(date(2031, 6, 30), -0, "no interim distribution"),
                CashFlow.of(date(2031, 6, 30), 1_134_000_000, "exit proceeds"),
            ]
        )
        assert stream.moic() == money("2.7")
        r = stream.xirr()
        # 2.7x over five years is a shade under 22%.
        assert 0.21 < r < 0.22
        assert abs(stream.npv(money(r))) < Decimal("0.01")

    def test_a_dividend_recapitalisation_lifts_the_rate_at_the_same_multiple(self) -> None:
        without = CashFlowStream.of(
            [(date(2026, 6, 30), -400_000_000), (date(2031, 6, 30), 1_000_000_000)]
        )
        with_recap = CashFlowStream.of(
            [
                (date(2026, 6, 30), -400_000_000),
                (date(2028, 6, 30), 200_000_000),
                (date(2031, 6, 30), 800_000_000),
            ]
        )
        assert without.moic() == with_recap.moic() == money("2.5")
        assert with_recap.xirr() > without.xirr()
