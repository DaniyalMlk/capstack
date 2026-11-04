import math
from datetime import date
from decimal import Decimal

import pytest

from capstack.daycount import DayCount
from capstack.money import money
from capstack.returns import (
    RTOL_FLOOR,
    AmbiguousIRRError,
    CashFlow,
    CashFlowStream,
    DidNotConvergeError,
    NoSignChangeError,
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


def _quadratic(x: float) -> float:
    return x * x - 2.0


def _cube(x: float) -> float:
    return (x - 0.3) ** 3


def _line(x: float) -> float:
    return x - math.sqrt(2)


class TestBrentRootTolerances:
    """The three keyword options: refused where they cannot be met, honoured
    where they can.

    Each is chosen here with a value that moves the answer, which is the point.
    Passing an option and checking nothing broke does not establish that the
    option does anything, and all three spent the repository's life so far at
    their defaults.
    """

    def test_a_non_positive_xtol_is_refused_rather_than_draining_the_budget(self) -> None:
        # The failure this replaces: xtol=0.0 made the exit test abs(sbis) <
        # delta unsatisfiable and the fallback step xcur += delta stop moving,
        # so a bracket holding a root raised DidNotConvergeError -- documented
        # as an exhausted budget inside a valid bracket, which blames the
        # bracket. A negative xtol did the same and pointed the fallback step
        # away from the root as well.
        for bad in (0.0, -1e-6, -1.0, float("nan")):
            with pytest.raises(ValueError, match="xtol must be positive"):
                brent_root(_quadratic, 0.0, 2.0, xtol=bad)

    def test_an_rtol_below_the_representable_floor_is_refused(self) -> None:
        # Below four epsilons, rtol * abs(x) is finer than the gap between the
        # floats either side of x, so the relative half of the test asks for a
        # distance that does not exist. It used to be accepted and do nothing.
        assert repr(RTOL_FLOOR) == "8.881784197001252e-16"
        for bad in (0.0, 1e-30, 1e-17, float("nan")):
            with pytest.raises(ValueError, match="rtol must be at least"):
                brent_root(_quadratic, 0.0, 2.0, rtol=bad)

    def test_the_shipped_default_sits_just_above_that_floor(self) -> None:
        # The guard has to admit the value the function already defaults to, or
        # every existing call site breaks. 8.9e-16 clears 8.8817...e-16, and the
        # margin is thin enough to be worth pinning.
        assert RTOL_FLOOR <= 8.9e-16
        assert brent_root(_quadratic, 0.0, 2.0, rtol=8.9e-16) == brent_root(_quadratic, 0.0, 2.0)

    def test_a_max_iter_below_one_is_a_caller_error_not_a_drained_budget(self) -> None:
        for bad in (0, -1, -200):
            with pytest.raises(ValueError, match="max_iter must be at least 1"):
                brent_root(_quadratic, 0.0, 2.0, max_iter=bad)

    def test_choosing_max_iter_bounds_the_work(self) -> None:
        # Measured on this bracket: seven iterations are not enough and eight
        # are, so the budget is genuinely consulted rather than decorative.
        with pytest.raises(DidNotConvergeError, match="no root after 7 iterations"):
            brent_root(_quadratic, 0.0, 2.0, max_iter=7)
        assert abs(brent_root(_quadratic, 0.0, 2.0, max_iter=8) - math.sqrt(2)) < 1e-12

    def test_choosing_xtol_loosens_the_answer_by_the_amount_asked_for(self) -> None:
        tight = brent_root(_quadratic, 0.0, 2.0)
        loose = brent_root(_quadratic, 0.0, 2.0, xtol=1e-3)
        assert abs(tight - math.sqrt(2)) < 1e-13  # measured 4.13e-14
        assert abs(loose - math.sqrt(2)) < 1e-3  # measured 1.42e-04, inside its own bound
        # Both halves: looser than the default, not merely different from it.
        assert abs(loose - math.sqrt(2)) > abs(tight - math.sqrt(2))

    def test_rtol_is_a_live_knob_and_not_only_a_default(self) -> None:
        # The claim this replaces was that rtol could not move a result here.
        # It can, at ordinary magnitudes: raising it to 1e-6 moves the root of
        # x^2-2 by 2.4e-07, and the cube -- flat around its root, so the bracket
        # closes slowly and the relative term binds sooner -- moves at 9e-15.
        assert brent_root(_quadratic, 0.0, 2.0, rtol=1e-6) != brent_root(_quadratic, 0.0, 2.0)
        assert brent_root(_cube, -1.0, 2.0, rtol=9e-15) != brent_root(_cube, -1.0, 2.0)

    def test_a_linear_fixture_cannot_observe_either_tolerance(self) -> None:
        # Recorded because measuring the two tolerances on a line is how the
        # first reading of rtol came out wrong. The secant step is exact on a
        # line, so the method returns before either half of the convergence
        # test is reached, and decades of either tolerance give one answer. A
        # fixture built to measure a tolerance has to be nonlinear.
        assert len({brent_root(_line, 0.0, 2.0, rtol=r) for r in (RTOL_FLOOR, 1e-10, 1e-6, 1e-1)}) == 1
        assert len({brent_root(_line, 0.0, 2.0, xtol=x) for x in (1e-12, 1e-6, 1e-3, 1.0)}) == 1
        # The same xtol sweep on a nonlinear function does separate, which is
        # what makes the line's single answer a property of the line rather
        # than of the sweep.
        assert len({brent_root(_quadratic, 0.0, 2.0, xtol=x) for x in (1e-12, 1e-6, 1e-3, 1.0)}) > 1


class TestNPVPeriodicOffset:
    """``first_period_offset``, the one option in this package that nothing
    had chosen.

    The arithmetic turned out to be right, so this exercises it rather than
    changing it. Worth saying, because the reflex on finding an unchosen
    documented parameter is to assume it is wrong.
    """

    RATE = "0.0875"
    STREAM = (-100, 30, 40, 50, 60)

    def _flows(self) -> list[Decimal]:
        return flows(*self.STREAM)

    def test_the_default_is_no_shift(self) -> None:
        assert npv_periodic(self.RATE, self._flows(), first_period_offset=0) == npv_periodic(
            self.RATE, self._flows()
        )

    @pytest.mark.parametrize("offset", [1, 2, 3, 7])
    def test_a_positive_offset_is_exactly_a_run_of_leading_zeros(self, offset: int) -> None:
        # The identity is an equality, not a bound: both sides discount the same
        # terms by the same powers, so there is nothing for the 34-digit context
        # to re-associate. Measured exact in 288 of 288 cases over six rates and
        # six streams.
        shifted = npv_periodic(self.RATE, self._flows(), first_period_offset=offset)
        padded = npv_periodic(self.RATE, flows(*([0] * offset + list(self.STREAM))))
        assert shifted == padded

    def test_a_negative_offset_compounds_forward(self) -> None:
        # Monotone in the offset, and strictly: each period earlier is one more
        # period of compounding on a stream whose present value is positive.
        # Asserted as an ordering rather than as an equality against
        # plain * base ** -offset, because that is a different association of
        # the same arithmetic and lands 1e-32 out at offset -2. The bound on
        # that identity is the test below.
        values = [
            npv_periodic(self.RATE, self._flows(), first_period_offset=offset)
            for offset in (0, -1, -2, -4, -6)
        ]
        assert values == sorted(values)
        assert len(set(values)) == len(values)
        # One period earlier is one factor of (1 + r), to the same relative
        # bound the scaling identity holds to.
        base = Decimal(1) + money(self.RATE)
        one_earlier = npv_periodic(self.RATE, self._flows(), first_period_offset=-1)
        assert abs(one_earlier - values[0] * base) <= abs(one_earlier) * Decimal("1e-30")

    def test_the_offset_scales_the_whole_stream_to_within_a_relative_bound(self) -> None:
        # Deliberately a bound where the test above is an equality. Discounting
        # each term by one more period and dividing the finished total by one
        # more period are different associations of the same arithmetic, and the
        # 34-digit context is not associative. Swept over six rates, six streams
        # and nineteen offsets: worst absolute residue 2e-29, worst relative
        # 1.5e-33, so the bound is relative -- the absolute residue tracks the
        # magnitude of the answer.
        base = Decimal(1) + money(self.RATE)
        plain = npv_periodic(self.RATE, self._flows())
        inexact = 0
        for offset in range(-6, 13):
            shifted = npv_periodic(self.RATE, self._flows(), first_period_offset=offset)
            scaled = plain / (base**offset)
            assert abs(shifted - scaled) <= abs(shifted) * Decimal("1e-30")
            if shifted != scaled:
                inexact += 1
        # Both halves. An equality here would be the wrong assertion, and a
        # bound nothing ever strains is decoration, so at least one case has to
        # actually be inexact.
        assert inexact > 0

    def test_the_rate_guard_holds_whatever_the_offset(self) -> None:
        # The offset multiplies through a power of (1 + r), so a rate at or
        # below -100% has to be refused before the offset is applied rather
        # than after.
        for offset in (0, 5, -5):
            with pytest.raises(ValueError, match="discount rate must exceed"):
                npv_periodic("-1", self._flows(), first_period_offset=offset)

    def test_a_deferred_annuity_is_the_textbook_figure(self) -> None:
        # Five payments of 100 starting at the end of period three, at 10%.
        # The ordinary annuity factor for five periods is 3.790786769408448,
        # and deferring it two further periods divides by 1.1 squared.
        payments = flows(*([100] * 5))
        deferred = npv_periodic("0.10", payments, first_period_offset=3)
        factor = npv_periodic("0.10", payments, first_period_offset=1) / money(100)
        assert factor == pytest.approx(Decimal("3.790786769408448"), abs=Decimal("1e-15"))
        assert deferred == pytest.approx(
            money(100) * factor / (Decimal("1.1") ** 2), abs=Decimal("1e-13")
        )


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
        with pytest.raises(NoSignChangeError):
            irr_periodic(flows(-100, -100, -100))

    def test_all_inflows_has_no_rate(self) -> None:
        with pytest.raises(NoSignChangeError):
            irr_periodic(flows(100, 100, 100))

    def test_all_zeros_has_no_rate(self) -> None:
        with pytest.raises(NoSignChangeError):
            irr_periodic(flows(0, 0, 0))

    def test_a_single_flow_has_no_rate(self) -> None:
        with pytest.raises(NoSignChangeError):
            irr_periodic(flows(-100))

    def test_two_sign_changes_give_two_rates_and_a_refusal(self) -> None:
        # -1000 + 2500x - 1560x^2 = 0 with x = 1/(1+r) factors to roots at
        # x = 5/6 and x = 10/13, that is 20% and 30%. Both are the IRR.
        with pytest.raises(AmbiguousIRRError) as caught:
            irr_periodic(flows(-1000, 2500, -1560))
        roots = caught.value.roots
        assert len(roots) == 2
        assert abs(roots[0] - 0.20) < 1e-9
        assert abs(roots[1] - 0.30) < 1e-9

    def test_the_ambiguity_message_names_both_rates(self) -> None:
        with pytest.raises(AmbiguousIRRError, match=r"20\.000000%"):
            irr_periodic(flows(-1000, 2500, -1560))

    def test_a_sign_change_that_is_only_a_zero_does_not_count(self) -> None:
        # Zeros are skipped, so this is still a pure outflow stream.
        with pytest.raises(NoSignChangeError):
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
            list(zip(dates, amounts, strict=True)), convention=DayCount.THIRTY_360_US
        )
        assert abs(stream.xirr() - irr_periodic(flows(*amounts))) < 1e-9

    def test_a_leap_day_in_the_window_lowers_the_annualised_rate(self) -> None:
        # The same nominal flows on the same anniversaries take 1096 actual
        # days rather than 1095 when a 29 February falls inside the hold, so
        # ACT/365F annualises them slightly lower. Small, but not nothing, and
        # it is the reason the convention has to be stated.
        dates = [date(2026, 1, 1), date(2027, 1, 1), date(2028, 1, 1), date(2029, 1, 1)]
        amounts = [-1000, 150, 150, 1150]
        actual = CashFlowStream.of(list(zip(dates, amounts, strict=True))).xirr()
        thirty_360 = CashFlowStream.of(
            list(zip(dates, amounts, strict=True)), convention=DayCount.THIRTY_360_US
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
        with pytest.raises(NoSignChangeError):
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
