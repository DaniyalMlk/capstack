"""The exit: valuation, the waterfall through the equity, and the bridge behind it."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from capstack.daycount import DayCount
from capstack.debt import CapitalStructure, DebtSchedule, InterestBasis, Tranche, TrancheKind
from capstack.drivers import Driver
from capstack.money import ONE, ZERO, is_close, money
from capstack.operating import OperatingAssumptions, OperatingModel
from capstack.outcome import (
    ExitValuation,
    Outcome,
    Security,
    SecurityKind,
    default_securities,
)
from capstack.periods import Frequency, PeriodGrid
from capstack.transaction import DebtFunding, EntryValuation, Transaction

CLOSE = date(2026, 6, 30)
EXIT = date(2031, 6, 30)


def case(years: int = 5, *, margin: str = "0.20", growth: str = "0.05") -> OperatingModel:
    return OperatingModel.project(
        PeriodGrid.build(CLOSE, years=years, frequency=Frequency.ANNUAL),
        OperatingAssumptions.of(
            revenue_growth=Driver.constant(growth, years),
            ebitda_margin=Driver.constant(margin, years),
            da_rate=Driver.constant("0.03", years),
            capex_rate=Driver.constant("0.04", years),
            nwc_rate=Driver.constant("0", years),
            tax_rate="0.25",
        ),
        opening_revenue=1000,
    )


def transaction(
    *,
    ltm: int = 200,
    multiple: str = "9",
    debt: int = 1000,
    rollover: int = 0,
    fee_rate: str = "0",
) -> Transaction:
    return Transaction.of(
        EntryValuation.of(ltm, multiple),
        debt=(DebtFunding.of("Term Loan B", debt),),
        rollover_equity=rollover,
        transaction_fee_rate=fee_rate,
    )


def schedule(model: OperatingModel, face: int = 1000, *, sweep: str = "1") -> DebtSchedule:
    structure = CapitalStructure.of(
        [
            Tranche.of(
                "Term Loan B", TrancheKind.TERM_LOAN, face, cash_rate="0.05", floating=False
            )
        ],
        interest_basis=InterestBasis.OPENING,
        day_count=DayCount.ACT_365F,
        sweep_rate=sweep,
    )
    return DebtSchedule.from_operating_model(structure, model, opening_cash=0)


def realise(**kwargs: object) -> Outcome:
    model = case()
    return Outcome.realise(
        transaction(),
        model,
        schedule(model),
        entry_date=CLOSE,
        **kwargs,  # type: ignore[arg-type]
    )


class TestExitValuation:
    def test_enterprise_value_is_the_multiple_on_exit_earnings(self) -> None:
        v = ExitValuation.of(EXIT, 300, 10, debt=1200, cash=100)
        assert v.enterprise_value == money(3000)
        assert v.net_debt == money(1100)
        assert v.equity_value == money(1900)

    def test_fees_are_charged_on_enterprise_value(self) -> None:
        v = ExitValuation.of(EXIT, 300, 10, debt=1200, cash=100, fee_rate="0.01")
        assert v.fees == money(30)
        assert v.equity_value == money(1870)

    def test_a_structure_under_water_hands_the_keys_over(self) -> None:
        v = ExitValuation.of(EXIT, 100, 5, debt=2000)
        assert v.gross_equity_value == money(-1500)
        assert v.equity_value == ZERO
        assert v.is_wiped_out

    def test_losses_are_not_worth_a_multiple_of_themselves(self) -> None:
        v = ExitValuation.of(EXIT, -50, 10, debt=100)
        assert v.enterprise_value == money(-500)
        assert v.fees == ZERO  # nothing to charge a sale fee against
        assert v.equity_value == ZERO

    def test_exit_leverage_is_reported_net(self) -> None:
        v = ExitValuation.of(EXIT, 300, 10, debt=1200, cash=300)
        assert v.exit_leverage == money(3)

    def test_a_non_positive_multiple_is_refused(self) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            ExitValuation.of(EXIT, 300, 0)

    def test_negative_balances_are_refused(self) -> None:
        with pytest.raises(ValueError, match="debt outstanding"):
            ExitValuation.of(EXIT, 300, 10, debt=-1)
        with pytest.raises(ValueError, match="cash at exit"):
            ExitValuation.of(EXIT, 300, 10, cash=-1)
        with pytest.raises(ValueError, match="fee rate"):
            ExitValuation.of(EXIT, 300, 10, fee_rate=-1)


class TestSecurityValidation:
    def test_a_security_needs_a_name(self) -> None:
        with pytest.raises(ValueError, match="needs a name"):
            Security.of(" ", SecurityKind.COMMON, 100, ownership=1)

    def test_ownership_is_a_share(self) -> None:
        with pytest.raises(ValueError, match="between 0 and 1"):
            Security.of("Common", SecurityKind.COMMON, 100, ownership="1.2")

    def test_common_cannot_carry_a_preferred_return(self) -> None:
        with pytest.raises(ValueError, match="accrues on preferred capital"):
            Security.of("Common", SecurityKind.COMMON, 100, preferred_rate="0.08")

    def test_the_residual_must_be_fully_owned(self) -> None:
        model = case()
        with pytest.raises(ValueError, match="fully owned"):
            Outcome.realise(
                transaction(),
                model,
                schedule(model),
                entry_date=CLOSE,
                securities=[Security.of("Common", SecurityKind.COMMON, 100, ownership="0.5")],
            )

    def test_names_must_be_distinct(self) -> None:
        model = case()
        with pytest.raises(ValueError, match="distinct"):
            Outcome.realise(
                transaction(),
                model,
                schedule(model),
                entry_date=CLOSE,
                securities=[
                    Security.of("A", SecurityKind.COMMON, 100, ownership="0.5"),
                    Security.of("A", SecurityKind.COMMON, 100, ownership="0.5"),
                ],
            )

    def test_an_exit_before_the_close_is_refused(self) -> None:
        model = case()
        with pytest.raises(ValueError, match="not after the close"):
            Outcome.realise(
                transaction(), model, schedule(model), entry_date=date(2032, 1, 1)
            )

    def test_a_mismatched_schedule_and_case_are_refused(self) -> None:
        with pytest.raises(ValueError, match="operating case 3"):
            Outcome.realise(
                transaction(), case(3), schedule(case(5)), entry_date=CLOSE
            )


class TestDefaultSecurities:
    def test_a_sponsor_alone_owns_the_whole_residual(self) -> None:
        securities = default_securities(transaction())
        assert len(securities) == 1
        assert securities[0].ownership == ONE
        assert securities[0].kind is SecurityKind.COMMON

    def test_rollover_takes_a_share_in_proportion_to_what_it_put_in(self) -> None:
        t = transaction(rollover=100)
        securities = default_securities(t)
        assert [s.name for s in securities] == ["Sponsor equity", "Rollover equity"]
        total = t.sponsor_equity + money(100)
        assert securities[1].ownership == money(100) / total
        assert is_close(sum(s.ownership for s in securities), ONE, tolerance="1E-20")

    def test_a_deal_with_no_equity_is_refused(self) -> None:
        # Debt raised exactly covers the deal, so the sponsor writes no cheque.
        t = Transaction.of(
            EntryValuation.of(200, 5), debt=(DebtFunding.of("Term Loan B", 1000),)
        )
        assert t.sponsor_equity == ZERO
        with pytest.raises(ValueError, match="no equity in it"):
            default_securities(t)


class TestPreferredAccrual:
    def test_a_compounding_return_accrues_on_itself(self) -> None:
        s = Security.of(
            "Preferred", SecurityKind.PREFERRED, 1000, preferred_rate="0.08"
        )
        accrued = s.accrued_at(money(2))
        # 1000 x 1.08^2 = 1166.40, so 166.40 of accrued return.
        assert round(Decimal(accrued), 2) == Decimal("166.40")
        assert s.claim_at(money(2)) == money(1000) + accrued

    def test_a_simple_return_does_not(self) -> None:
        s = Security.of(
            "Preferred",
            SecurityKind.PREFERRED,
            1000,
            preferred_rate="0.08",
            compounding=False,
        )
        assert s.accrued_at(money(2)) == money(160)

    def test_compounding_is_worth_more_over_a_full_hold(self) -> None:
        terms = {
            "compound": Security.of(
                "P", SecurityKind.PREFERRED, 1000, preferred_rate="0.08"
            ),
            "simple": Security.of(
                "P",
                SecurityKind.PREFERRED,
                1000,
                preferred_rate="0.08",
                compounding=False,
            ),
        }
        assert terms["compound"].accrued_at(money(5)) > terms["simple"].accrued_at(money(5))

    def test_a_fractional_year_accrues_a_fraction(self) -> None:
        s = Security.of("P", SecurityKind.PREFERRED, 1000, preferred_rate="0.10")
        half = s.accrued_at(money("0.5"))
        assert ZERO < half < s.accrued_at(ONE)

    def test_common_accrues_nothing(self) -> None:
        s = Security.of("Common", SecurityKind.COMMON, 1000, ownership=1)
        assert s.accrued_at(money(5)) == ZERO
        assert s.claim_at(money(5)) == ZERO

    def test_no_time_means_no_accrual(self) -> None:
        s = Security.of("P", SecurityKind.PREFERRED, 1000, preferred_rate="0.08")
        assert s.accrued_at(ZERO) == ZERO


class TestTheWaterfall:
    def preferred_stack(self) -> list[Security]:
        return [
            Security.of(
                "Preferred", SecurityKind.PREFERRED, 800, preferred_rate="0.08"
            ),
            Security.of("Sponsor common", SecurityKind.COMMON, 100, ownership="0.8"),
            Security.of("Management common", SecurityKind.COMMON, 0, ownership="0.2"),
        ]

    def outcome(self, **kwargs: object) -> Outcome:
        model = case()
        return Outcome.realise(
            transaction(),
            model,
            schedule(model),
            entry_date=CLOSE,
            securities=self.preferred_stack(),
            **kwargs,  # type: ignore[arg-type]
        )

    def test_the_preferred_is_paid_before_the_common(self) -> None:
        outcome = self.outcome()
        preferred = outcome.security("Preferred")
        assert preferred.preferred_paid == preferred.claim
        assert preferred.residual_paid == ZERO
        assert outcome.security("Sponsor common").residual_paid > 0

    def test_the_residual_splits_by_ownership(self) -> None:
        outcome = self.outcome()
        sponsor = outcome.security("Sponsor common")
        management = outcome.security("Management common")
        residual = sponsor.residual_paid + management.residual_paid
        assert is_close(sponsor.residual_paid, residual * money("0.8"), tolerance="1E-15")
        assert is_close(management.residual_paid, residual * money("0.2"), tolerance="1E-15")

    def test_everything_available_is_distributed(self) -> None:
        outcome = self.outcome()
        assert outcome.distributes_everything
        assert outcome.proceeds == outcome.valuation.equity_value

    def test_the_common_takes_the_leverage_the_preferred_does_not(self) -> None:
        outcome = self.outcome()
        preferred = outcome.security("Preferred")
        sponsor = outcome.security("Sponsor common")
        assert preferred.moic is not None and sponsor.moic is not None
        # Same deal, same exit: the instrument with a capped claim earns its
        # coupon and the one behind it earns everything above that.
        assert preferred.moic < sponsor.moic
        assert preferred.irr is not None
        assert round(preferred.irr, 4) == 0.08

    def test_carried_capital_with_no_cheque_still_earns_its_share(self) -> None:
        management = self.outcome().security("Management common")
        assert management.invested == ZERO
        assert management.proceeds > 0
        assert management.moic is None
        assert management.irr is None
        assert management.irr_note == "no capital was invested"

    def test_a_shortfall_is_reported_rather_than_hidden(self) -> None:
        # An exit multiple low enough that the preferred is not made whole.
        outcome = self.outcome(exit_multiple="4.2")
        preferred = outcome.security("Preferred")
        assert preferred.shortfall > 0
        assert preferred.preferred_paid < preferred.claim
        assert outcome.security("Sponsor common").proceeds == ZERO

    def test_a_wipeout_reports_no_rate_and_says_why(self) -> None:
        outcome = self.outcome(exit_multiple="0.5")
        assert outcome.valuation.is_wiped_out
        assert outcome.proceeds == ZERO
        for row in outcome:
            assert row.irr is None
        assert "wiped out" in outcome.security("Preferred").irr_note
        assert outcome.irr is None

    def test_seniority_ranks_the_preferred_among_itself(self) -> None:
        model = case()
        securities = [
            Security.of(
                "Junior preferred",
                SecurityKind.PREFERRED,
                600,
                preferred_rate="0.10",
                seniority=1,
            ),
            Security.of(
                "Senior preferred",
                SecurityKind.PREFERRED,
                600,
                preferred_rate="0.06",
                seniority=0,
            ),
            Security.of("Common", SecurityKind.COMMON, 10, ownership=1),
        ]
        outcome = Outcome.realise(
            transaction(),
            model,
            schedule(model),
            entry_date=CLOSE,
            securities=securities,
            exit_multiple="5.4",
        )
        senior = outcome.security("Senior preferred")
        junior = outcome.security("Junior preferred")
        assert senior.preferred_paid == senior.claim
        assert junior.shortfall > 0
        assert outcome.security("Common").proceeds == ZERO

    def test_two_preferred_at_the_same_rank_share_pro_rata(self) -> None:
        model = case()
        securities = [
            Security.of("A", SecurityKind.PREFERRED, 900, preferred_rate="0.08"),
            Security.of("B", SecurityKind.PREFERRED, 300, preferred_rate="0.08"),
            Security.of("Common", SecurityKind.COMMON, 10, ownership=1),
        ]
        outcome = Outcome.realise(
            transaction(),
            model,
            schedule(model),
            entry_date=CLOSE,
            securities=securities,
            exit_multiple="5.4",
        )
        a, b = outcome.security("A"), outcome.security("B")
        assert a.shortfall > 0 and b.shortfall > 0
        # Both short by the same proportion of their claims.
        assert is_close(
            a.preferred_paid / a.claim, b.preferred_paid / b.claim, tolerance="1E-15"
        )

    def test_a_participating_preferred_takes_its_claim_and_a_share(self) -> None:
        model = case()
        plain = [
            Security.of("P", SecurityKind.PREFERRED, 800, preferred_rate="0.08"),
            Security.of("Common", SecurityKind.COMMON, 200, ownership=1),
        ]
        participating = [
            Security.of(
                "P", SecurityKind.PREFERRED, 800, preferred_rate="0.08", ownership="0.5"
            ),
            Security.of("Common", SecurityKind.COMMON, 200, ownership="0.5"),
        ]
        built = [
            Outcome.realise(
                transaction(), model, schedule(model), entry_date=CLOSE, securities=s
            )
            for s in (plain, participating)
        ]
        assert built[0].security("P").residual_paid == ZERO
        assert built[1].security("P").residual_paid > 0
        assert built[1].security("P").proceeds > built[0].security("P").proceeds
        assert built[1].security("Common").proceeds < built[0].security("Common").proceeds
        # The pot is the same either way; only its division changed.
        assert built[0].proceeds == built[1].proceeds


class TestDealLevelFigures:
    def test_the_flat_multiple_is_the_default_case(self) -> None:
        assert realise().valuation.multiple == money(9)

    def test_a_higher_exit_multiple_is_worth_more(self) -> None:
        assert realise(exit_multiple=11).proceeds > realise().proceeds

    def test_the_holding_period_is_the_elapsed_time(self) -> None:
        outcome = realise()
        assert outcome.valuation.when == EXIT
        assert round(Decimal(outcome.holding_period_years), 2) == Decimal("5.00")

    def test_moic_and_irr_agree_with_each_other(self) -> None:
        outcome = realise()
        assert outcome.moic is not None and outcome.irr is not None
        years = float(outcome.holding_period_years)
        implied = float(outcome.moic) ** (1 / years) - 1
        assert outcome.irr == pytest.approx(implied, abs=1e-9)

    def test_profit_is_proceeds_less_capital(self) -> None:
        outcome = realise()
        assert outcome.profit == outcome.proceeds - outcome.invested

    def test_a_security_can_be_read_back_by_name(self) -> None:
        outcome = realise()
        assert outcome.security("Sponsor equity").name == "Sponsor equity"
        with pytest.raises(KeyError):
            outcome.security("Preferred")

    def test_the_outcome_iterates_over_its_securities(self) -> None:
        outcome = realise()
        assert len(outcome) == 1
        assert [r.name for r in outcome] == ["Sponsor equity"]


class TestAttribution:
    def test_the_bridge_ties_to_the_change_in_equity_value(self) -> None:
        for multiple in ("6", "9", "13"):
            attribution = realise(exit_multiple=multiple).attribution
            assert attribution.reconciles()
            assert is_close(attribution.total, attribution.value_created, tolerance="1E-20")

    def test_growth_is_valued_at_the_entry_multiple(self) -> None:
        model = case()
        outcome = Outcome.realise(
            transaction(), model, schedule(model), entry_date=CLOSE, exit_multiple=11
        )
        expected = (model.exit_ebitda - money(200)) * money(9)
        assert outcome.attribution.ebitda_growth == expected

    def test_the_multiple_line_carries_the_cross_term(self) -> None:
        model = case()
        outcome = Outcome.realise(
            transaction(), model, schedule(model), entry_date=CLOSE, exit_multiple=11
        )
        expected = (money(11) - money(9)) * model.exit_ebitda
        assert outcome.attribution.multiple_change == expected

    def test_a_flat_multiple_contributes_nothing(self) -> None:
        assert realise().attribution.multiple_change == ZERO

    def test_a_contraction_contributes_negatively(self) -> None:
        assert realise(exit_multiple=7).attribution.multiple_change < 0

    def test_paydown_measures_the_schedules_work(self) -> None:
        model = case()
        run = schedule(model)
        outcome = Outcome.realise(transaction(), model, run, entry_date=CLOSE)
        expected = money(1000) - run.closing_net_debt
        assert outcome.attribution.debt_paydown == expected
        assert outcome.attribution.debt_paydown > 0

    def test_costs_are_the_fees_the_equity_funded(self) -> None:
        model = case()
        t = transaction(fee_rate="0.02")
        outcome = Outcome.realise(
            t, model, schedule(model), entry_date=CLOSE, exit_fee_rate="0.01"
        )
        expected = -(t.transaction_fees + outcome.valuation.fees)
        assert is_close(outcome.attribution.costs, expected, tolerance="1E-15")
        assert outcome.attribution.reconciles()

    def test_a_deal_with_no_costs_has_no_cost_line(self) -> None:
        assert realise().attribution.costs == ZERO

    def test_shares_of_the_bridge_sum_to_one_in_magnitude(self) -> None:
        attribution = realise(exit_multiple=11).attribution
        components = [
            attribution.ebitda_growth,
            attribution.multiple_change,
            attribution.debt_paydown,
            attribution.costs,
        ]
        total = sum(abs(attribution.share(c)) for c in components)
        assert is_close(total, ONE, tolerance="1E-15")

    def test_a_wipeout_separates_the_bridge_from_the_distribution(self) -> None:
        outcome = realise(exit_multiple="0.4")
        attribution = outcome.attribution
        assert attribution.realised < 0
        assert attribution.reconciles()
        # Nothing is distributed, and the difference is the loss the lenders take.
        assert attribution.distributed == ZERO
        assert attribution.floored > 0
        assert outcome.proceeds == ZERO


class TestAgainstIndependentArithmetic:
    """A deal worked end to end by hand."""

    def test_a_hand_computed_exit(self) -> None:
        # 1,000 of revenue growing 5%, 20% margin: exit EBITDA is 1000 x 1.05^5
        # x 0.20 = 255.256..., and the case is bought and sold at 9.0x.
        model = case()
        expected_ebitda = money(1000) * money("1.05") ** 5 * money("0.20")
        assert is_close(model.exit_ebitda, expected_ebitda, tolerance="1E-20")

        run = schedule(model)
        t = transaction()
        outcome = Outcome.realise(t, model, run, entry_date=CLOSE)

        # Enterprise value is 9.0x the exit figure; equity is what is left after
        # the debt the schedule did not repay.
        assert outcome.valuation.enterprise_value == model.exit_ebitda * money(9)
        assert outcome.valuation.equity_value == (
            outcome.valuation.enterprise_value - run.closing_debt + run.closing_cash
        )

        # No fees anywhere, so the cheque is the implied equity value at close:
        # 9.0 x 200 of enterprise value less 1,000 of debt is 800.
        assert t.sponsor_equity == money(800)
        assert outcome.invested == money(800)
        assert outcome.attribution.costs == ZERO

        # And the three real components explain the whole gain.
        a = outcome.attribution
        assert a.multiple_change == ZERO
        assert is_close(
            a.ebitda_growth + a.debt_paydown, outcome.proceeds - money(800), tolerance="1E-15"
        )

        # The rate is the multiple annualised over the elapsed period.
        assert outcome.moic is not None and outcome.irr is not None
        assert outcome.irr == pytest.approx(
            float(outcome.moic) ** (1 / float(outcome.holding_period_years)) - 1, abs=1e-9
        )
