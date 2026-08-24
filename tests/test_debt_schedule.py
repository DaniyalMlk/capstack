import random
from datetime import date
from decimal import Decimal

import pytest

from capstack.daycount import DayCount
from capstack.debt import (
    CapitalStructure,
    CircularityNotResolved,
    DebtSchedule,
    InterestBasis,
    Tranche,
    TrancheKind,
    _one_pass,
)
from capstack.drivers import Driver
from capstack.money import ZERO, Money, is_close, money
from capstack.periods import Frequency, Period, PeriodGrid

#: A single period covering exactly one non-leap year, so an ACT/365F year
#: fraction is exactly 1 and hand arithmetic is arithmetic rather than a
#: day-count exercise.
YEAR = Period(1, date(2026, 1, 1), date(2027, 1, 1))


def one_year_grid(years: int = 5) -> list[Period]:
    return list(PeriodGrid.build(date(2026, 1, 1), years=years, frequency=Frequency.ANNUAL))


def fixed(name: str, kind: TrancheKind, face: object, **kwargs: object) -> Tranche:
    """A fixed-rate tranche, so no base rate is needed to price it."""
    kwargs.setdefault("floating", False)
    return Tranche.of(name, kind, face, **kwargs)  # type: ignore[arg-type]


def structure(*tranches: Tranche, **kwargs: object) -> CapitalStructure:
    kwargs.setdefault("day_count", DayCount.ACT_365F)
    return CapitalStructure.of(tranches, **kwargs)  # type: ignore[arg-type]


class TestInterestOnTheOpeningBalance:
    """The non-circular basis, where every figure can be checked by hand."""

    def test_a_bullet_pays_its_coupon_and_nothing_else(self) -> None:
        s = structure(
            fixed("Notes", TrancheKind.NOTES, 100, cash_rate="0.08"),
            interest_basis=InterestBasis.OPENING,
        )
        row = DebtSchedule.run(s, [YEAR], [30])[0]
        assert row.cash_interest == money(8)
        assert row.total_repayment == money(0)
        assert row.closing_debt == money(100)
        assert row.closing_cash == money(22)
        assert row.iterations == 1

    def test_scheduled_amortisation_is_paid_before_anything_else(self) -> None:
        s = structure(
            fixed(
                "Term Loan A",
                TrancheKind.TERM_LOAN,
                1000,
                cash_rate="0.05",
                amortisation=Driver.constant("0.10", 3),
                swept=False,
            ),
            interest_basis=InterestBasis.OPENING,
        )
        schedule = DebtSchedule.run(s, one_year_grid(3), [200, 200, 200])
        # 10% of the original 1,000 every period, regardless of the balance left.
        assert [p.mandatory_repayment for p in schedule] == [money(100)] * 3
        assert schedule[2].closing_debt == money(700)

    def test_amortisation_cannot_exceed_what_is_owed(self) -> None:
        s = structure(
            fixed(
                "Term Loan A",
                TrancheKind.TERM_LOAN,
                100,
                amortisation=Driver.constant("0.60", 3),
                swept=False,
            ),
            interest_basis=InterestBasis.OPENING,
        )
        schedule = DebtSchedule.run(s, one_year_grid(3), [500, 500, 500])
        assert [p.mandatory_repayment for p in schedule] == [money(60), money(40), money(0)]
        assert schedule[2].closing_debt == money(0)

    def test_the_sweep_takes_what_is_left_after_service(self) -> None:
        s = structure(
            fixed("Term Loan B", TrancheKind.TERM_LOAN, 100, cash_rate="0.10", swept=True),
            interest_basis=InterestBasis.OPENING,
        )
        row = DebtSchedule.run(s, [YEAR], [30])[0]
        # 30 of cash flow, 10 of interest, 20 swept.
        assert row.sweep_repayment == money(20)
        assert row.closing_debt == money(80)
        assert row.closing_cash == money(0)

    def test_a_partial_sweep_leaves_the_rest_on_the_balance_sheet(self) -> None:
        s = structure(
            fixed("Term Loan B", TrancheKind.TERM_LOAN, 100, cash_rate="0.10", swept=True),
            interest_basis=InterestBasis.OPENING,
            sweep_rate="0.75",
        )
        row = DebtSchedule.run(s, [YEAR], [30])[0]
        assert row.sweep_repayment == money(15)
        assert row.closing_cash == money(5)

    def test_a_partial_sweep_does_not_come_back_for_the_rest_next_period(self) -> None:
        # A 50% sweep on 100 of excess cash flow repays 50 and leaves 50. If the
        # sweep took a share of the whole cash balance instead, it would take
        # 25 of that retained 50 next period, then 12.50, and a 50% sweep would
        # quietly become a 100% one over a normal hold.
        s = structure(
            fixed("Term Loan B", TrancheKind.TERM_LOAN, 1000, swept=True),
            sweep_rate="0.5",
            interest_basis=InterestBasis.OPENING,
        )
        schedule = DebtSchedule.run(s, one_year_grid(4), [100, 0, 0, 0])
        assert [p.sweep_repayment for p in schedule] == [money(50)] + [money(0)] * 3
        assert schedule.closing_cash == money(50)

    def test_the_sweep_cannot_spend_cash_that_is_not_there(self) -> None:
        # Excess cash flow of 30, but the minimum balance leaves only 8 of it
        # actually available to pay with.
        s = structure(
            fixed("Term Loan B", TrancheKind.TERM_LOAN, 100, cash_rate="0.10", swept=True),
            minimum_cash=12,
            interest_basis=InterestBasis.OPENING,
        )
        row = DebtSchedule.run(s, [YEAR], [30])[0]
        assert row.sweep_repayment == money(8)

    def test_the_minimum_cash_balance_is_held_back_from_the_sweep(self) -> None:
        s = structure(
            fixed("Term Loan B", TrancheKind.TERM_LOAN, 100, cash_rate="0.10", swept=True),
            interest_basis=InterestBasis.OPENING,
            minimum_cash=12,
        )
        row = DebtSchedule.run(s, [YEAR], [30])[0]
        assert row.closing_cash == money(12)
        assert row.sweep_repayment == money(8)


