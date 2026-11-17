"""The two options that choose a day count, chosen.

``Outcome.realise(convention=)`` and ``OperatingModel.project(day_count=)``
had only ever run at their default, ACT/365F. A convention the engine applies
once and never revisits is the shape that has hidden real defects in this
codebase before, so both were traced before these tests were written: each is
read at every site that needs it and nowhere re-derived on a default. These
tests make that a claim rather than a reading.

"The number changed" would be weak evidence. Each test here is an identity that
an implementation applying the convention in one place and not another would
break: the money multiple, which cannot depend on how years are counted; the
holding period, which is the days over the denominator; the two rates, which
must compound to the same multiple over their own year counts; and the stub,
whose revenue is a year's run-rate times a fraction whose only moving part is
the denominator.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from capstack.daycount import DayCount, year_fraction
from capstack.debt import CapitalStructure, DebtSchedule, InterestBasis, Tranche, TrancheKind
from capstack.drivers import Driver
from capstack.money import Money, money
from capstack.operating import OperatingAssumptions, OperatingModel
from capstack.outcome import Outcome, Security, SecurityKind
from capstack.periods import Frequency, PeriodGrid
from capstack.transaction import DebtFunding, EntryValuation, Transaction

CLOSE = date(2026, 6, 30)

#: Five years and a day, 2026-06-30 to 2031-06-30 across the 2028 leap day.
HOLD_DAYS = 1826

#: Every convention but the default, each a value a caller could choose.
CHOSEN = (DayCount.ACT_360, DayCount.THIRTY_360_US, DayCount.ACT_ACT_ISDA)


def operating_case(grid: PeriodGrid, *, day_count: DayCount = DayCount.ACT_365F) -> OperatingModel:
    years = len(grid.whole_periods) + 1
    return OperatingModel.project(
        grid,
        OperatingAssumptions.of(
            revenue_growth=Driver.constant("0.05", years),
            ebitda_margin=Driver.constant("0.20", years),
            da_rate=Driver.constant("0.03", years),
            capex_rate=Driver.constant("0.04", years),
            nwc_rate=Driver.constant("0", years),
            tax_rate="0.25",
        ),
        opening_revenue=1000,
        day_count=day_count,
    )


def deal() -> tuple[Transaction, OperatingModel, DebtSchedule]:
    model = operating_case(PeriodGrid.build(CLOSE, years=5, frequency=Frequency.ANNUAL))
    structure = CapitalStructure.of(
        [Tranche.of("Term Loan B", TrancheKind.TERM_LOAN, 1000, cash_rate="0.05", floating=False)],
        interest_basis=InterestBasis.OPENING,
        day_count=DayCount.ACT_365F,
        sweep_rate="1",
    )
    transaction = Transaction.of(
        EntryValuation.of(200, "9"), debt=(DebtFunding.of("Term Loan B", 1000),)
    )
    return transaction, model, DebtSchedule.from_operating_model(structure, model, opening_cash=0)


def realised(convention: DayCount, securities: list[Security] | None = None) -> Outcome:
    transaction, model, schedule = deal()
    return Outcome.realise(
        transaction,
        model,
        schedule,
        entry_date=CLOSE,
        securities=securities,
        convention=convention,
    )


def split_capital() -> list[Security]:
    """Three quarters preferred at 8% compounding, a quarter common."""
    equity = deal()[0].sponsor_equity
    return [
        Security.of("Preferred", SecurityKind.PREFERRED, equity * 3 / 4, preferred_rate="0.08"),
        Security.of("Common", SecurityKind.COMMON, equity / 4, ownership=1),
    ]


def test_the_fixture_is_a_single_payment_deal_across_the_leap_day() -> None:
    """The rate identity below is exact only with no interim distribution."""
    outcome = realised(DayCount.ACT_365F)
    assert [payment.when for payment in outcome.payments] == [outcome.valuation.when]
    assert (outcome.valuation.when - CLOSE).days == HOLD_DAYS


@pytest.mark.parametrize("convention", CHOSEN, ids=lambda c: c.value)
class TestRealiseUnderAChosenConvention:
    def test_the_convention_is_kept_on_the_outcome(self, convention: DayCount) -> None:
        assert realised(convention).convention is convention

    def test_the_money_multiple_does_not_depend_on_it(self, convention: DayCount) -> None:
        # Exact: no year count enters a multiple, so any difference at all would
        # mean the convention reached something it has no business in.
        assert realised(convention).moic == realised(DayCount.ACT_365F).moic

    def test_the_holding_period_is_the_conventions_year_fraction(
        self, convention: DayCount
    ) -> None:
        outcome = realised(convention)
        assert outcome.holding_period_years == year_fraction(
            CLOSE, outcome.valuation.when, convention
        )

    def test_the_rate_compounds_to_the_multiple_over_the_conventions_years(
        self, convention: DayCount
    ) -> None:
        # With one payment the rate r solves (1 + r)^T = M for this convention's
        # T. A rate solved on one count of years and reported against another
        # breaks this by the ratio of the two counts.
        outcome = realised(convention)
        assert outcome.irr is not None and outcome.moic is not None
        grown = (1.0 + outcome.irr) ** float(outcome.holding_period_years)
        assert grown == pytest.approx(float(outcome.moic), rel=1e-12, abs=0.0)

    def test_the_rate_moves_and_the_money_does_not(self, convention: DayCount) -> None:
        default, chosen = realised(DayCount.ACT_365F), realised(convention)
        if chosen.holding_period_years == default.holding_period_years:
            pytest.skip("this convention counts the fixture's hold the same way")
        assert chosen.irr != default.irr
        longer = chosen.holding_period_years > default.holding_period_years
        assert chosen.irr is not None and default.irr is not None
        assert (chosen.irr < default.irr) is longer, "a longer count of years means a lower rate"

    def test_the_preferred_claim_grows_by_the_extra_years_alone(
        self, convention: DayCount
    ) -> None:
        # Capital plus accrued at exit is C x 1.08^T. Across two conventions
        # C cancels and the ratio is 1.08^(T_chosen - T_default): written here
        # from the terms, not from the engine's accrual. A convention that never
        # reached the accrual leaves the ratio at exactly one.
        def claim(outcome: Outcome) -> Money:
            row = outcome.securities[0]
            return row.security.invested + row.accrued

        chosen, default = realised(convention, split_capital()), realised(
            DayCount.ACT_365F, split_capital()
        )
        extra = float(chosen.holding_period_years - default.holding_period_years)
        assert float(claim(chosen) / claim(default)) == pytest.approx(
            1.08**extra, rel=1e-12, abs=0.0
        )

    def test_the_convention_moves_value_between_holders_and_creates_none(
        self, convention: DayCount
    ) -> None:
        chosen = realised(convention, split_capital())
        default = realised(DayCount.ACT_365F, split_capital())
        moved = [c.proceeds - d.proceeds for c, d in zip(chosen.securities, default.securities, strict=True)]
        assert sum(moved, Decimal(0)) == 0
        if chosen.holding_period_years != default.holding_period_years:
            assert moved[0] != 0


def test_act_360_counts_the_hold_longer_and_reports_the_lower_rate() -> None:
    """The one pair worth naming: a day count most credit agreements use."""
    default, act360 = realised(DayCount.ACT_365F), realised(DayCount.ACT_360)
    assert act360.holding_period_years == money(HOLD_DAYS) / 360
    assert default.holding_period_years == money(HOLD_DAYS) / 365
    assert act360.irr is not None and default.irr is not None
    assert act360.irr < default.irr
    # (1 + r360)^(d/360) and (1 + r365)^(d/365) are the same multiple.
    assert (1 + act360.irr) ** (HOLD_DAYS / 360) == pytest.approx(
        (1 + default.irr) ** (HOLD_DAYS / 365), rel=1e-12, abs=0.0
    )


STUB_CLOSE = date(2026, 11, 16)
STUB_END = date(2026, 12, 31)


def stub_case(day_count: DayCount) -> OperatingModel:
    return operating_case(PeriodGrid.build(STUB_CLOSE, years=5, stub_to=STUB_END), day_count=day_count)


@pytest.mark.parametrize("day_count", CHOSEN, ids=lambda c: c.value)
class TestProjectUnderAChosenDayCount:
    def test_the_stub_trades_its_conventions_share_of_a_year(self, day_count: DayCount) -> None:
        model = stub_case(day_count)
        stub = model.periods[0]
        assert stub.period.is_stub
        assert stub.revenue == money(1000) * year_fraction(STUB_CLOSE, STUB_END, day_count)

    def test_every_whole_period_is_untouched(self, day_count: DayCount) -> None:
        # The first whole period compounds from the base the deal was
        # underwritten on, not from the stub, so the stub's day count must not
        # reach any of them. Whole-row equality: nothing else differs between
        # the runs, so every line of every whole period must agree exactly.
        chosen, default = stub_case(day_count), stub_case(DayCount.ACT_365F)
        assert chosen.periods[1:] == default.periods[1:]
        # And the stub differs exactly when the two conventions count it
        # differently. ACT/ACT ISDA over a stub inside a 365-day year is
        # days/365, the default to the last digit, so it cannot be seen here.
        counted_alike = year_fraction(STUB_CLOSE, STUB_END, day_count) == year_fraction(
            STUB_CLOSE, STUB_END, DayCount.ACT_365F
        )
        assert (chosen.periods[0] == default.periods[0]) is counted_alike


def test_an_act_360_stub_carries_exactly_365_360ths_of_the_act_365_stub() -> None:
    chosen, default = stub_case(DayCount.ACT_360), stub_case(DayCount.ACT_365F)
    ratio = chosen.periods[0].revenue / default.periods[0].revenue
    assert abs(ratio - Decimal(365) / Decimal(360)) < Decimal("1e-30")
    assert chosen.periods[0].ebitda > default.periods[0].ebitda


def test_act_act_isda_is_seen_on_a_stub_inside_a_leap_year() -> None:
    """The blind spot above, closed: in 2028 the same stub is days/366."""
    leap_close, leap_end = date(2028, 11, 16), date(2028, 12, 31)

    def stub_revenue(day_count: DayCount) -> Money:
        grid = PeriodGrid.build(leap_close, years=5, stub_to=leap_end)
        return operating_case(grid, day_count=day_count).periods[0].revenue

    isda, act365 = stub_revenue(DayCount.ACT_ACT_ISDA), stub_revenue(DayCount.ACT_365F)
    assert isda == money(1000) * money(45) / 366
    assert act365 == money(1000) * money(45) / 365
    assert isda < act365


def test_a_grid_with_no_stub_cannot_see_the_day_count() -> None:
    """Named, so that nobody reads the whole-period equality as coverage of the stub."""
    grid = PeriodGrid.build(CLOSE, years=5, frequency=Frequency.ANNUAL)
    chosen = operating_case(grid, day_count=DayCount.ACT_360)
    default = operating_case(grid)
    assert [p.revenue for p in chosen.periods] == [p.revenue for p in default.periods]
