from decimal import Decimal

import pytest

from capstack.money import ZERO, money
from capstack.operating import apply_carryforward
from capstack.tax import (
    DEFAULT_INTEREST_LIMIT_RATE,
    EarningsBase,
    InterestLimit,
    LeveredTaxPeriod,
    TaxPolicy,
    TaxPolicyError,
    levered_tax,
    levered_tax_schedule,
)

RATE = "0.25"


def policy(
    *,
    rate: str = RATE,
    limit: InterestLimit | None = None,
    nol_usage_limit: str = "0.80",
    deduct_accrued_interest: bool = True,
) -> TaxPolicy:
    return TaxPolicy.of(
        rate,
        nol_usage_limit=nol_usage_limit,
        interest_limit=limit,
        deduct_accrued_interest=deduct_accrued_interest,
    )


def one(
    *,
    ebitda: str = "100",
    ebit: str = "80",
    cash_interest: str = "0",
    accrued_interest: str = "0",
    opening_carryforward: str = "0",
    opening_disallowed_interest: str = "0",
    pol: TaxPolicy | None = None,
) -> LeveredTaxPeriod:
    return levered_tax(
        index=1,
        ebitda=money(ebitda),
        ebit=money(ebit),
        cash_interest=money(cash_interest),
        accrued_interest=money(accrued_interest),
        opening_carryforward=money(opening_carryforward),
        opening_disallowed_interest=money(opening_disallowed_interest),
        policy=pol if pol is not None else policy(),
    )


# -- The deduction itself ------------------------------------------------


def test_interest_reduces_taxable_income_pound_for_pound_when_uncapped() -> None:
    row = one(ebit="80", cash_interest="30")
    assert row.taxable_income == money("50")
    assert row.cash_tax == money("12.50")


def test_tax_without_interest_matches_the_unlevered_computation_exactly() -> None:
    # The new path against the one already in the model. They share no code, so
    # agreement at zero interest is a cross-check rather than a tautology.
    for ebit, pool in (("80", "0"), ("80", "40"), ("-20", "0"), ("-20", "15"), ("0", "10")):
        levered = one(ebit=ebit, cash_interest="0", opening_carryforward=pool)
        unlevered = apply_carryforward(money(ebit), money(pool), money(RATE))
        assert levered.cash_tax == unlevered.cash_tax
        assert levered.closing_carryforward == unlevered.closing_carryforward
        assert levered.loss_relief_used == unlevered.loss_relief_used
        assert levered.taxable_after_relief == unlevered.taxable_after_relief


def test_a_deduction_that_creates_a_loss_adds_the_levered_loss_to_the_pool() -> None:
    row = one(ebit="20", cash_interest="50")
    assert row.taxable_income == money("-30")
    assert row.cash_tax == ZERO
    assert row.closing_carryforward == money("30")


def test_interest_is_offered_whether_it_is_paid_in_cash_or_accrued() -> None:
    row = one(ebit="80", cash_interest="10", accrued_interest="20")
    assert row.deduction.arising == money("30")
    assert row.taxable_income == money("50")


def test_a_regime_that_waits_for_the_cash_defers_the_accrued_part_to_the_pool() -> None:
    row = one(
        ebit="80",
        cash_interest="10",
        accrued_interest="20",
        pol=policy(deduct_accrued_interest=False),
    )
    assert row.deduction.arising == money("10")
    assert row.deduction.deductible == money("10")
    # The accrued margin is not lost. It waits, and is offered again later.
    assert row.deduction.carried_out == money("20")
    assert row.taxable_income == money("70")


def test_deferred_accrued_interest_is_deductible_in_a_later_period() -> None:
    schedule = levered_tax_schedule(
        ebitda=[money("100")] * 2,
        ebit=[money("80")] * 2,
        cash_interest=[money("10"), money("10")],
        accrued_interest=[money("20"), ZERO],
        policy=policy(deduct_accrued_interest=False),
    )
    assert schedule[0].deduction.deductible == money("10")
    # Period two offers its own cash interest plus the balance waiting from one.
    assert schedule[1].deduction.deductible == money("30")
    assert schedule.closing_disallowed_interest == ZERO


# -- The cap ------------------------------------------------------------