class TestTheWaterfall:
    def test_the_revolver_is_repaid_before_the_term_loan(self) -> None:
        s = structure(
            fixed("Revolver", TrancheKind.REVOLVER, 50, cash_rate="0.03", commitment=100),
            fixed("Term Loan B", TrancheKind.TERM_LOAN, 100, cash_rate="0.05", swept=True),
            interest_basis=InterestBasis.OPENING,
        )
        row = DebtSchedule.run(s, [YEAR], [56.5])[0]
        # 6.50 of interest leaves 50 to sweep, which exactly clears the revolver.
        assert row.tranche("Revolver").sweep_repayment == money(50)
        assert row.tranche("Term Loan B").sweep_repayment == money(0)

    def test_surplus_beyond_the_senior_class_flows_down(self) -> None:
        s = structure(
            fixed("Revolver", TrancheKind.REVOLVER, 50, cash_rate="0.03", commitment=100),
            fixed("Term Loan B", TrancheKind.TERM_LOAN, 100, cash_rate="0.05", swept=True),
            interest_basis=InterestBasis.OPENING,
        )
        row = DebtSchedule.run(s, [YEAR], [86.5])[0]
        assert row.tranche("Revolver").closing == money(0)
        assert row.tranche("Term Loan B").sweep_repayment == money(30)

    def test_equal_ranks_share_pro_rata(self) -> None:
        s = structure(
            fixed("Term Loan A", TrancheKind.TERM_LOAN, 300, seniority=1, swept=True),
            fixed("Term Loan B", TrancheKind.TERM_LOAN, 100, seniority=1, swept=True),
            interest_basis=InterestBasis.OPENING,
        )
        row = DebtSchedule.run(s, [YEAR], [40])[0]
        assert row.tranche("Term Loan A").sweep_repayment == money(30)
        assert row.tranche("Term Loan B").sweep_repayment == money(10)

    def test_a_pro_rata_split_that_does_not_terminate_still_adds_up(self) -> None:
        # 1/3 and 2/3 of 100 are both non-terminating decimals.
        s = structure(
            fixed("Term Loan A", TrancheKind.TERM_LOAN, 100, seniority=1, swept=True),
            fixed("Term Loan B", TrancheKind.TERM_LOAN, 200, seniority=1, swept=True),
            interest_basis=InterestBasis.OPENING,
        )
        row = DebtSchedule.run(s, [YEAR], [100])[0]
        assert row.sweep_repayment == money(100)
        assert row.closing_debt == money(200)

    def test_notes_sit_outside_the_sweep(self) -> None:
        s = structure(
            fixed("Term Loan B", TrancheKind.TERM_LOAN, 100, swept=True),
            fixed("Senior notes", TrancheKind.NOTES, 400, cash_rate="0.07"),
            interest_basis=InterestBasis.OPENING,
        )
        row = DebtSchedule.run(s, [YEAR], [200])[0]
        assert row.tranche("Term Loan B").closing == money(0)
        assert row.tranche("Senior notes").closing == money(400)
        # Cash the sweep could not place stays on the balance sheet.
        assert row.closing_cash == money(72)

    def test_the_sweep_never_repays_more_than_is_outstanding(self) -> None:
        s = structure(
            fixed("Term Loan B", TrancheKind.TERM_LOAN, 100, swept=True),
            interest_basis=InterestBasis.OPENING,
        )
        row = DebtSchedule.run(s, [YEAR], [500])[0]
        assert row.tranche("Term Loan B").closing == money(0)
        assert row.closing_cash == money(400)


