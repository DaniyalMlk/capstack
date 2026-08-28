"""Capitalised financing costs: the rate, the release, and the balance left.

The effective-rate cases are checked against an independent solve written out
in the test rather than against the engine's own answer, so a bug in the solver
cannot agree with itself.
"""

from __future__ import annotations

import dataclasses
import json
from decimal import Decimal
from pathlib import Path

import pytest

from capstack.cli import main
from capstack.daycount import DayCount
from capstack.debt import CapitalStructure, Tranche, TrancheKind
from capstack.drivers import Driver
from capstack.fees import (
    FeeMethod,
    FeePeriod,
    FeeSchedule,
    TrancheFees,
    contractual_profile,
    effective_rate,
)
from capstack.events import Draw, Refinancing, RefinancingError
from capstack.money import ZERO, is_close, money
from capstack.spec import DealSpecError, load_deal

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
THORNBURY = str(EXAMPLES / "thornbury.json")
KESTREL = str(EXAMPLES / "kestrel.json")
MERIDIAN = str(EXAMPLES / "meridian.json")


def ytm(price: float, coupon: float, face: float, years: int) -> float:
    """A bullet's yield to maturity, bisected here and nowhere near the engine."""
    def value(rate: float) -> float:
        flows = sum(
            face * coupon / (1 + rate) ** t for t in range(1, years + 1)
        )
        return flows + face / (1 + rate) ** years

    low, high = 0.0, 1.0
    for _ in range(200):
        mid = (low + high) / 2
        if value(mid) > price:
            low = mid
        else:
            high = mid
    return (low + high) / 2


def fixed(name: str, kind: TrancheKind, face: object, **kwargs: object) -> Tranche:
    kwargs.setdefault("floating", False)
    return Tranche.of(name, kind, face, **kwargs)  # type: ignore[arg-type]


def structure(*tranches: Tranche) -> CapitalStructure:
    return CapitalStructure.of(tranches, day_count=DayCount.ACT_365F)


def bullet(face: object = 100, rate: str = "0.06", maturity: int | None = 6) -> Tranche:
    return fixed("Notes", TrancheKind.NOTES, face, cash_rate=rate, maturity=maturity)


class TestTheContractualProfile:
    """What the rate is solved against, and what it deliberately excludes."""

    def test_a_bullet_repays_everything_at_maturity(self) -> None:
        opening, principal = contractual_profile(bullet(), 5)
        assert opening == [money(100)] * 5
        assert principal == [ZERO, ZERO, ZERO, ZERO, money(100)]

    def test_an_amortising_loan_steps_down_and_bullets_the_rest(self) -> None:
        loan = fixed(
            "TLB",
            TrancheKind.TERM_LOAN,
            100,
            cash_rate="0.05",
            amortisation=Driver.constant("0.10", 5),
            maturity=6,
        )
        opening, principal = contractual_profile(loan, 5)
        assert principal[:4] == [money(10)] * 4
        # The bullet at maturity is what is left, not another instalment.
        assert principal[4] == money(60)
        assert opening == [money(100), money(90), money(80), money(70), money(60)]

    def test_paper_outliving_the_grid_repays_at_the_end_of_it(self) -> None:
        # A model cannot amortise past its own horizon, so the profile the rate
        # is solved against ends where the grid does.
        opening, principal = contractual_profile(bullet(maturity=None), 3)
        assert principal == [ZERO, ZERO, money(100)]

    def test_it_refuses_a_profile_with_no_periods(self) -> None:
        with pytest.raises(ValueError, match="at least one period"):
            contractual_profile(bullet(), 0)