def test_the_cap_refuses_interest_above_its_allowance() -> None:
    row = one(ebitda="100", ebit="80", cash_interest="45", pol=policy(limit=InterestLimit.of()))
    assert row.deduction.allowance == money("30")
    assert row.deduction.deductible == money("30")
    assert row.deduction.disallowed == money("15")
    assert row.deduction.binding
    assert row.taxable_income == money("50")


def test_a_structure_inside_the_cap_is_untouched_by_it() -> None:
    row = one(ebitda="100", ebit="80", cash_interest="20", pol=policy(limit=InterestLimit.of()))
    assert row.deduction.deductible == money("20")
    assert row.deduction.disallowed == ZERO
    assert not row.deduction.binding
    assert row.deduction.headroom == money("10")


def test_the_same_deal_passes_on_an_ebitda_base_and_binds_on_an_ebit_base() -> None:
    # D&A of 20 on EBITDA of 100 puts the two allowances at 30 and 24. Interest
    # of 28 sits between them, so the base alone decides whether the cap bites.
    on_ebitda = one(
        ebitda="100",
        ebit="80",
        cash_interest="28",
        pol=policy(limit=InterestLimit.of(base=EarningsBase.EBITDA)),
    )
    on_ebit = one(
        ebitda="100",
        ebit="80",
        cash_interest="28",
        pol=policy(limit=InterestLimit.of(base=EarningsBase.EBIT)),
    )

    assert on_ebitda.deduction.allowance == money("30")
    assert not on_ebitda.deduction.binding
    assert on_ebit.deduction.allowance == money("24")
    assert on_ebit.deduction.disallowed == money("4")
    assert on_ebit.cash_tax > on_ebitda.cash_tax


def test_the_ebit_base_is_the_harsher_of_the_two_whenever_there_is_any_depreciation() -> None:
    for da in ("1", "10", "40"):
        ebitda = money("100")
        ebit = ebitda - money(da)
        a = one(
            ebitda="100",
            ebit=str(ebit),
            cash_interest="35",
            pol=policy(limit=InterestLimit.of(base=EarningsBase.EBITDA)),
        )
        b = one(
            ebitda="100",
            ebit=str(ebit),
            cash_interest="35",
            pol=policy(limit=InterestLimit.of(base=EarningsBase.EBIT)),
        )
        assert b.deduction.allowance is not None
        assert a.deduction.allowance is not None
        assert b.deduction.allowance < a.deduction.allowance


def test_a_loss_making_period_gets_an_allowance_of_zero_not_a_negative_one() -> None:
    row = one(ebitda="-50", ebit="-70", cash_interest="10", pol=policy(limit=InterestLimit.of()))
    assert row.deduction.allowance == ZERO
    assert row.deduction.deductible == ZERO
    assert row.deduction.carried_out == money("10")


def test_no_limit_means_the_allowance_is_absent_rather_than_infinite() -> None:
    row = one(ebit="80", cash_interest="300", pol=policy(limit=None))
    assert row.deduction.allowance is None
    assert row.deduction.deductible == money("300")
    assert row.deduction.headroom == ZERO


def test_a_zero_rate_cap_refuses_everything() -> None:
    row = one(ebit="80", cash_interest="10", pol=policy(limit=InterestLimit.of("0")))
    assert row.deduction.allowance == ZERO
    assert row.deduction.deductible == ZERO
    assert row.taxable_income == money("80")


# -- The disallowed pool ------------------------------------------------


def test_disallowed_interest_is_tested_again_next_period_alongside_new_interest() -> None:
    schedule = levered_tax_schedule(
        ebitda=[money("100"), money("200")],
        ebit=[money("80"), money("180")],
        cash_interest=[money("45"), money("20")],
        policy=policy(limit=InterestLimit.of()),
    )
    assert schedule[0].deduction.disallowed == money("15")
    # Period two offers 20 of its own plus the 15 carried, against a cap of 60.
    assert schedule[1].deduction.offered == money("35")
    assert schedule[1].deduction.deductible == money("35")
    assert schedule.closing_disallowed_interest == ZERO


def test_a_pool_can_stay_stuck_behind_the_cap_for_the_whole_hold() -> None:
    schedule = levered_tax_schedule(
        ebitda=[money("100")] * 5,
        ebit=[money("80")] * 5,
        cash_interest=[money("45")] * 5,
        policy=policy(limit=InterestLimit.of()),
    )
    # Fifteen refused every period and nothing ever frees it, so it accumulates.
    assert schedule.closing_disallowed_interest == money("75")
    assert schedule.binding_periods == (1, 2, 3, 4, 5)
    assert schedule.total_interest_arising == money("225")
    assert schedule.total_interest_deducted == money("150")