class TestPaymentInKind:
    def test_accrual_compounds_into_the_balance(self) -> None:
        s = structure(
            fixed("Mezzanine", TrancheKind.MEZZANINE, 100, cash_rate="0.05", pik_rate="0.05"),
            interest_basis=InterestBasis.OPENING,
        )
        schedule = DebtSchedule.run(s, one_year_grid(2), [50, 50])
        assert schedule[0].tranche("Mezzanine").closing == money(105)
        # Second period accrues on 105, not on 100.
        assert schedule[1].tranche("Mezzanine").pik_interest == money("5.25")
        assert schedule[1].tranche("Mezzanine").closing == money("110.25")

    def test_accrual_costs_no_cash_this_period(self) -> None:
        s = structure(
            fixed("Seller note", TrancheKind.SELLER_NOTE, 100, pik_rate="0.08"),
            interest_basis=InterestBasis.OPENING,
        )
        row = DebtSchedule.run(s, [YEAR], [10])[0]
        assert row.cash_interest == money(0)
        assert row.pik_interest == money(8)
        assert row.closing_cash == money(10)
        assert row.levered_free_cash_flow == money(10)

    def test_a_structure_can_end_larger_than_it_started(self) -> None:
        s = structure(
            fixed("Seller note", TrancheKind.SELLER_NOTE, 100, pik_rate="0.12"),
            interest_basis=InterestBasis.OPENING,
        )
        schedule = DebtSchedule.run(s, one_year_grid(3), [0, 0, 0])
        assert schedule.closing_debt > schedule.opening_debt
        assert schedule.debt_repaid < 0
        # The whole increase is accrual. Compared at a tolerance rather than
        # exactly: the balance carries 34 significant digits by this point, so
        # subtracting 100 from it drops the tail that the accrual still holds.
        assert is_close(
            schedule.total_pik_interest,
            schedule.closing_debt - money(100),
            tolerance="0.0000000001",
        )


