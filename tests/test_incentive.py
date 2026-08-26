"""The incentive plan: vesting, the treasury method, and where the bands open."""

from __future__ import annotations

from decimal import Decimal

import pytest

from capstack.incentive import (
    IncentiveError,
    OptionPool,
    Ratchet,
    RatchetBand,
    Vesting,
    settle_pool,
)
from capstack.money import ONE, ZERO, is_close, money


def approx(value: Decimal, expected: str, tolerance: str = "1E-9") -> bool:
    return is_close(value, money(expected), tolerance=tolerance)


# --------------------------------------------------------------------------
# Vesting
# --------------------------------------------------------------------------

class TestVesting:
    def test_straight_line_across_the_schedule(self) -> None:
        vest = Vesting.of(4)
        assert vest.vested_at(money(0)) == ZERO
        assert vest.vested_at(money(1)) == money("0.25")
        assert vest.vested_at(money(2)) == money("0.5")
        assert vest.vested_at(money(4)) == ONE

    def test_nothing_vests_after_the_schedule_has_run(self) -> None:
        vest = Vesting.of(4)
        assert vest.vested_at(money(9)) == ONE

    def test_a_cliff_withholds_rather_than_slows(self) -> None:
        """A one-year cliff on a four-year vest pays a quarter the day it lapses."""
        vest = Vesting.of(4, cliff_years=1)
        assert vest.vested_at(money("0.99")) == ZERO
        assert vest.vested_at(money(1)) == money("0.25")
        assert vest.vested_at(money(2)) == money("0.5")

    def test_acceleration_overrides_the_schedule_and_the_cliff(self) -> None:
        vest = Vesting.of(4, cliff_years=2, accelerates=True)
        assert vest.vested_at(money("0.1")) == ONE

    def test_a_negative_holding_period_vests_nothing(self) -> None:
        assert Vesting.of(4).vested_at(money(-1)) == ZERO

    def test_a_vest_that_does_not_run_is_refused(self) -> None:
        with pytest.raises(IncentiveError, match="must be positive"):
            Vesting.of(0)

    def test_a_cliff_beyond_the_schedule_is_refused(self) -> None:
        with pytest.raises(IncentiveError, match="never lapses"):
            Vesting.of(4, cliff_years=5)

    def test_a_negative_cliff_is_refused(self) -> None:
        with pytest.raises(IncentiveError, match="not be negative"):
            Vesting.of(4, cliff_years=-1)


# --------------------------------------------------------------------------
# The pool, flat
# --------------------------------------------------------------------------

