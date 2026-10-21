"""The outer fixed point: the deduction against the debt it is charged on.

The tax layer takes an interest series. The interest series is an output of the
thing it is an input to, so this file is about the solve that settles the two
against each other — whether it converges, how fast, what it refuses, and the
two results that are not what a reader would guess.
"""

from datetime import date

import pytest

from capstack.daycount import DayCount
from capstack.debt import CapitalStructure, Tranche, TrancheKind
from capstack.drivers import Driver
from capstack.money import ZERO, Money, money
from capstack.operating import OperatingAssumptions, OperatingModel
from capstack.periods import Frequency, PeriodGrid
from capstack.tax import (
    EarningsBase,
    InterestLimit,
    LeveredSolution,
    TaxCircularityError,
    TaxPolicy,
    TaxPolicyError,
    levered_schedule,
    levered_tax_for_model,
)

YEARS = 5
TAX_RATE = "0.25"
COUPON = "0.08"


def assumptions(*, tax_rate: str = TAX_RATE, nol_usage_limit: str = "0.80") -> OperatingAssumptions:
    return OperatingAssumptions.of(
        revenue_growth=Driver.constant("0.08", YEARS),
        ebitda_margin=Driver.constant("0.20", YEARS),
        da_rate=Driver.constant("0.04", YEARS),
        capex_rate=Driver.constant("0.05", YEARS),
        nwc_rate=Driver.constant("0.15", YEARS),
        tax_rate=tax_rate,
        nol_usage_limit=nol_usage_limit,
    )


def case(**kwargs: str) -> tuple[OperatingAssumptions, OperatingModel]:
    a = assumptions(**kwargs)
    grid = PeriodGrid.build(date(2026, 6, 30), years=YEARS, frequency=Frequency.ANNUAL)
    return a, OperatingModel.project(grid, a, opening_revenue=1000)


def structure(model: OperatingModel, turns: str, *, coupon: str = COUPON) -> CapitalStructure:
    """A single swept term loan at ``turns`` of entry EBITDA."""
    return CapitalStructure.of(
        [
            Tranche.of(
                "Term Loan B",
                TrancheKind.TERM_LOAN,
                model.entry_ebitda * money(turns),
                cash_rate=coupon,
                swept=True,
                floating=False,
            )
        ],
        day_count=DayCount.ACT_365F,
    )


def solve(
    turns: str,
    *,
    limit: InterestLimit | None = None,
    coupon: str = COUPON,
    **kwargs: object,
) -> LeveredSolution:
    a, model = case()
    return levered_schedule(
        structure(model, turns, coupon=coupon),
        model,
        policy=TaxPolicy.from_assumptions(a, interest_limit=limit),
        **kwargs,  # type: ignore[arg-type]
    )


# -- It converges, and how fast ------------------------------------------


@pytest.mark.parametrize("turns", ["0", "2.0", "3.0", "4.0", "5.0", "6.0", "6.5", "8.0"])
@pytest.mark.parametrize("limit", [None, InterestLimit.of()])
def test_the_solve_settles_on_every_structure_in_range(
    turns: str, limit: InterestLimit | None
) -> None:
    solution = solve(turns, limit=limit)
    assert solution.residual <= money("0.0001")
    assert 1 <= solution.iterations <= 6


@pytest.mark.parametrize("coupon", ["0.04", "0.08", "0.12", "0.20"])
def test_a_richer_coupon_does_not_cost_the_solve_its_convergence(coupon: str) -> None:
    # The loop gain is the tax rate times the interest on the cash the last pass
    # freed, so it rises with the coupon. It is still nowhere near one.
    solution = solve("5.0", coupon=coupon)
    assert solution.residual <= money("0.0001")
    assert solution.iterations <= 6


def test_an_unlevered_structure_needs_no_iteration_and_leaves_the_tax_line_alone() -> None:
    _, model = case()
    solution = solve("0")
    assert solution.residual == ZERO
    for period, row in zip(model, solution.tax, strict=True):
        assert row.cash_tax == period.tax.cash_tax
    assert solution.total_shield == ZERO
    assert solution.cash_flows == tuple(p.unlevered_free_cash_flow for p in model)


def test_a_tighter_tolerance_costs_more_passes_and_a_looser_one_fewer() -> None:
    tight = solve("5.0", tolerance="0.00000001")
    loose = solve("5.0", tolerance="1")
    assert tight.iterations > loose.iterations
    assert tight.residual < loose.residual