class TestTheRevolver:
    def test_it_is_drawn_when_the_period_is_short(self) -> None:
        s = structure(
            fixed("Revolver", TrancheKind.REVOLVER, 0, cash_rate="0.03", commitment=100),
            fixed("Senior notes", TrancheKind.NOTES, 400, cash_rate="0.10"),
            interest_basis=InterestBasis.OPENING,
        )
        row = DebtSchedule.run(s, [YEAR], [-10])[0]
        # 40 of interest against a cash outflow of 10: 50 has to come from somewhere.
        assert row.revolver_draw == money(50)
        assert row.tranche("Revolver").closing == money(50)
        assert row.closing_cash == money(0)
        assert row.is_funded

    def test_the_draw_respects_the_commitment(self) -> None:
        s = structure(
            fixed("Revolver", TrancheKind.REVOLVER, 0, cash_rate="0.03", commitment=20),
            fixed("Senior notes", TrancheKind.NOTES, 400, cash_rate="0.10"),
            interest_basis=InterestBasis.OPENING,
        )
        row = DebtSchedule.run(s, [YEAR], [-10])[0]
        assert row.revolver_draw == money(20)
        assert row.funding_shortfall == money(30)
        assert not row.is_funded

    def test_a_gap_is_plugged_to_zero_rather_than_carried_forward_negative(self) -> None:
        # The gap is reported and notionally funded. Carrying a negative cash
        # balance into the next period would re-report the same deficit every
        # period and shrink every later sweep by an amount already accounted for.
        s = structure(
            fixed("Revolver", TrancheKind.REVOLVER, 0, cash_rate="0.03", commitment=20),
            fixed("Senior notes", TrancheKind.NOTES, 400, cash_rate="0.10"),
            minimum_cash=10,
            interest_basis=InterestBasis.OPENING,
        )
        schedule = DebtSchedule.run(s, one_year_grid(4), [-10, -10, -10, 500])
        assert [p.closing_cash for p in schedule.periods[:3]] == [money(0)] * 3
        # Each period reports the new money it needed, not the running total.
        assert schedule[1].funding_shortfall < schedule[0].funding_shortfall + money(30)
        for period in schedule:
            assert period.reconciles()

    def test_falling_below_the_minimum_is_not_the_same_as_running_out(self) -> None:
        s = structure(
            fixed("Senior notes", TrancheKind.NOTES, 400, cash_rate="0.05"),
            minimum_cash=100,
            interest_basis=InterestBasis.OPENING,
        )
        row = DebtSchedule.run(s, [YEAR], [-50], opening_cash=100)[0]
        # 30 of cash left: a policy breach, not a funding gap.
        assert row.closing_cash == money(30)
        assert row.funding_shortfall == money(0)
        assert row.cash_below_minimum == money(70)
        assert row.is_funded
        assert not row.meets_minimum_cash

    def test_a_structure_with_no_revolver_is_simply_short(self) -> None:
        s = structure(
            fixed("Senior notes", TrancheKind.NOTES, 400, cash_rate="0.10"),
            interest_basis=InterestBasis.OPENING,
        )
        schedule = DebtSchedule.run(s, [YEAR], [-10])
        assert schedule[0].funding_shortfall == money(50)
        assert schedule[0].closing_cash == money(0)
        assert not schedule.is_funded
        assert schedule.first_shortfall is schedule[0]
        assert schedule.total_shortfall == money(50)

    def test_the_commitment_fee_is_charged_on_what_is_not_drawn(self) -> None:
        s = structure(
            fixed(
                "Revolver",
                TrancheKind.REVOLVER,
                25,
                cash_rate="0.04",
                commitment=100,
                undrawn_fee="0.005",
            ),
            interest_basis=InterestBasis.OPENING,
        )
        row = DebtSchedule.run(s, [YEAR], [200])[0]
        # 75 undrawn at 50bp.
        assert row.undrawn_fees == money("0.375")
        assert row.cash_cost_of_debt == money("1.375")

    def test_the_peak_drawn_balance_is_reported(self) -> None:
        s = structure(
            fixed("Revolver", TrancheKind.REVOLVER, 0, cash_rate="0.03", commitment=200),
            fixed("Senior notes", TrancheKind.NOTES, 400, cash_rate="0.10"),
            interest_basis=InterestBasis.OPENING,
        )
        schedule = DebtSchedule.run(s, one_year_grid(3), [-30, -20, 200])
        assert schedule.peak_revolver_drawn > money(0)
        assert schedule.peak_revolver_drawn == max(
            p.tranche("Revolver").closing for p in schedule
        )
        # And it is drawn down again once the business turns cash generative.
        assert schedule[2].tranche("Revolver").closing == money(0)


class TestMaturity:
    def test_the_balance_falls_due_in_its_maturity_period(self) -> None:
        s = structure(
            fixed("Term Loan A", TrancheKind.TERM_LOAN, 100, maturity=2, swept=False),
            interest_basis=InterestBasis.OPENING,
        )
        schedule = DebtSchedule.run(s, one_year_grid(3), [200, 200, 200])
        assert schedule[0].mandatory_repayment == money(0)
        assert schedule[1].mandatory_repayment == money(100)
        assert schedule[2].closing_debt == money(0)

    def test_a_matured_facility_cannot_be_drawn_again(self) -> None:
        # Repaying a facility at maturity does not make it available again. A
        # model that keeps the commitment alive will clear the balance and
        # redraw it in the same period, and go on doing so indefinitely.
        s = structure(
            fixed(
                "Revolver",
                TrancheKind.REVOLVER,
                0,
                cash_rate="0.05",
                commitment=100,
                undrawn_fee="0.005",
                maturity=2,
            ),
            fixed("Senior notes", TrancheKind.NOTES, 400, cash_rate="0.10"),
            interest_basis=InterestBasis.OPENING,
        )
        schedule = DebtSchedule.run(s, one_year_grid(4), [50, 50, -10, -10])
        assert schedule[2].revolver_draw == money(0)
        assert schedule[2].tranche("Revolver").undrawn_fee == money(0)
        assert schedule[3].tranche("Revolver").closing == money(0)
        # With no facility left, the shortfall is reported rather than papered over.
        assert schedule[2].funding_shortfall > money(0)

    def test_a_maturing_balance_owes_a_full_period_of_interest(self) -> None:
        # It is repaid at the end of the period, not across it, so averaging it
        # against a closing balance of zero would halve the interest in the
        # period carrying the largest repayment in the model.
        tranche = fixed(
            "Term Loan A", TrancheKind.TERM_LOAN, 1000, cash_rate="0.08", maturity=1,
            swept=False,
        )
        opening_basis = DebtSchedule.run(
            structure(tranche, interest_basis=InterestBasis.OPENING), [YEAR], [2000]
        )
        average_basis = DebtSchedule.run(
            structure(tranche, interest_basis=InterestBasis.AVERAGE), [YEAR], [2000]
        )
        assert opening_basis[0].cash_interest == money(80)
        assert average_basis[0].cash_interest == money(80)

    def test_maturity_the_cash_flow_cannot_meet_is_reported_short(self) -> None:
        s = structure(
            fixed("Term Loan A", TrancheKind.TERM_LOAN, 100, maturity=1, swept=False),
            interest_basis=InterestBasis.OPENING,
        )
        schedule = DebtSchedule.run(s, one_year_grid(1), [10])
        assert schedule[0].funding_shortfall == money(90)
        assert schedule[0].closing_debt == money(0)
        assert schedule[0].closing_cash == money(0)
        assert schedule[0].reconciles()