def test_an_opening_pool_is_offered_in_the_first_period() -> None:
    row = one(
        ebitda="100",
        ebit="80",
        cash_interest="10",
        opening_disallowed_interest="12",
        pol=policy(limit=InterestLimit.of()),
    )
    assert row.deduction.carried_in == money("12")
    assert row.deduction.deductible == money("22")


def test_which_of_current_and_carried_interest_is_deemed_deducted_changes_nothing() -> None:
    # The claim the aggregate treatment rests on: with no expiry on the pool,
    # splitting the allowance between current and carried interest in any
    # proportion leaves the same cash tax and the same closing balance.
    row = one(
        ebitda="100",
        ebit="80",
        cash_interest="25",
        opening_disallowed_interest="25",
        pol=policy(limit=InterestLimit.of()),
    )
    assert row.deduction.offered == money("50")
    assert row.deduction.deductible == money("30")
    assert row.deduction.carried_out == money("20")
    # Whichever 30 of the 50 is labelled deducted, 20 remains and the income is
    # the same, because the pool that receives it is a single undated balance.
    assert row.taxable_income == money("50")


# -- Ordering against the loss pool -------------------------------------


def test_the_cap_is_struck_before_relief_so_the_loss_pool_cannot_enlarge_it() -> None:
    # A pool large enough to shelter the period does not raise the allowance:
    # the earnings the cap is measured against are taken before relief.
    with_pool = one(
        ebitda="100",
        ebit="80",
        cash_interest="45",
        opening_carryforward="60",
        pol=policy(limit=InterestLimit.of()),
    )
    without_pool = one(
        ebitda="100", ebit="80", cash_interest="45", pol=policy(limit=InterestLimit.of())
    )
    assert with_pool.deduction.allowance == without_pool.deduction.allowance
    assert with_pool.deduction.deductible == without_pool.deduction.deductible


def test_relief_applies_to_what_the_interest_deduction_leaves_behind() -> None:
    row = one(
        ebitda="100",
        ebit="80",
        cash_interest="30",
        opening_carryforward="100",
        pol=policy(limit=InterestLimit.of()),
    )
    # Income after the deduction is 50; the 80% limit shelters 40 of it.
    assert row.taxable_income == money("50")
    assert row.loss_relief_used == money("40")
    assert row.taxable_after_relief == money("10")
    assert row.cash_tax == money("2.50")
    assert row.closing_carryforward == money("60")


def test_reversing_the_order_would_change_the_answer_and_here_is_the_case() -> None:
    # Relief first would shelter 80% of 80 = 64, leaving 16, and the 30 of
    # interest would then take that to -14: no tax, a pool of 14, and 36 of the
    # original 100 left. The implemented order pays 2.50 and keeps 60.
    row = one(
        ebitda="100",
        ebit="80",
        cash_interest="30",
        opening_carryforward="100",
        pol=policy(limit=InterestLimit.of()),
    )
    assert (row.cash_tax, row.closing_carryforward) == (money("2.50"), money("60"))
    assert (row.cash_tax, row.closing_carryforward) != (ZERO, money("50"))


# -- Conservation identities, as equalities -----------------------------


def test_every_unit_of_interest_is_either_deducted_or_in_the_pool() -> None:
    schedule = levered_tax_schedule(
        ebitda=[money("100"), money("40"), money("250"), money("-10"), money("300")],
        ebit=[money("80"), money("20"), money("220"), money("-30"), money("260")],
        cash_interest=[money("45"), money("50"), money("20"), money("55"), money("10")],
        accrued_interest=[money("5"), ZERO, money("8"), ZERO, money("2")],
        policy=policy(limit=InterestLimit.of()),
        opening_disallowed_interest="7",
    )
    arising = schedule.total_interest_arising
    assert (
        schedule.total_interest_deducted + schedule.closing_disallowed_interest
        == arising + schedule.opening_disallowed_interest
    )