class TestTheEffectiveRate:
    """Solved against an independent bisection, not against the engine."""

    def test_a_five_year_bullet_at_ninety_eight(self) -> None:
        opening, principal = contractual_profile(bullet(), 5)
        solved = effective_rate(money(98), opening, principal, money("0.06"))
        assert abs(float(solved) - ytm(98.0, 0.06, 100.0, 5)) < 1e-10

    def test_a_ten_year_bullet_at_ninety_five(self) -> None:
        opening, principal = contractual_profile(bullet(rate="0.08", maturity=11), 10)
        solved = effective_rate(money(95), opening, principal, money("0.08"))
        assert abs(float(solved) - ytm(95.0, 0.08, 100.0, 10)) < 1e-10

    def test_paper_placed_at_par_prices_to_its_coupon(self) -> None:
        opening, principal = contractual_profile(bullet(), 5)
        solved = effective_rate(money(100), opening, principal, money("0.06"))
        assert abs(float(solved) - 0.06) < 1e-10

    def test_a_zero_coupon_prices_to_its_own_accretion(self) -> None:
        # 100 in five years for 78.352616... is exactly 5% a period.
        opening, principal = contractual_profile(bullet(rate="0", maturity=6), 5)
        price = money("78.35261664684589")
        solved = effective_rate(price, opening, principal, ZERO)
        assert abs(float(solved) - 0.05) < 1e-9

    def test_it_refuses_proceeds_of_nothing(self) -> None:
        opening, principal = contractual_profile(bullet(), 5)
        with pytest.raises(ValueError, match="no effective rate"):
            effective_rate(money(-1), opening, principal, money("0.06"))

    def test_it_refuses_mismatched_series(self) -> None:
        with pytest.raises(ValueError, match="one principal repayment"):
            effective_rate(money(98), [money(100)] * 3, [ZERO] * 2, money("0.06"))

    def test_it_refuses_an_empty_profile(self) -> None:
        with pytest.raises(ValueError, match="at least one period"):
            effective_rate(money(98), [], [], money("0.06"))


class TestTheReleaseSchedule:
    def bullet_fees(self, amount: int = 2) -> TrancheFees:
        return FeeSchedule.build(
            structure(bullet()), {"Notes": money(amount)}, 5
        ).tranche("Notes")

    def test_the_first_charge_is_the_gap_between_effective_and_contractual(self) -> None:
        row = self.bullet_fees()
        expected = money(98) * row.effective_rate - money(100) * money("0.06")
        assert is_close(row.periods[0].charge, expected, tolerance="1E-15")

    def test_the_charge_rises_across_a_bullet(self) -> None:
        # The carrying amount climbs towards face as the discount unwinds, so
        # the charge climbs with it.
        charges = [p.charge for p in self.bullet_fees()]
        assert charges == sorted(charges)
        assert charges[0] < charges[-1]

    def test_the_charge_falls_across_an_amortising_loan(self) -> None:
        # Principal comes off faster than the discount closes, so the same
        # method runs the other way. A method that only ever rose would be a
        # straight line with extra steps.
        loan = fixed(
            "TLB",
            TrancheKind.TERM_LOAN,
            100,
            cash_rate="0.05",
            amortisation=Driver.constant("0.10", 5),
            maturity=6,
        )
        charges = [p.charge for p in FeeSchedule.build(structure(loan), {"TLB": money(3)}, 5).tranche("TLB")]
        assert charges == sorted(charges, reverse=True)

    def test_everything_capitalised_is_released_by_maturity(self) -> None:
        row = self.bullet_fees()
        assert is_close(row.total_charged, row.capitalised, tolerance="1E-18")
        assert row.periods[-1].closing == ZERO

    def test_every_period_rolls_forward(self) -> None:
        for period in self.bullet_fees():
            assert period.reconciles()

    def test_the_uplift_is_the_effective_rate_less_the_coupon(self) -> None:
        row = self.bullet_fees()
        assert row.rate_uplift == row.effective_rate - row.coupon
        assert row.rate_uplift > 0

    def test_a_larger_fee_costs_more(self) -> None:
        small = self.bullet_fees(1).effective_rate
        large = self.bullet_fees(5).effective_rate
        assert large > small