class TestFloatingRates:
    def test_the_coupon_moves_with_the_base_rate(self) -> None:
        s = CapitalStructure.of(
            [Tranche.of("Term Loan B", TrancheKind.TERM_LOAN, 100, cash_rate="0.04")],
            base_rate=Driver.of(["0.02", "0.06"]),
            day_count=DayCount.ACT_365F,
            interest_basis=InterestBasis.OPENING,
        )
        schedule = DebtSchedule.run(s, one_year_grid(2), [0, 0])
        assert schedule[0].cash_interest == money(6)
        assert schedule[1].cash_interest == money(10)

    def test_the_floor_stops_the_borrower_benefiting_from_a_collapse(self) -> None:
        s = CapitalStructure.of(
            [
                Tranche.of(
                    "Term Loan B", TrancheKind.TERM_LOAN, 100, cash_rate="0.04", floor="0.01"
                )
            ],
            base_rate=Driver.constant("0.0005", 1),
            day_count=DayCount.ACT_365F,
            interest_basis=InterestBasis.OPENING,
        )
        row = DebtSchedule.run(s, [YEAR], [0])[0]
        assert row.cash_interest == money(5)  # 1% floor plus 400bp, not 5bp plus 400bp
        assert row.tranche("Term Loan B").rate == money("0.05")