def test_the_loss_pool_closes_at_opening_plus_losses_added_less_relief_used() -> None:
    schedule = levered_tax_schedule(
        ebitda=[money("100"), money("40"), money("250"), money("-10"), money("300")],
        ebit=[money("80"), money("20"), money("220"), money("-30"), money("260")],
        cash_interest=[money("45"), money("50"), money("20"), money("55"), money("10")],
        policy=policy(limit=InterestLimit.of()),
        opening_carryforward="30",
    )
    losses = sum((-p.taxable_income for p in schedule if p.taxable_income < 0), ZERO)
    relief = sum((p.loss_relief_used for p in schedule), ZERO)
    assert schedule.closing_carryforward == schedule.opening_carryforward + losses - relief


def test_neither_pool_ever_goes_negative() -> None:
    schedule = levered_tax_schedule(
        ebitda=[money("100"), money("40"), money("250"), money("-10"), money("300")],
        ebit=[money("80"), money("20"), money("220"), money("-30"), money("260")],
        cash_interest=[money("45"), money("50"), money("20"), money("55"), money("10")],
        policy=policy(limit=InterestLimit.of()),
        opening_carryforward="30",
        opening_disallowed_interest="7",
    )
    for p in schedule:
        assert p.closing_carryforward >= ZERO
        assert p.deduction.carried_out >= ZERO
        assert p.deduction.deductible >= ZERO
        assert p.cash_tax >= ZERO


def test_cash_tax_is_the_rate_on_income_after_relief_in_every_period() -> None:
    schedule = levered_tax_schedule(
        ebitda=[money("100"), money("40"), money("250")],
        ebit=[money("80"), money("20"), money("220")],
        cash_interest=[money("45"), money("50"), money("20")],
        policy=policy(limit=InterestLimit.of()),
    )
    for p in schedule:
        assert p.cash_tax == p.taxable_after_relief * money(RATE)


# -- What the saving actually is -----------------------------------------


def test_the_saving_is_the_rate_on_the_deduction_in_the_plain_case() -> None:
    row = one(ebit="80", cash_interest="30")
    assert row.shield == money("7.50")
    assert row.shield == row.naive_shield


def test_the_naive_saving_overstates_it_when_the_deduction_creates_a_loss() -> None:
    # EBIT of 20 against 50 of interest. Only 20 of the deduction meets income;
    # the rest becomes a pool. Rate times deduction books 12.50 of saving where
    # the period can only ever have saved the 5 it was going to pay.
    row = one(ebit="20", cash_interest="50")
    assert row.cash_tax_unlevered == money("5")
    assert row.cash_tax == ZERO
    assert row.shield == money("5")
    assert row.naive_shield == money("12.50")
    assert row.shield < row.naive_shield


def test_the_naive_saving_overstates_it_when_the_period_is_already_sheltered() -> None:
    # Relief has already taken 80% of the income away, so the deduction lands on
    # the fifth that was left and saves the rate on that fifth, not on itself.
    row = one(ebit="100", cash_interest="40", opening_carryforward="500")
    assert row.cash_tax_unlevered == money("5")
    assert row.cash_tax == money("3")
    assert row.shield == money("2")
    assert row.naive_shield == money("10")


def test_the_saving_is_never_more_than_the_naive_figure() -> None:
    schedule = levered_tax_schedule(
        ebitda=[money("100"), money("40"), money("250"), money("-10"), money("300")],
        ebit=[money("80"), money("20"), money("220"), money("-30"), money("260")],
        cash_interest=[money("45"), money("50"), money("20"), money("55"), money("10")],
        policy=policy(limit=InterestLimit.of()),
        opening_carryforward="30",
    )
    for p in schedule:
        assert p.shield <= p.naive_shield
    assert schedule.total_shield <= schedule.total_naive_shield
    assert schedule.total_shield == schedule.total_cash_tax_unlevered - schedule.total_cash_tax


def test_the_cap_costs_cash_tax_and_the_difference_prices_it() -> None:
    ebitda = [money("100")] * 5
    ebit = [money("80")] * 5
    interest = [money("45")] * 5
    capped = levered_tax_schedule(
        ebitda=ebitda, ebit=ebit, cash_interest=interest, policy=policy(limit=InterestLimit.of())
    )
    uncapped = levered_tax_schedule(
        ebitda=ebitda, ebit=ebit, cash_interest=interest, policy=policy(limit=None)
    )
    # 75 of interest refused over the hold, taxed at 25%: 18.75 of extra tax.
    assert capped.total_cash_tax - uncapped.total_cash_tax == money("18.75")
    assert uncapped.deducted_share == money(1)
    assert capped.deducted_share == money("150") / money("225")