class TestTheStraightLine:
    def test_it_spreads_the_balance_evenly(self) -> None:
        schedule = FeeSchedule.build(
            structure(bullet()), {"Notes": money(5)}, 5, method=FeeMethod.STRAIGHT_LINE
        )
        assert [p.charge for p in schedule.tranche("Notes")] == [money(1)] * 5

    def test_the_last_period_squares_a_balance_that_does_not_divide(self) -> None:
        # Three periods into ten leaves a third of a unit unaccounted for. The
        # last period takes it, so the balance reaches exactly zero rather than
        # a residual a reader has to know to ignore.
        schedule = FeeSchedule.build(
            structure(bullet(maturity=4)),
            {"Notes": money(10)},
            3,
            method=FeeMethod.STRAIGHT_LINE,
        )
        row = schedule.tranche("Notes")
        assert row.periods[-1].closing == ZERO
        assert row.total_charged == money(10)

    def test_a_revolver_is_released_evenly_whatever_it_is_asked_for(self) -> None:
        # A commitment fee buys a term, not a principal profile, so there is
        # nothing to solve an effective rate against.
        revolver = fixed("RCF", TrancheKind.REVOLVER, 0, commitment=50, cash_rate="0.03")
        row = FeeSchedule.build(
            structure(revolver),
            {"RCF": money(1)},
            4,
            method=FeeMethod.EFFECTIVE_INTEREST,
        ).tranche("RCF")
        assert row.method is FeeMethod.STRAIGHT_LINE
        assert [p.charge for p in row] == [money("0.25")] * 4

    def test_paper_placed_below_the_cost_of_placing_it_falls_back(self) -> None:
        # Fees larger than the face raise nothing to solve a rate against.
        row = FeeSchedule.build(structure(bullet(face=1)), {"Notes": money(5)}, 5).tranche(
            "Notes"
        )
        assert row.method is FeeMethod.STRAIGHT_LINE


class TestNothingCapitalised:
    def test_a_tranche_placed_at_par_with_no_fee_charges_nothing(self) -> None:
        row = FeeSchedule.build(structure(bullet()), {}, 5).tranche("Notes")
        assert row.capitalised == ZERO
        assert row.total_charged == ZERO
        assert row.unamortised_at(0) == ZERO

    def test_it_does_not_divide_by_a_face_of_zero(self) -> None:
        empty = fixed("Undrawn", TrancheKind.TERM_LOAN, 0, cash_rate="0.05")
        row = FeeSchedule.build(structure(empty), {}, 3).tranche("Undrawn")
        assert row.total_charged == ZERO

    def test_asking_about_a_tranche_that_capitalised_nothing_answers_zero(self) -> None:
        schedule = FeeSchedule.build(structure(bullet()), {}, 5)
        assert schedule.unamortised_at("Notes", 2) == ZERO

    def test_asking_about_a_tranche_that_is_not_there_answers_zero(self) -> None:
        schedule = FeeSchedule.build(structure(bullet()), {}, 5)
        assert schedule.unamortised_at("Mezzanine", 2) == ZERO

    def test_capitalising_against_a_name_that_is_not_there_is_an_error(self) -> None:
        with pytest.raises(KeyError, match="no tranche named"):
            FeeSchedule.build(structure(bullet()), {"Mezzanine": money(1)}, 5)

    def test_a_negative_capitalised_cost_is_refused(self) -> None:
        with pytest.raises(ValueError, match="premium"):
            FeeSchedule.build(structure(bullet()), {"Notes": money(-1)}, 5)

    def test_a_schedule_needs_a_period(self) -> None:
        with pytest.raises(ValueError, match="at least one period"):
            FeeSchedule.build(structure(bullet()), {"Notes": money(1)}, 0)


class TestPaperThatOutlivesTheModel:
    def test_a_seven_year_loan_on_a_five_year_hold_has_a_balance_left(self) -> None:
        schedule = FeeSchedule.build(
            structure(bullet(maturity=8)), {"Notes": money(7)}, 5
        )
        assert schedule.unreleased > 0
        assert schedule.total_charged < schedule.total_capitalised

    def test_a_balance_read_past_the_end_holds_the_last_one(self) -> None:
        row = FeeSchedule.build(
            structure(bullet(maturity=8)), {"Notes": money(7)}, 5
        ).tranche("Notes")
        assert row.unamortised_at(99) == row.unamortised_at(4)

    def test_a_negative_index_is_refused(self) -> None:
        row = FeeSchedule.build(structure(bullet()), {"Notes": money(2)}, 5).tranche(
            "Notes"
        )
        with pytest.raises(IndexError, match="must not be negative"):
            row.unamortised_at(-1)