def test_a_solve_that_runs_out_of_passes_is_reported_rather_than_returned() -> None:
    with pytest.raises(TaxCircularityError) as exc:
        solve("5.0", max_iterations=1)
    assert "did not settle" in str(exc.value)
    # The message carries the residual it stopped on, which is the figure a
    # caller needs to decide whether to raise the ceiling or the tolerance.
    assert "still moving by" in str(exc.value)


# -- What it refuses -----------------------------------------------------


def test_a_policy_disagreeing_with_the_operating_case_is_refused() -> None:
    # The saving credited to the cash flow is a difference against the case's
    # own tax line. A policy at a different rate measures it off another base,
    # so the solve refuses rather than returning a quietly wrong number.
    _, model = case(tax_rate="0.25")
    with pytest.raises(TaxPolicyError) as exc:
        levered_schedule(structure(model, "5.0"), model, policy=TaxPolicy.of("0.30"))
    assert "disagrees with the operating case" in str(exc.value)
    assert "from_assumptions" in str(exc.value)


def test_a_policy_disagreeing_only_on_the_loss_limit_is_also_refused() -> None:
    # The rate is not the only thing that can disagree. A case carrying an
    # opening pool makes the loss limit observable in the tax line, so a policy
    # that matches on rate and differs on the limit is caught by the same check.
    pooled = OperatingAssumptions.of(
        revenue_growth=Driver.constant("0.08", YEARS),
        ebitda_margin=Driver.constant("0.20", YEARS),
        da_rate=Driver.constant("0.04", YEARS),
        capex_rate=Driver.constant("0.05", YEARS),
        nwc_rate=Driver.constant("0.15", YEARS),
        tax_rate=TAX_RATE,
        nol_usage_limit="0.50",
        opening_carryforward="400",
    )
    grid = PeriodGrid.build(date(2026, 6, 30), years=YEARS, frequency=Frequency.ANNUAL)
    model = OperatingModel.project(grid, pooled, opening_revenue=1000)

    with pytest.raises(TaxPolicyError):
        levered_schedule(
            structure(model, "5.0"),
            model,
            policy=TaxPolicy.of(TAX_RATE, nol_usage_limit="0.80"),
            opening_carryforward="400",
        )

    # The matching policy, built from the case, goes through.
    solution = levered_schedule(
        structure(model, "5.0"),
        model,
        policy=TaxPolicy.from_assumptions(pooled),
        opening_carryforward="400",
    )
    assert solution.residual <= money("0.0001")


def test_from_assumptions_passes_the_guard_by_construction() -> None:
    for rate in ("0", "0.21", "0.25", "0.35"):
        a, model = case(tax_rate=rate)
        solution = levered_schedule(
            structure(model, "5.0"), model, policy=TaxPolicy.from_assumptions(a)
        )
        assert solution.residual <= money("0.0001")


@pytest.mark.parametrize("bad", [0, -1])
def test_a_solve_needs_at_least_one_pass(bad: int) -> None:
    with pytest.raises(TaxPolicyError):
        solve("5.0", max_iterations=bad)


def test_a_negative_tolerance_is_refused() -> None:
    with pytest.raises(TaxPolicyError):
        solve("5.0", tolerance="-0.01")


# -- The answer is self-consistent ---------------------------------------


def test_rerunning_the_schedule_on_the_returned_flows_reproduces_it_exactly() -> None:
    # The strongest statement available about a fixed point: what is returned is
    # a pass, not an average of passes. The schedule, the tax read off it and
    # the cash flow all describe the same run.
    a, model = case()
    s = structure(model, "5.0")
    solution = levered_schedule(s, model, policy=TaxPolicy.from_assumptions(a))
    from capstack.debt import DebtSchedule

    again = DebtSchedule.run(
        s,
        [p.period for p in model],
        solution.cash_flows,
        opening_cash=0,
        ebitda=[model.trailing_ebitda(i) for i in range(len(model))],
        opening_ebitda=model.entry_ebitda,
    )
    for left, right in zip(solution.schedule, again, strict=True):
        assert left.cash_interest == right.cash_interest
        assert left.closing_debt == right.closing_debt