# -- Degenerate and invalid inputs ---------------------------------------


def test_an_empty_schedule_reports_its_opening_pools() -> None:
    schedule = levered_tax_schedule(
        ebitda=[], ebit=[], cash_interest=[], policy=policy(), opening_carryforward="12"
    )
    assert len(schedule) == 0
    assert schedule.closing_carryforward == money("12")
    assert schedule.closing_disallowed_interest == ZERO
    assert schedule.total_cash_tax == ZERO
    assert schedule.deducted_share == ZERO


def test_a_zero_tax_rate_pays_nothing_but_still_rolls_the_pools() -> None:
    schedule = levered_tax_schedule(
        ebitda=[money("100")] * 2,
        ebit=[money("80")] * 2,
        cash_interest=[money("45")] * 2,
        policy=policy(rate="0", limit=InterestLimit.of()),
    )
    assert schedule.total_cash_tax == ZERO
    assert schedule.total_shield == ZERO
    assert schedule.closing_disallowed_interest == money("30")


def test_periods_are_indexed_from_one_by_default_and_can_be_supplied() -> None:
    default = levered_tax_schedule(
        ebitda=[money("100")] * 3, ebit=[money("80")] * 3, cash_interest=[ZERO] * 3, policy=policy()
    )
    assert [p.index for p in default] == [1, 2, 3]
    supplied = levered_tax_schedule(
        ebitda=[money("100")] * 3,
        ebit=[money("80")] * 3,
        cash_interest=[ZERO] * 3,
        indices=[0, 1, 2],
        policy=policy(),
    )
    assert [p.index for p in supplied] == [0, 1, 2]


@pytest.mark.parametrize("bad", ["-0.01", "1.01"])
def test_a_tax_rate_outside_zero_to_one_is_refused(bad: str) -> None:
    with pytest.raises(TaxPolicyError):
        TaxPolicy.of(bad)


@pytest.mark.parametrize("bad", ["-0.01", "1.01"])
def test_a_loss_usage_limit_outside_zero_to_one_is_refused(bad: str) -> None:
    with pytest.raises(TaxPolicyError):
        TaxPolicy.of(RATE, nol_usage_limit=bad)


def test_a_negative_cap_rate_is_refused() -> None:
    with pytest.raises(TaxPolicyError):
        InterestLimit.of("-0.1")


def test_negative_interest_is_refused() -> None:
    with pytest.raises(TaxPolicyError):
        one(cash_interest="-1")
    with pytest.raises(TaxPolicyError):
        one(accrued_interest="-1")


def test_a_negative_opening_pool_is_refused() -> None:
    with pytest.raises(TaxPolicyError):
        one(opening_carryforward="-1")
    with pytest.raises(TaxPolicyError):
        one(opening_disallowed_interest="-1")


def test_mismatched_series_lengths_are_refused() -> None:
    with pytest.raises(TaxPolicyError):
        levered_tax_schedule(
            ebitda=[money("100")] * 3,
            ebit=[money("80")] * 2,
            cash_interest=[ZERO] * 3,
            policy=policy(),
        )


def test_a_wrong_number_of_indices_is_refused() -> None:
    with pytest.raises(TaxPolicyError):
        levered_tax_schedule(
            ebitda=[money("100")] * 3,
            ebit=[money("80")] * 3,
            cash_interest=[ZERO] * 3,
            indices=[1, 2],
            policy=policy(),
        )


def test_the_default_cap_rate_is_thirty_per_cent_on_an_ebitda_base() -> None:
    assert money("0.30") == DEFAULT_INTEREST_LIMIT_RATE
    limit = InterestLimit.of()
    assert limit.base is EarningsBase.EBITDA
    assert limit.allowance(ebitda=money("1000"), ebit=money("600")) == money("300")


def test_the_figures_are_exact_decimals_not_floats() -> None:
    row = one(ebit="80", cash_interest="33.33")
    assert isinstance(row.cash_tax, Decimal)
    assert row.taxable_income == money("46.67")
    assert row.cash_tax == money("11.6675")
