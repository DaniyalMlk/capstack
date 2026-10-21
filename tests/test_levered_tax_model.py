"""The levered pass driven through a real operating case.

The unit tests next door feed the computation earnings and interest written
down by hand, which is the right way to test the cap and the pools. This file
runs it against an operating model projected from drivers, which is what the
layer above will hand it, and settles the two questions a reader asks of the
cap: when does it bind, and what does it cost.
"""

from datetime import date
from decimal import Decimal

from capstack.drivers import Driver
from capstack.money import ZERO, Money, money, safe_div
from capstack.operating import OperatingAssumptions, OperatingModel
from capstack.periods import Frequency, PeriodGrid
from capstack.tax import (
    EarningsBase,
    InterestLimit,
    TaxPolicy,
    levered_tax_for_model,
)

YEARS = 5
TAX_RATE = "0.25"


def case(*, da: str = "0.04", margin: str = "0.20") -> OperatingAssumptions:
    return OperatingAssumptions.of(
        revenue_growth=Driver.constant("0.08", YEARS),
        ebitda_margin=Driver.constant(margin, YEARS),
        da_rate=Driver.constant(da, YEARS),
        capex_rate=Driver.constant("0.05", YEARS),
        nwc_rate=Driver.constant("0.15", YEARS),
        tax_rate=TAX_RATE,
    )


def model(*, da: str = "0.04", margin: str = "0.20") -> OperatingModel:
    grid = PeriodGrid.build(date(2026, 6, 30), years=YEARS, frequency=Frequency.ANNUAL)
    return OperatingModel.project(grid, case(da=da, margin=margin), opening_revenue=1000)


def flat_interest(m: OperatingModel, share_of_ebitda: str) -> list[Money]:
    """An interest series struck as a share of each period's own EBITDA.

    Expressing it this way rather than as a fixed amount keeps the cap's
    binding condition independent of how big the business happens to be, which
    is what makes the threshold below a statement about structure.
    """
    return [p.ebitda * money(share_of_ebitda) for p in m]


# -- The pass runs against a projected case ------------------------------


def test_the_pass_reads_earnings_off_the_model_and_keeps_its_period_indices() -> None:
    m = model()
    schedule = levered_tax_for_model(
        m,
        cash_interest=flat_interest(m, "0.20"),
        policy=TaxPolicy.of(TAX_RATE),
    )
    assert len(schedule) == len(m)
    assert [p.index for p in schedule] == [p.index for p in m]
    for row, period in zip(schedule, m, strict=True):
        assert row.ebitda == period.ebitda
        assert row.ebit == period.ebit


def test_with_no_interest_the_pass_reproduces_the_models_own_tax_line() -> None:
    # The operating model computes tax on EBIT for itself. Handing the levered
    # pass a zero interest series must give back exactly that, period by
    # period, or the two layers disagree about the same business.
    m = model()
    schedule = levered_tax_for_model(
        m,
        cash_interest=[ZERO] * len(m),
        policy=TaxPolicy.of(TAX_RATE),
    )
    for row, period in zip(schedule, m, strict=True):
        assert row.cash_tax == period.tax.cash_tax
        assert row.closing_carryforward == period.tax.closing_carryforward


def test_interest_lowers_cash_tax_against_the_unlevered_line_of_the_same_case() -> None:
    m = model()
    unlevered = sum((p.tax.cash_tax for p in m), ZERO)
    schedule = levered_tax_for_model(
        m,
        cash_interest=flat_interest(m, "0.25"),
        policy=TaxPolicy.of(TAX_RATE),
    )
    assert schedule.total_cash_tax < unlevered
    assert schedule.total_shield == unlevered - schedule.total_cash_tax


def test_the_whole_hold_reconciles_line_by_line() -> None:
    m = model()
    schedule = levered_tax_for_model(
        m,
        cash_interest=flat_interest(m, "0.35"),
        policy=TaxPolicy.of(TAX_RATE, interest_limit=InterestLimit.of()),
    )
    assert (
        schedule.total_interest_deducted + schedule.closing_disallowed_interest
        == schedule.total_interest_arising
    )
    for row in schedule:
        assert row.taxable_income == row.ebit - row.deduction.deductible
        assert row.cash_tax == row.taxable_after_relief * money(TAX_RATE)


# -- When the cap binds --------------------------------------------------


def _binds(m: OperatingModel, share: Money, limit: InterestLimit) -> bool:
    schedule = levered_tax_for_model(
        m,
        cash_interest=[p.ebitda * share for p in m],
        policy=TaxPolicy.of(TAX_RATE, interest_limit=limit),
    )
    return bool(schedule.binding_periods)


def _threshold(m: OperatingModel, limit: InterestLimit) -> Money:
    """The interest-to-EBITDA share at which the cap first refuses something.

    Found by bisection rather than asserted from the arithmetic, so that the
    figure in the docstring below is measured against the implementation.
    """
    low, high = ZERO, money(1)
    assert not _binds(m, low, limit)
    assert _binds(m, high, limit)
    for _ in range(40):
        mid = (low + high) / 2
        if _binds(m, mid, limit):
            high = mid
        else:
            low = mid
    return high