class TestTheCircularity:
    """Interest on an average balance has no closed form once anything clamps."""

    def test_it_matches_the_closed_form_where_one_exists(self) -> None:
        # One tranche, no amortisation, everything swept, so nothing clamps:
        #   i = r(B - F/2) / (1 - r/2)
        # with B = 100, F = 30, r = 10%: i = 8.5 / 0.95.
        s = structure(
            fixed("Term Loan B", TrancheKind.TERM_LOAN, 100, cash_rate="0.10", swept=True),
        )
        row = DebtSchedule.run(s, [YEAR], [30])[0]
        exact = money("8.5") / money("0.95")
        assert abs(row.cash_interest - exact) < money("0.00000001")
        assert abs(row.closing_debt - (money(100) - (money(30) - exact))) < money("0.00000001")

    def test_the_answer_is_a_fixed_point_and_not_merely_a_stopping_place(self) -> None:
        # The real test of the solver: feed its own answer back in as the guess
        # and the same balances have to come out.
        s = structure(
            fixed("Revolver", TrancheKind.REVOLVER, 20, cash_rate="0.035", commitment=100),
            fixed(
                "Term Loan B",
                TrancheKind.TERM_LOAN,
                800,
                cash_rate="0.055",
                amortisation=Driver.constant("0.01", 5),
                swept=True,
            ),
            fixed("Mezzanine", TrancheKind.MEZZANINE, 200, cash_rate="0.05", pik_rate="0.05"),
            minimum_cash=25,
        )
        schedule = DebtSchedule.run(s, one_year_grid(3), [120, 140, 160], opening_cash=25)
        opening: dict[str, Money] = {t.name: t.face for t in s}
        cash = money(25)
        for i, row in enumerate(schedule):
            converged = {r.name: r.closing for r in row.tranches}
            again = _one_pass(
                s,
                index=i,
                period=row.period,
                opening=opening,
                opening_cash=cash,
                unlevered_free_cash_flow=row.unlevered_free_cash_flow,
                guess=converged,
                sweep_rate=row.sweep_rate,
            )
            for r in again.tranches:
                assert abs(r.closing - converged[r.name]) <= s.tolerance
            opening = converged
            cash = row.closing_cash

    def test_the_residual_is_reported_and_is_inside_tolerance(self) -> None:
        s = structure(
            fixed("Term Loan B", TrancheKind.TERM_LOAN, 500, cash_rate="0.06", swept=True),
        )
        schedule = DebtSchedule.run(s, one_year_grid(4), [80, 90, 100, 110])
        assert schedule.max_residual <= s.tolerance
        assert schedule.max_iterations_used > 1

    def test_accruing_on_the_opening_balance_overstates_interest(self) -> None:
        # The reason the circularity is worth resolving rather than dodging.
        tranche = fixed(
            "Term Loan B", TrancheKind.TERM_LOAN, 1000, cash_rate="0.08", swept=True
        )
        opening_basis = DebtSchedule.run(
            structure(tranche, interest_basis=InterestBasis.OPENING),
            one_year_grid(5),
            [200] * 5,
        )
        average_basis = DebtSchedule.run(
            structure(tranche, interest_basis=InterestBasis.AVERAGE), one_year_grid(5), [200] * 5
        )
        assert average_basis.total_cash_interest < opening_basis.total_cash_interest
        assert average_basis.closing_debt < opening_basis.closing_debt

    def test_the_two_bases_agree_when_the_balance_does_not_move(self) -> None:
        tranche = fixed("Senior notes", TrancheKind.NOTES, 400, cash_rate="0.07")
        a = DebtSchedule.run(
            structure(tranche, interest_basis=InterestBasis.OPENING), [YEAR], [100]
        )
        b = DebtSchedule.run(
            structure(tranche, interest_basis=InterestBasis.AVERAGE), [YEAR], [100]
        )
        assert a[0].cash_interest == b[0].cash_interest == money(28)

    def test_a_structure_that_cannot_settle_is_refused_rather_than_approximated(self) -> None:
        s = structure(
            fixed("Term Loan B", TrancheKind.TERM_LOAN, 1000, cash_rate="0.09", swept=True),
            max_iterations=2,
        )
        with pytest.raises(CircularityNotResolved, match="did not settle within 2 iterations"):
            DebtSchedule.run(s, [YEAR], [200])

    def test_a_realistic_structure_settles_at_a_full_step(self) -> None:
        # The halving is insurance, not the mechanism. If a plain structure
        # starts needing shortened steps, something has made the map far less
        # contractive than the coupon alone implies, and that is worth knowing.
        s = structure(
            fixed("Revolver", TrancheKind.REVOLVER, 20, cash_rate="0.035", commitment=100),
            fixed(
                "Term Loan B",
                TrancheKind.TERM_LOAN,
                800,
                cash_rate="0.055",
                amortisation=Driver.constant("0.01", 5),
                swept=True,
            ),
            fixed("Mezzanine", TrancheKind.MEZZANINE, 200, cash_rate="0.05", pik_rate="0.05"),
            minimum_cash=25,
        )
        schedule = DebtSchedule.run(s, one_year_grid(5), [120, 140, 160, 180, 200])
        assert schedule.shortest_step_taken == money(1)
        assert schedule.max_iterations_used <= 15

    def test_accretion_past_the_pole_is_refused_rather_than_guessed_at(self) -> None:
        # The fixed point C = B(1 + k/2)/(1 - k/2) has a pole at k = 2. Nothing
        # sensible lies on the other side of it, and no amount of iterating gets
        # there, so the engine says so.
        s = structure(
            fixed("Seller note", TrancheKind.SELLER_NOTE, 100, pik_rate="2.00"),
        )
        with pytest.raises(CircularityNotResolved, match="accreting faster"):
            DebtSchedule.run(s, [YEAR], [0])