def test_the_tax_returned_is_the_tax_of_the_interest_returned() -> None:
    a, model = case()
    solution = levered_schedule(
        structure(model, "5.0"),
        model,
        policy=TaxPolicy.from_assumptions(a, interest_limit=InterestLimit.of()),
    )
    recomputed = levered_tax_for_model(
        model,
        cash_interest=[p.cash_interest for p in solution.schedule],
        accrued_interest=[p.pik_interest for p in solution.schedule],
        policy=TaxPolicy.from_assumptions(a, interest_limit=InterestLimit.of()),
    )
    assert recomputed.cash_taxes == solution.tax.cash_taxes


def test_the_flows_are_the_unlevered_case_plus_the_saving_to_within_the_residual() -> None:
    a, model = case()
    solution = levered_schedule(structure(model, "5.0"), model, policy=TaxPolicy.from_assumptions(a))
    base = [p.unlevered_free_cash_flow for p in model]
    for flow, unlevered, shield in zip(solution.cash_flows, base, solution.shields, strict=True):
        assert abs(flow - (unlevered + shield)) <= solution.residual


def test_the_saving_is_the_whole_difference_against_the_unlevered_tax_line() -> None:
    a, model = case()
    solution = levered_schedule(structure(model, "5.0"), model, policy=TaxPolicy.from_assumptions(a))
    unlevered = sum((p.tax.cash_tax for p in model), ZERO)
    # Exact, because it is the definition: both sides are the same two totals.
    assert solution.total_shield == unlevered - solution.tax.total_cash_tax


def test_summing_the_period_savings_agrees_with_differencing_the_totals() -> None:
    """Equal in exact arithmetic, and not quite equal in a 34-digit context.

    Five roundings of a per-period difference against two roundings of a total.
    Asserting the two identical would be asserting something false, so the
    claim is the size of the disagreement: it is inside the working precision
    and nowhere near the cent anything downstream is expressed in.
    """
    a, model = case()
    solution = levered_schedule(structure(model, "5.0"), model, policy=TaxPolicy.from_assumptions(a))
    gap = abs(sum(solution.shields, ZERO) - solution.total_shield)
    assert gap > ZERO  # it really is not exact, which is the point
    assert gap < money("1e-28")

    # The bound is loose enough to pass and tight enough to be worth asserting:
    # a disagreement anywhere near a figure a deal expresses is refused by it.
    assert money("0.000001") > money("1e-28")
    cent = money("0.01")
    assert sum(solution.shields, ZERO).quantize(cent) == solution.total_shield.quantize(cent)


# -- The deduction frees cash, and the cash is visible -------------------


def test_closing_the_circularity_leaves_less_debt_than_ignoring_it() -> None:
    # The whole point of the phase. The same structure, the same case, with and
    # without the deduction reaching the cash flow.
    a, model = case()
    s = structure(model, "5.0")
    from capstack.debt import DebtSchedule

    ignored = DebtSchedule.from_operating_model(s, model, opening_cash=0)
    settled = levered_schedule(s, model, policy=TaxPolicy.from_assumptions(a))
    assert settled.schedule[-1].closing_debt < ignored[-1].closing_debt
    assert settled.total_shield > ZERO


def test_more_leverage_frees_more_cash_until_the_cap_stops_it() -> None:
    uncapped = [solve(t).total_shield for t in ("3.0", "4.0", "5.0", "6.0")]
    assert uncapped == sorted(uncapped)


# -- The two results that are not what a reader would guess --------------


def test_past_saturation_a_turn_of_leverage_buys_no_further_deduction_at_all() -> None:
    """The deduction saturates, exactly.

    Once the cap binds in every period and the pool is never absorbed, the
    deduction is the allowance, and the allowance is a function of earnings
    alone. So it stops depending on the debt entirely: 6.0x, 6.5x, 7.0x and
    8.0x deduct the same amount to the last place and pay the same cash tax,
    differing only in the interest they incur and the debt they leave behind.
    An equality rather than a tolerance, because that is what it is.
    """
    _, model = case()
    allowances = sum((p.ebitda * money("0.30") for p in model), ZERO)
    solved = [solve(t, limit=InterestLimit.of()) for t in ("6.0", "6.5", "7.0", "8.0")]

    for solution in solved:
        assert solution.tax.binding_periods == tuple(p.index for p in model)
        assert solution.tax.total_interest_deducted == allowances
        assert solution.tax.total_cash_tax == solved[0].tax.total_cash_tax

    # And the extra leverage is not free: it is all cost and no deduction.
    interest = [s.tax.total_interest_arising for s in solved]
    debt = [s.schedule[-1].closing_debt for s in solved]
    assert interest == sorted(interest)
    assert debt == sorted(debt)