def test_on_an_ebitda_base_the_cap_binds_above_thirty_per_cent_of_ebitda() -> None:
    # Which is the cap restated, and is the point: on this base the threshold is
    # a property of the regime alone and says nothing about the business.
    m = model()
    assert abs(_threshold(m, InterestLimit.of()) - money("0.30")) < money("1e-10")


def test_on_an_ebit_base_the_threshold_is_the_cap_scaled_by_the_margin_on_sales() -> None:
    # D&A of 4% of revenue against a 20% EBITDA margin is a fifth of EBITDA, so
    # EBIT is four fifths of it and the allowance is 24% of EBITDA rather than
    # 30%. Capital intensity, not leverage, moves this threshold.
    m = model()
    threshold = _threshold(m, InterestLimit.of(base=EarningsBase.EBIT))
    assert abs(threshold - money("0.24")) < money("1e-10")


def test_a_heavier_depreciation_charge_tightens_the_ebit_threshold_further() -> None:
    # Half the EBITDA margin going to D&A halves the allowance with it.
    heavy = model(da="0.10")
    threshold = _threshold(heavy, InterestLimit.of(base=EarningsBase.EBIT))
    assert abs(threshold - money("0.15")) < money("1e-10")
    assert threshold < _threshold(model(), InterestLimit.of(base=EarningsBase.EBIT))


def test_the_threshold_restated_as_leverage_is_where_a_deal_actually_sits() -> None:
    """The structural reading of the cap, which is the useful one.

    Interest is leverage times EBITDA times the blended coupon, so the cap binds
    once ``leverage x coupon`` passes the allowance as a share of EBITDA. At an
    8% blended cost that is 3.75 turns on an EBITDA base and 3.0 turns on an
    EBIT base for this case — and a sponsor structure at five or six turns is
    through both of them, which is why the cap is not a corner case here.
    """
    m = model()
    coupon = money("0.08")
    for limit, expected_turns in (
        (InterestLimit.of(), money("3.75")),
        (InterestLimit.of(base=EarningsBase.EBIT), money("3.00")),
    ):
        turns = safe_div(_threshold(m, limit), coupon)
        assert abs(turns - expected_turns) < money("1e-8")
        # A turn either side of the threshold is on the right side of it.
        assert not _binds(m, (expected_turns - money("0.5")) * coupon, limit)
        assert _binds(m, (expected_turns + money("0.5")) * coupon, limit)


# -- What the cap costs --------------------------------------------------


def test_the_cap_is_priced_by_running_the_same_case_twice() -> None:
    m = model()
    interest = flat_interest(m, "0.40")
    capped = levered_tax_for_model(
        m, cash_interest=interest, policy=TaxPolicy.of(TAX_RATE, interest_limit=InterestLimit.of())
    )
    uncapped = levered_tax_for_model(
        m, cash_interest=interest, policy=TaxPolicy.of(TAX_RATE, interest_limit=None)
    )
    refused = capped.total_interest_arising - capped.total_interest_deducted
    assert refused > ZERO
    # Every pound refused is taxed, so the extra cash tax is the rate on it.
    assert capped.total_cash_tax - uncapped.total_cash_tax == refused * money(TAX_RATE)
    # And a quarter of the interest on this structure never earns a deduction.
    assert capped.deducted_share == money("0.75")


def test_at_five_turns_and_an_eight_per_cent_coupon_a_fifth_of_interest_is_refused() -> None:
    # The headline figure for a structure a reader will recognise: 5.0x at 8%
    # puts interest at 40% of EBITDA against an allowance of 30%, so a quarter
    # of it is refused each period and none of it is ever recovered, because the
    # structure never generates the headroom to absorb the pool.
    m = model()
    schedule = levered_tax_for_model(
        m,
        cash_interest=[p.ebitda * money("5.0") * money("0.08") for p in m],
        policy=TaxPolicy.of(TAX_RATE, interest_limit=InterestLimit.of()),
    )
    assert schedule.binding_periods == tuple(p.index for p in m)
    assert schedule.deducted_share == money("0.75")
    assert schedule.closing_disallowed_interest > ZERO
    # The pool is the refused interest, untouched: nothing frees it.
    assert (
        schedule.closing_disallowed_interest
        == schedule.total_interest_arising - schedule.total_interest_deducted
    )


def test_the_saving_the_structure_books_is_a_decimal_difference_not_a_float() -> None:
    m = model()
    schedule = levered_tax_for_model(
        m, cash_interest=flat_interest(m, "0.25"), policy=TaxPolicy.of(TAX_RATE)
    )
    assert isinstance(schedule.total_shield, Decimal)
    assert schedule.total_shield > ZERO
    assert schedule.total_shield <= schedule.total_naive_shield