class TestReconciliation:
    """Every period has to close, on both the debt side and the cash side."""

    @pytest.fixture()
    def schedule(self) -> DebtSchedule:
        s = CapitalStructure.of(
            [
                Tranche.of(
                    "Revolver",
                    TrancheKind.REVOLVER,
                    0,
                    cash_rate="0.030",
                    commitment=150,
                    undrawn_fee="0.005",
                ),
                Tranche.of(
                    "Term Loan B",
                    TrancheKind.TERM_LOAN,
                    1150,
                    cash_rate="0.0375",
                    floor="0.01",
                    amortisation=Driver.constant("0.01", 5),
                ),
                Tranche.of("Senior secured notes", TrancheKind.NOTES, 450, cash_rate="0.0725"),
                Tranche.of(
                    "Second lien",
                    TrancheKind.MEZZANINE,
                    250,
                    cash_rate="0.0575",
                    pik_rate="0.0450",
                ),
            ],
            minimum_cash=40,
            base_rate=Driver.constant("0.0425", 5),
            day_count=DayCount.ACT_360,
        )
        return DebtSchedule.run(
            s,
            list(PeriodGrid.build(date(2026, 6, 30), years=5, frequency=Frequency.ANNUAL)),
            ["119.59325", "142.71", "166.65", "190.72", "214.17"],
            opening_cash=40,
        )

    def test_every_tranche_rolls_forward(self, schedule: DebtSchedule) -> None:
        for period in schedule:
            for row in period.tranches:
                assert row.reconciles(), f"{row.name} in period {period.index}"

    def test_every_period_reconciles_on_cash(self, schedule: DebtSchedule) -> None:
        for period in schedule:
            assert period.reconciles(), f"period {period.index}"

    def test_balances_chain_from_one_period_to_the_next(self, schedule: DebtSchedule) -> None:
        for earlier, later in zip(schedule.periods, schedule.periods[1:]):
            assert later.opening_cash == earlier.closing_cash
            for row in later.tranches:
                assert row.opening == earlier.tranche(row.name).closing

    def test_the_stack_conserves_across_the_hold(self, schedule: DebtSchedule) -> None:
        # What was owed at the start, plus what accrued and was drawn, less what
        # was repaid, is what is owed at the end. Nothing appears or vanishes.
        expected = (
            schedule.opening_debt
            + schedule.total_pik_interest
            + schedule.total_drawn
            - schedule.total_repaid
        )
        assert schedule.closing_debt == expected

    def test_cash_conserves_across_the_hold(self, schedule: DebtSchedule) -> None:
        flows = sum((p.unlevered_free_cash_flow for p in schedule), ZERO)
        expected = (
            schedule.opening_cash
            + flows
            - schedule.total_cash_interest
            - schedule.total_undrawn_fees
            - schedule.total_repaid
            + schedule.total_drawn
            + sum((p.funding_shortfall for p in schedule), ZERO)
        )
        assert schedule.closing_cash == expected

    def test_no_balance_ever_goes_negative(self, schedule: DebtSchedule) -> None:
        for period in schedule:
            assert period.closing_cash >= 0
            for row in period.tranches:
                assert row.closing >= 0

    def test_leverage_reads_off_the_closing_balance(self, schedule: DebtSchedule) -> None:
        assert schedule.leverage_at(0, 261.75) == schedule[0].closing_debt / money("261.75")
        assert schedule.net_leverage_at(0, 261.75) < schedule.leverage_at(0, 261.75)

    def test_balances_at_names_every_tranche(self, schedule: DebtSchedule) -> None:
        balances = schedule.balances_at(0)
        assert set(balances) == {t.name for t in schedule.structure}
        assert sum(balances.values(), ZERO) == schedule[0].closing_debt


class TestRunErrors:
    def test_periods_and_cash_flows_must_line_up(self) -> None:
        s = structure(fixed("Notes", TrancheKind.NOTES, 100, cash_rate="0.07"))
        with pytest.raises(ValueError, match="3 periods against 2 cash flows"):
            DebtSchedule.run(s, one_year_grid(3), [10, 20])

    def test_an_empty_projection_is_refused(self) -> None:
        s = structure(fixed("Notes", TrancheKind.NOTES, 100, cash_rate="0.07"))
        with pytest.raises(ValueError, match="at least one period"):
            DebtSchedule.run(s, [], [])

    def test_negative_opening_cash_is_refused(self) -> None:
        s = structure(fixed("Notes", TrancheKind.NOTES, 100, cash_rate="0.07"))
        with pytest.raises(ValueError, match="opening cash must not be negative"):
            DebtSchedule.run(s, [YEAR], [10], opening_cash=-1)

    def test_an_unknown_tranche_is_named(self) -> None:
        s = structure(fixed("Notes", TrancheKind.NOTES, 100, cash_rate="0.07"))
        with pytest.raises(KeyError, match="no tranche named"):
            DebtSchedule.run(s, [YEAR], [10])[0].tranche("Second lien")