class TestFlatPool:
    def test_a_pool_with_no_strike_takes_its_share_of_the_residual(self) -> None:
        outcome = settle_pool(OptionPool.of("MIP", "0.10"), money(1000), years=money(5))
        assert outcome.entitlement == money(100)
        assert outcome.paid == money(100)
        assert outcome.to_common == money(900)
        assert outcome.exercised

    def test_the_strike_joins_the_pot_before_the_pot_is_divided(self) -> None:
        """Treasury method: a tenth of 1,100, less the 100 paid to get it."""
        outcome = settle_pool(
            OptionPool.of("MIP", "0.10", strike=100), money(1000), years=money(5)
        )
        assert outcome.pot == money(1100)
        assert outcome.entitlement == money(110)
        assert outcome.strike_paid == money(100)
        assert outcome.paid == money(10)
        assert outcome.to_common == money(990)

    def test_an_out_of_the_money_pool_lapses_and_dilutes_nobody(self) -> None:
        # A tenth of 1,200 is 120, which is less than the 200 it costs to get it.
        outcome = settle_pool(
            OptionPool.of("MIP", "0.10", strike=200), money(1000), years=money(5)
        )
        assert not outcome.exercised
        assert outcome.entitlement == ZERO
        assert outcome.strike_paid == ZERO
        assert outcome.paid == ZERO
        assert outcome.to_common == money(1000)
        assert outcome.dilution == ZERO

    def test_the_payoff_is_continuous_where_the_decision_is_not(self) -> None:
        """At the exercise boundary both branches pay the pool nothing."""
        # Entitlement equals strike when 0.1 * (R + S) == S, i.e. R == 9S.
        pool = OptionPool.of("MIP", "0.10", strike=100)
        at = settle_pool(pool, money(900), years=money(5))
        assert at.paid == ZERO
        assert at.to_common == money(900)

        below = settle_pool(pool, money("899.99"), years=money(5))
        above = settle_pool(pool, money("900.01"), years=money(5))
        assert below.paid == ZERO
        assert approx(above.paid, "0.001")
        assert approx(above.to_common, "900.009")

    def test_everything_that_reaches_the_common_is_distributed(self) -> None:
        for residual in ("0", "1", "899", "900", "5000"):
            outcome = settle_pool(
                OptionPool.of("MIP", "0.12", strike=250), money(residual), years=money(5)
            )
            assert outcome.paid + outcome.to_common == money(residual)

    def test_vesting_scales_the_pool_and_its_strike_together(self) -> None:
        """Half-vested is half the options, so half the strike is paid."""
        pool = OptionPool.of("MIP", "0.10", strike=100, vesting=Vesting.of(4))
        outcome = settle_pool(pool, money(1000), years=money(2))
        assert outcome.vested == money("0.5")
        assert outcome.strike_paid == money(50)
        assert outcome.pot == money(1050)
        assert outcome.entitlement == money("52.5")  # 5% of 1,050
        assert outcome.paid == money("2.5")
        assert outcome.forfeited_share == money("0.5")

    def test_an_unvested_pool_takes_nothing(self) -> None:
        pool = OptionPool.of("MIP", "0.10", vesting=Vesting.of(4, cliff_years=2))
        outcome = settle_pool(pool, money(1000), years=money(1))
        assert outcome.vested == ZERO
        assert outcome.paid == ZERO
        assert outcome.to_common == money(1000)

    def test_a_wiped_out_residual_leaves_the_pool_nothing(self) -> None:
        outcome = settle_pool(OptionPool.of("MIP", "0.10"), ZERO, years=money(5))
        assert outcome.paid == ZERO
        assert outcome.effective_share == ZERO

    def test_a_negative_residual_is_refused(self) -> None:
        with pytest.raises(IncentiveError, match="must not be negative"):
            settle_pool(OptionPool.of("MIP", "0.10"), money(-1), years=money(5))

    def test_a_pool_holding_everything_is_refused(self) -> None:
        with pytest.raises(IncentiveError, match="below 1"):
            OptionPool.of("MIP", 1)

    def test_a_pool_needs_a_name(self) -> None:
        with pytest.raises(IncentiveError, match="needs a name"):
            OptionPool.of("   ", "0.1")

    def test_a_negative_strike_is_refused(self) -> None:
        with pytest.raises(IncentiveError, match="strike must not be negative"):
            OptionPool.of("MIP", "0.1", strike=-1)


# --------------------------------------------------------------------------
# The ratchet
# --------------------------------------------------------------------------

BASE = {
    "measured_capital": money(1000),
    "measured_prior": ZERO,
    "measured_ownership": ONE,
}