class TestTheWriteOffOnARefinancing:
    def test_a_file_that_states_the_balance_is_believed(self) -> None:
        event = Refinancing.of(2, "Old TLB", unamortised_fees=9)
        assert event.states_its_write_off
        assert event.write_off(money(3)) == money(9)

    def test_a_file_that_does_not_takes_the_derived_figure(self) -> None:
        event = Refinancing.of(2, "Old TLB")
        assert not event.states_its_write_off
        assert event.write_off(money(3)) == money(3)

    def test_stating_nothing_and_stating_zero_are_different(self) -> None:
        assert Refinancing.of(2, "Old TLB", unamortised_fees=0).write_off(money(3)) == ZERO

    def test_a_negative_stated_balance_is_still_refused(self) -> None:
        with pytest.raises(RefinancingError, match="a credit, not a cost"):
            Refinancing.of(2, "Old TLB", unamortised_fees=-1)


class TestOnAWorkedExample:
    """Thornbury, whose repricing no longer carries a figure of its own."""

    def test_the_write_off_is_derived_rather_than_stated(self) -> None:
        deal = load_deal(THORNBURY)
        event = deal.refinancings[0]
        assert not event.states_its_write_off
        assert deal.write_offs()[event.period] > 0

    def test_it_is_the_balance_left_at_the_end_of_the_takeout_period(self) -> None:
        deal = load_deal(THORNBURY)
        fees = deal.fee_schedule()
        event = deal.refinancings[0]
        assert deal.write_offs()[event.period] == fees.unamortised_at(
            event.tranche, event.period - 1
        )

    def test_the_schedule_reports_what_was_derived(self) -> None:
        deal = load_deal(THORNBURY)
        outcome = deal.schedule().refinancings[0]
        assert outcome.write_off_was_derived
        assert outcome.written_off == deal.write_offs()[outcome.event.period]
        assert outcome.fees_written_off == outcome.written_off

    def test_the_write_off_is_not_cash_and_does_not_move_one(self) -> None:
        # The whole reason the charge is reported apart from the cash cost.
        deal = load_deal(THORNBURY)
        outcome = deal.schedule().refinancings[0]
        assert outcome.written_off > 0
        assert outcome.written_off not in (outcome.cash_cost,)
        assert outcome.cash_cost == (
            outcome.call_premium + outcome.event.financing_fees + outcome.event.discount
        )

    def test_the_capitalised_amount_is_the_fee_and_the_discount_together(self) -> None:
        deal = load_deal(THORNBURY)
        priced = {t.name: t for t in deal.transaction.debt}
        row = deal.fee_schedule().tranche("Term Loan B")
        loan = priced["Term Loan B"]
        assert row.capitalised == loan.financing_fee + loan.original_issue_discount

    def test_the_uplift_is_real_on_a_deal_with_fees(self) -> None:
        row = load_deal(KESTREL).fee_schedule().tranche("Unitranche")
        assert row.effective_rate > row.coupon
        assert row.rate_uplift > money("0.005")

    def test_a_deal_with_no_projection_cannot_release_anything(self) -> None:
        deal = load_deal(MERIDIAN)
        stripped = dataclasses.replace(deal, grid=None)
        with pytest.raises(DealSpecError, match="projection"):
            stripped.fee_schedule()


class TestOnTheCommandLine:
    def test_the_report_names_the_uplift(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["fees", KESTREL]) == 0
        out = capsys.readouterr().out
        assert "Cost of the money" in out
        assert "effective interest" in out

    def test_the_takeout_is_named(self, capsys: pytest.CaptureFixture[str]) -> None:
        main(["fees", THORNBURY])
        assert "Charged off early at a takeout" in capsys.readouterr().out

    def test_the_straight_line_is_flat(self, capsys: pytest.CaptureFixture[str]) -> None:
        main(["fees", THORNBURY, "--method", "straight-line", "--json"])
        report = json.loads(capsys.readouterr().out)
        charges = next(
            t["charges"] for t in report["tranches"] if t["name"] == "Term Loan B"
        )
        assert len(set(charges)) == 1

    def test_the_json_carries_the_rate_and_the_balances(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["fees", KESTREL, "--json"])
        report = json.loads(capsys.readouterr().out)
        row = next(t for t in report["tranches"] if t["name"] == "Unitranche")
        assert row["effective_rate"] > row["coupon"]
        # The unitranche outlives the five-year hold, so a balance is still
        # capitalised when the model ends.
        assert Decimal(row["balances"][-1]) > 0
        assert Decimal(row["balances"][-1]) < Decimal(row["capitalised"])
        assert Decimal(report["unreleased"]) == Decimal(row["balances"][-1])
        assert report["method"] == "effective interest"