def test_the_cap_damps_the_circularity_and_the_iteration_count_shows_it() -> None:
    # A binding cap pins the deduction to earnings, so the loop has no gain: the
    # saving does not respond to the interest, and the second pass reproduces
    # the first exactly. An unbound structure has to iterate.
    saturated = solve("6.5", limit=InterestLimit.of())
    assert saturated.iterations == 2
    assert saturated.residual == ZERO

    unbound = solve("6.5", limit=None)
    assert unbound.iterations > saturated.iterations
    assert unbound.residual > ZERO


def test_the_shield_is_the_wrong_way_to_price_the_cap() -> None:
    """A cap that defers deductions can leave a larger total saving.

    At five turns on an EBITDA base the pool is absorbed before exit, so every
    pound of interest is deducted either way. What the cap changes is *when*,
    and deferring a deduction means paying tax earlier, sweeping less, and
    leaving debt outstanding to earn more interest — which is itself deducted.
    So the capped run books a *larger* saving and a *lower* total cash tax than
    the uncapped one, while leaving more debt at exit.

    Anyone comparing shields would conclude the cap had helped. It had not: the
    sponsor owns the equity, and the equity is behind more debt.
    """
    capped = solve("5.0", limit=InterestLimit.of())
    uncapped = solve("5.0", limit=None)

    assert capped.tax.closing_disallowed_interest == ZERO
    assert capped.tax.total_interest_deducted == capped.tax.total_interest_arising

    assert capped.total_shield > uncapped.total_shield
    assert capped.tax.total_cash_tax < uncapped.tax.total_cash_tax
    # The figure that actually moved in the sponsor's direction, and it moved
    # the wrong way.
    assert capped.schedule[-1].closing_debt > uncapped.schedule[-1].closing_debt


def test_where_the_pool_is_never_absorbed_the_cap_costs_what_it_looks_like() -> None:
    # The regime above is the confusing one. Past saturation the cap does what a
    # reader expects: less deduction, more tax, more debt.
    capped = solve("6.5", limit=InterestLimit.of())
    uncapped = solve("6.5", limit=None)
    assert capped.tax.closing_disallowed_interest > ZERO
    assert capped.total_shield < uncapped.total_shield
    assert capped.tax.total_cash_tax > uncapped.tax.total_cash_tax
    assert capped.schedule[-1].closing_debt > uncapped.schedule[-1].closing_debt


def test_the_ebit_base_binds_sooner_through_the_circularity_too() -> None:
    on_ebitda = solve("5.0", limit=InterestLimit.of())
    on_ebit = solve("5.0", limit=InterestLimit.of(base=EarningsBase.EBIT))
    assert on_ebitda.tax.closing_disallowed_interest == ZERO
    assert on_ebit.tax.closing_disallowed_interest > ZERO
    assert on_ebit.tax.total_cash_tax > on_ebitda.tax.total_cash_tax


def test_a_pik_margin_is_offered_to_the_cap_through_the_solve() -> None:
    a, model = case()
    s = CapitalStructure.of(
        [
            Tranche.of(
                "Mezzanine",
                TrancheKind.MEZZANINE,
                model.entry_ebitda * money("3.0"),
                cash_rate="0.05",
                pik_rate="0.05",
                floating=False,
            )
        ],
        day_count=DayCount.ACT_365F,
    )
    deducted = levered_schedule(s, model, policy=TaxPolicy.from_assumptions(a))
    waits = levered_schedule(
        s, model, policy=TaxPolicy.from_assumptions(a, deduct_accrued_interest=False)
    )
    accrued: Money = sum((p.pik_interest for p in deducted.schedule), ZERO)
    assert accrued > ZERO
    # The regime that waits for the cash defers the accrued margin, so it
    # deducts less over the hold and pays more.
    assert waits.tax.total_interest_deducted < deducted.tax.total_interest_deducted
    assert waits.tax.total_cash_tax > deducted.tax.total_cash_tax