class TestRatchetShape:
    def test_bands_must_start_at_zero(self) -> None:
        with pytest.raises(IncentiveError, match="start the"):
            Ratchet.of([("2.0", "0.10")])

    def test_hurdles_must_step_up(self) -> None:
        with pytest.raises(IncentiveError, match="must step up"):
            Ratchet.of([("0", "0.05"), ("2.0", "0.10"), ("2.0", "0.15")])

    def test_a_band_that_pays_less_for_a_better_outcome_is_refused(self) -> None:
        with pytest.raises(IncentiveError, match="not a ratchet"):
            Ratchet.of([("0", "0.10"), ("2.0", "0.05")])

    def test_a_marginal_share_of_one_is_refused(self) -> None:
        with pytest.raises(IncentiveError, match="at the margin"):
            Ratchet.of([("0", "0.05"), ("2.0", "1")])

    def test_an_empty_ratchet_is_refused(self) -> None:
        with pytest.raises(IncentiveError, match="at least one band"):
            Ratchet.of([])

    def test_duplicate_measured_instruments_are_refused(self) -> None:
        with pytest.raises(IncentiveError, match="must be distinct"):
            Ratchet.of([("0", "0.05")], measured_on=["Sponsor", "Sponsor"])

    def test_bands_can_be_given_as_objects(self) -> None:
        ratchet = Ratchet.of([RatchetBand.of("0", "0.05"), RatchetBand.of("2", "0.10")])
        assert len(ratchet) == 2
        assert ratchet.top_share == money("0.10")
        assert [b.hurdle for b in ratchet] == [ZERO, money(2)]


class TestRatchetBoundaries:
    """The point of the design: the hurdles mean what they say."""

    RATCHET = Ratchet.of([("0", "0.05"), ("2.0", "0.10"), ("2.5", "0.15")])

    def test_the_first_band_opens_at_nothing(self) -> None:
        assert self.RATCHET.boundaries(**BASE)[0] == ZERO

    def test_the_measured_holders_land_exactly_on_the_hurdle(self) -> None:
        opens = self.RATCHET.boundaries(**BASE)
        # Band one: the pool takes 5% at the margin, so the sponsor takes 95%.
        # 2.0x on 1,000 of capital needs 2,000, which needs a pot of 2,000/0.95.
        assert approx(opens[1], "2105.263157894736842105263158")
        entitlement = self.RATCHET.entitlement(opens[1], **BASE)
        assert approx(opens[1] - entitlement, "2000")

    def test_the_second_hurdle_lands_exactly_too(self) -> None:
        opens = self.RATCHET.boundaries(**BASE)
        # 500 more of sponsor proceeds at 90% of the margin: 555.55... more pot.
        assert approx(opens[2], "2660.818713450292397660818713")
        entitlement = self.RATCHET.entitlement(opens[2], **BASE)
        assert approx(opens[2] - entitlement, "2500")

    def test_the_blended_share_sits_between_the_bands_it_spans(self) -> None:
        opens = self.RATCHET.boundaries(**BASE)
        pot = opens[2] + money(1000)
        entitlement = self.RATCHET.entitlement(pot, **BASE)
        blended = entitlement / pot
        assert money("0.05") < blended < money("0.15")

    def test_below_the_first_hurdle_only_the_base_share_applies(self) -> None:
        pot = money(1000)
        assert self.RATCHET.entitlement(pot, **BASE) == money(50)

    def test_entitlement_is_monotone_in_the_pot(self) -> None:
        previous = ZERO
        for step in range(0, 6000, 250):
            current = self.RATCHET.entitlement(money(step), **BASE)
            assert current >= previous
            previous = current

    def test_the_measured_holders_never_do_worse_for_a_better_outcome(self) -> None:
        previous = ZERO
        for step in range(0, 6000, 137):
            pot = money(step)
            proceeds = pot - self.RATCHET.entitlement(pot, **BASE)
            assert proceeds >= previous
            previous = proceeds

    def test_an_empty_pot_entitles_the_pool_to_nothing(self) -> None:
        assert self.RATCHET.entitlement(ZERO, **BASE) == ZERO
        assert self.RATCHET.entitlement(money(-5), **BASE) == ZERO

    def test_preferred_proceeds_count_towards_the_hurdle(self) -> None:
        """A sponsor already holding 1,000 of preferred is halfway to 2.0x."""
        kwargs = {**BASE, "measured_prior": money(1000)}
        opens = self.RATCHET.boundaries(**kwargs)
        # Only 1,000 more is needed, at 95% of the margin.
        assert approx(opens[1], "1052.631578947368421052631579")
        entitlement = self.RATCHET.entitlement(opens[1], **kwargs)
        assert approx(money(1000) + opens[1] - entitlement, "2000")

    def test_a_hurdle_already_cleared_opens_an_empty_band(self) -> None:
        """Prior proceeds past the top hurdle leave both bands opening at zero."""
        kwargs = {**BASE, "measured_prior": money(9999)}
        opens = self.RATCHET.boundaries(**kwargs)
        assert opens == (ZERO, ZERO, ZERO)
        # Every pound is then in the top band.
        assert self.RATCHET.entitlement(money(1000), **kwargs) == money(150)

    def test_ownership_below_one_slows_the_climb_to_the_hurdle(self) -> None:
        """A sponsor owning half the common needs twice the pot to clear 2.0x."""
        kwargs = {**BASE, "measured_ownership": money("0.5")}
        opens = self.RATCHET.boundaries(**kwargs)
        assert approx(opens[1], "4210.526315789473684210526316")
        entitlement = self.RATCHET.entitlement(opens[1], **kwargs)
        assert approx((opens[1] - entitlement) * money("0.5"), "2000")

    def test_holders_with_no_ownership_never_reach_the_next_hurdle(self) -> None:
        kwargs = {**BASE, "measured_ownership": ZERO}
        opens = self.RATCHET.boundaries(**kwargs)
        assert opens[1] > money("1E20")
        # Everything stays in the base band however large the pot.
        assert self.RATCHET.entitlement(money("1E9"), **kwargs) == money("5E7")

    def test_capital_of_nothing_has_no_multiple_to_test(self) -> None:
        with pytest.raises(IncentiveError, match="no multiple"):
            self.RATCHET.boundaries(**{**BASE, "measured_capital": ZERO})

    def test_vesting_scales_every_band(self) -> None:
        """Half-vested halves each marginal share, so the hurdles come sooner."""
        opens = self.RATCHET.boundaries(**BASE, vested=money("0.5"))
        # Base band now takes 2.5% at the margin, so the sponsor takes 97.5%.
        assert approx(opens[1], "2051.282051282051282051282051")
        entitlement = self.RATCHET.entitlement(opens[1], **BASE, vested=money("0.5"))
        assert approx(opens[1] - entitlement, "2000")