class TestExactness:
    def test_a_tranche_amortises_to_exactly_zero(self) -> None:
        # The reason the money layer exists: a residue of a few billionths here
        # draws a revolver in the final period to repay it.
        s = structure(
            fixed(
                "Term Loan A",
                TrancheKind.TERM_LOAN,
                "333.33",
                cash_rate="0.0675",
                amortisation=Driver.constant(Decimal(1) / Decimal(3), 3),
                swept=False,
            ),
            interest_basis=InterestBasis.OPENING,
        )
        schedule = DebtSchedule.run(s, one_year_grid(3), [500, 500, 500])
        assert schedule.closing_debt == Decimal(0)

    def test_a_tranche_at_zero_is_not_handed_the_rounding_dust(self) -> None:
        # Pro-rating an awkward pot across three live claims leaves a residue
        # that has to land somewhere. It must not land on the fourth, which is
        # entitled to nothing: both the repayment and the balance floor at zero,
        # so a few billionths there can never be cleared again.
        s = structure(
            *(
                fixed(name, TrancheKind.TERM_LOAN, face, seniority=1, swept=True)
                for name, face in (("A", 87577), ("B", 81955), ("C", 150), ("D", 0))
            ),
            interest_basis=InterestBasis.OPENING,
        )
        row = DebtSchedule.run(s, [YEAR], [80203])[0]
        assert row.tranche("D").sweep_repayment == ZERO
        assert row.tranche("D").closing == ZERO
        assert row.sweep_repayment == money(80203)

    def test_a_commitment_fee_needs_a_commitment_to_be_charged_on(self) -> None:
        with pytest.raises(ValueError, match="only a revolving facility has any"):
            fixed("Term Loan B", TrancheKind.TERM_LOAN, 100, undrawn_fee="0.005")

    def test_a_three_way_pro_rata_sweep_leaves_no_residue(self) -> None:
        s = structure(
            *(
                fixed(f"Loan {n}", TrancheKind.TERM_LOAN, face, seniority=1, swept=True)
                for n, face in (("A", "100.01"), ("B", "200.02"), ("C", "299.97"))
            ),
            interest_basis=InterestBasis.OPENING,
        )
        row = DebtSchedule.run(s, [YEAR], ["123.45"])[0]
        assert row.sweep_repayment == money("123.45")
        assert row.closing_debt == money("476.55")


class TestInvariantsUnderRandomStructures:
    """The reconciliations have to hold for structures nobody thought about.

    A fixed seed, so a failure is reproducible and a regression does not depend
    on which day the suite runs.
    """

    @staticmethod
    def _case(rng: random.Random, periods: int) -> tuple[CapitalStructure, list[Money], Money]:
        def rate(low: float, high: float) -> Money:
            return money(str(round(rng.uniform(low, high), 4)))

        tranches = [
            Tranche.of(
                "Revolver",
                TrancheKind.REVOLVER,
                0,
                cash_rate=rate(0.01, 0.06),
                commitment=rng.randint(20, 200),
                undrawn_fee="0.005",
            ),
            Tranche.of(
                "Term Loan B",
                TrancheKind.TERM_LOAN,
                rng.randint(100, 1500),
                cash_rate=rate(0.02, 0.08),
                floor="0.01",
                amortisation=Driver.constant(rate(0, 0.10), periods),
            ),
            Tranche.of(
                "Mezzanine",
                TrancheKind.MEZZANINE,
                rng.randint(0, 400),
                cash_rate=rate(0, 0.10),
                pik_rate=rate(0, 0.15),
            ),
        ]
        s = CapitalStructure.of(
            tranches,
            minimum_cash=rng.randint(0, 80),
            sweep_rate=rate(0.3, 1.0),
            base_rate=Driver.constant(rate(0, 0.08), periods),
            day_count=DayCount.ACT_360,
        )
        flows = [money(str(round(rng.uniform(-100, 500), 2))) for _ in range(periods)]
        return s, flows, money(rng.randint(0, 100))

    @pytest.mark.parametrize("seed", range(40))
    def test_everything_closes_and_nothing_goes_negative(self, seed: int) -> None:
        rng = random.Random(seed)
        periods = one_year_grid(5)
        for _ in range(10):
            s, flows, cash = self._case(rng, len(periods))
            schedule = DebtSchedule.run(s, periods, flows, opening_cash=cash)
            for period in schedule:
                assert period.reconciles()
                assert period.closing_cash >= 0
                for row in period.tranches:
                    assert row.reconciles()
                    assert row.closing >= 0

    @pytest.mark.parametrize("seed", range(10))
    def test_the_solver_settles_without_shortening_its_step(self, seed: int) -> None:
        rng = random.Random(seed + 1000)
        periods = one_year_grid(5)
        for _ in range(10):
            s, flows, cash = self._case(rng, len(periods))
            schedule = DebtSchedule.run(s, periods, flows, opening_cash=cash)
            assert schedule.max_residual <= s.tolerance
            assert schedule.max_iterations_used <= 20
            assert schedule.shortest_step_taken == money(1)