#: Two bands, used where the pool rather than the ratchet is under test.
RATCHET_FIXTURE = Ratchet.of([("0", "0.05"), ("2.0", "0.10")])


class TestRatchetThroughThePool:
    def test_the_pool_settles_on_the_bands_rather_than_its_quoted_share(self) -> None:
        pool = OptionPool.of("MIP", "0.10", ratchet=RATCHET_FIXTURE)
        outcome = settle_pool(
            pool,
            money(3000),
            years=money(5),
            measured_capital=money(1000),
            measured_ownership=ONE,
        )
        # The base band runs to a pot of 2,105.26 and pays 5% of it; the rest
        # pays 10%.
        assert approx(outcome.entitlement, "194.7368421052631578947368421")
        assert outcome.effective_share > money("0.05")
        assert outcome.effective_share < money("0.10")

    def test_a_ratcheted_pool_can_still_be_out_of_the_money(self) -> None:
        pool = OptionPool.of("MIP", "0.10", strike=500, ratchet=RATCHET_FIXTURE)
        outcome = settle_pool(
            pool,
            money(1000),
            years=money(5),
            measured_capital=money(1000),
            measured_ownership=ONE,
        )
        assert not outcome.exercised
        assert outcome.to_common == money(1000)

    def test_the_pot_is_still_fully_distributed_under_a_ratchet(self) -> None:
        pool = OptionPool.of("MIP", "0.10", strike=120, ratchet=RATCHET_FIXTURE)
        for residual in ("500", "2000", "4000", "10000"):
            outcome = settle_pool(
                pool,
                money(residual),
                years=money(5),
                measured_capital=money(1000),
                measured_ownership=ONE,
            )
            assert outcome.paid + outcome.to_common == money(residual)

