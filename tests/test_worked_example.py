"""A deal computed by hand, asserted layer by layer.

Most of the suite tests the engine against itself: a schedule that reconciles,
a bridge that ties, a waterfall that distributes everything it was given. Those
catch a great deal and they share one blind spot — a misunderstanding held by
both the code and the test that exercises it survives all of them intact.

So this file holds one deal whose every figure was worked out on paper first.
The arithmetic is in the comments beside each assertion, so a reader can audit
the expected values without running anything, and a change that moves a number
here has to be argued with rather than re-recorded.

Project Anchor is deliberately unglamorous. Two annual periods, a 30/360 day
count so a year is exactly a year, interest accrued on opening balances so
there is no fixed point to solve, and no revolver so there is nothing to draw.
Every quantity below terminates in decimal.

    Entry
      LTM EBITDA                                          100.00
      Entry multiple                                       10.00x
      Enterprise value      100 x 10                    = 1,000.00
      Net debt              200 - 50                    =   150.00
      Equity purchase price 1,000 - 150                 =   850.00

    Funding
      Senior notes    400 at par, 2% fee                 fee    8.00
      Mezzanine       200 at 95, no fee                  OID   10.00
      Transaction fee 1,000 x 1%                              10.00
      Uses    850 + 200 + 10 + 8 + 10 + 40             = 1,118.00
      Sources 600 debt + 60 rollover + 50 from cash    =   710.00
      Sponsor equity, the plug   1,118 - 710           =   408.00
      Cash at close              50 - 50 + 40          =    40.00
"""

from __future__ import annotations

from pathlib import Path

import pytest

from capstack.money import money
from capstack.outcome import SecurityKind
from capstack.spec import Deal, load_deal

ANCHOR = Path(__file__).resolve().parent / "data" / "anchor.json"


@pytest.fixture(scope="module")
def deal() -> Deal:
    return load_deal(ANCHOR)


def exactly(value: object, expected: str) -> bool:
    """Equality against a decimal literal, with no tolerance at all.

    Every figure in this file was chosen to terminate, so anything that does
    not match to the last place is a real disagreement rather than rounding.
    """
    assert isinstance(value, type(money(0)))
    return value == money(expected)


class TestTheTransaction:
    def test_the_entry_valuation(self, deal: Deal) -> None:
        valuation = deal.transaction.valuation
        assert exactly(valuation.enterprise_value, "1000")  # 100 x 10
        assert exactly(valuation.net_debt, "150")  # 200 - 50
        assert exactly(valuation.equity_purchase_price, "850")  # 1,000 - 150

    def test_the_costs_of_doing_the_deal(self, deal: Deal) -> None:
        transaction = deal.transaction
        assert exactly(transaction.financing_fees, "8")  # 400 x 2%, mezz has none
        assert exactly(transaction.transaction_fees, "10")  # 1,000 x 1%
        assert exactly(transaction.original_issue_discount, "10")  # 200 x (1 - 0.95)

    def test_the_sponsor_cheque_is_the_plug(self, deal: Deal) -> None:
        # Uses    850 + 200 + 10 + 8 + 10 + 40 = 1,118
        # Sources 400 + 200 + 60 + 50          =   710
        assert exactly(deal.transaction.sponsor_equity, "408")

    def test_the_funding_table_balances(self, deal: Deal) -> None:
        table = deal.transaction.sources_and_uses()
        assert exactly(table.total_sources, "1118")
        assert exactly(table.total_uses, "1118")

    def test_cash_at_close(self, deal: Deal) -> None:
        assert exactly(deal.cash_at_close, "40")  # 50 held - 50 taken + 40 funded

    def test_entry_leverage(self, deal: Deal) -> None:
        assert exactly(deal.transaction.entry_leverage, "6")  # 600 / 100


class TestTheOperatingCase:
    def test_the_first_period(self, deal: Deal) -> None:
        period = deal.project()[0]
        assert exactly(period.revenue, "550")  # 500 x 1.10
        assert exactly(period.ebitda, "137.50")  # 550 x 25%
        assert exactly(period.depreciation_and_amortisation, "22")  # 550 x 4%
        assert exactly(period.ebit, "115.50")  # 137.50 - 22
        assert exactly(period.tax.cash_tax, "28.875")  # 115.50 x 25%
        assert exactly(period.capital_expenditure, "27.50")  # 550 x 5%
        # Working capital opens at 500 x 10% = 50 and closes at 550 x 10% = 55.
        assert exactly(period.net_working_capital, "55")
        assert exactly(period.change_in_net_working_capital, "5")
        # 86.625 of NOPAT, plus 22 of D&A back, less 27.50 capex, less 5 of
        # working capital absorbed.
        assert exactly(period.unlevered_free_cash_flow, "76.125")

    def test_the_second_period(self, deal: Deal) -> None:
        period = deal.project()[1]
        assert exactly(period.revenue, "605")  # 550 x 1.10
        assert exactly(period.ebitda, "151.25")  # 605 x 25%
        assert exactly(period.ebit, "127.05")  # 151.25 - 24.20
        assert exactly(period.tax.cash_tax, "31.7625")  # 127.05 x 25%
        # 95.2875 + 24.20 - 30.25 - 5.50
        assert exactly(period.unlevered_free_cash_flow, "83.7375")

    def test_no_loss_is_carried_forward(self, deal: Deal) -> None:
        # Both periods are profitable, so the carryforward machinery is idle
        # and the tax is simply the rate on EBIT.
        assert exactly(deal.project().closing_carryforward, "0")


class TestTheDebtSchedule:
    def test_a_year_is_exactly_a_year(self, deal: Deal) -> None:
        # 30/360 US, which is what makes the interest below terminate.
        structure = deal.structure
        assert structure is not None
        assert str(structure.day_count) == "30/360 US"

    def test_the_first_period(self, deal: Deal) -> None:
        period = deal.schedule()[0]
        # Interest accrues on opening balances: 400 x 6% and 200 x 5%.
        assert exactly(period.cash_interest, "34")
        assert exactly(period.pik_interest, "6")  # 200 x 3%
        assert exactly(period.mandatory_repayment, "20")  # 5% of the original 400
        # Excess is 76.125 of cash flow less 34 of interest less 20 amortised,
        # and there is more than that available above the 30 minimum, so all
        # 22.125 is swept.
        assert exactly(period.sweep_repayment, "22.125")
        assert exactly(period.tranche("Senior notes").closing, "357.875")
        assert exactly(period.tranche("Mezzanine").closing, "206")  # 200 + 6 PIK
        # 40 opening + 76.125 in - 34 interest - 20 amortised - 22.125 swept
        assert exactly(period.closing_cash, "40")

    def test_the_second_period(self, deal: Deal) -> None:
        period = deal.schedule()[1]
        # 357.875 x 6% = 21.4725, plus 206 x 5% = 10.30
        assert exactly(period.cash_interest, "31.7725")
        assert exactly(period.pik_interest, "6.18")  # 206 x 3%
        assert exactly(period.mandatory_repayment, "20")
        # 83.7375 - 31.7725 - 20
        assert exactly(period.sweep_repayment, "31.965")
        assert exactly(period.tranche("Senior notes").closing, "305.91")
        assert exactly(period.tranche("Mezzanine").closing, "212.18")
        assert exactly(period.closing_debt, "518.09")
        assert exactly(period.closing_cash, "40")

    def test_the_mezzanine_is_never_swept(self, deal: Deal) -> None:
        # Notes and mezzanine sit outside the sweep by convention; the senior
        # notes are swept here only because the file says so explicitly.
        for period in deal.schedule():
            assert exactly(period.tranche("Mezzanine").sweep_repayment, "0")

    def test_the_amortisation_is_on_the_original_face(self, deal: Deal) -> None:
        # 5% of 400 in both periods, not 5% of a balance that has fallen.
        assert [p.mandatory_repayment for p in deal.schedule()] == [
            money("20"),
            money("20"),
        ]

    def test_every_period_funds_itself(self, deal: Deal) -> None:
        schedule = deal.schedule()
        assert all(p.is_funded for p in schedule)
        assert all(p.meets_minimum_cash for p in schedule)
        assert all(p.reconciles() for p in schedule)


class TestTheExit:
    def test_the_valuation(self, deal: Deal) -> None:
        valuation = deal.realise().valuation
        assert exactly(valuation.ebitda, "151.25")
        assert exactly(valuation.enterprise_value, "1361.25")  # 151.25 x 9
        assert exactly(valuation.net_debt, "478.09")  # 518.09 - 40
        assert exactly(valuation.equity_value, "883.16")  # 1,361.25 - 478.09

    def test_the_waterfall(self, deal: Deal) -> None:
        outcome = deal.realise()
        sponsor = outcome.security("Sponsor equity")
        rollover = outcome.security("Rollover equity")
        assert exactly(sponsor.invested, "408")
        assert exactly(rollover.invested, "60")
        assert exactly(sponsor.proceeds, "750.686")  # 883.16 x 85%
        assert exactly(rollover.proceeds, "132.474")  # 883.16 x 15%
        assert exactly(outcome.proceeds, "883.16")
        assert outcome.distributes_everything

    def test_the_multiples(self, deal: Deal) -> None:
        outcome = deal.realise()
        # 883.16 / 468 across the whole equity; the two holders differ because
        # they own the residual in shares that are not their share of the
        # capital, which is what a promote looks like from the outside.
        assert outcome.moic is not None
        assert outcome.moic == money("883.16") / money("468")
        sponsor = outcome.security("Sponsor equity")
        assert sponsor.moic is not None
        assert sponsor.moic == money("750.686") / money("408")

    def test_the_rate_is_the_two_year_compound(self, deal: Deal) -> None:
        outcome = deal.realise()
        # Two flows, so the rate is just the growth factor annualised. The hold
        # spans 731 days because 2028 is a leap year, and ACT/365F divides by
        # 365 regardless, which is the convention doing what it says.
        assert outcome.irr is not None
        expected = (883.16 / 468.0) ** (365.0 / 731.0) - 1.0
        assert abs(outcome.irr - expected) < 1e-12

    def test_neither_holder_is_preferred(self, deal: Deal) -> None:
        for row in deal.realise():
            assert row.security.kind is SecurityKind.COMMON
            assert exactly(row.accrued, "0")
            assert exactly(row.shortfall, "0")


class TestTheBridge:
    def test_each_component(self, deal: Deal) -> None:
        attribution = deal.realise().attribution
        # Growth valued at the entry multiple: (151.25 - 100) x 10.
        assert exactly(attribution.ebitda_growth, "512.50")
        # The multiple change applied to exit EBITDA: (9 - 10) x 151.25.
        assert exactly(attribution.multiple_change, "-151.25")
        # Entry net debt is 600 funded less the 40 left on the balance sheet,
        # against 478.09 at exit.
        assert exactly(attribution.debt_paydown, "81.91")
        # The equity implied at close is 10 x 100 - 560 = 440, against a cheque
        # of 468. The 28 difference is the 10 of transaction fees, 8 of
        # financing fees and 10 of issue discount the equity funded.
        assert exactly(attribution.costs, "-28")

    def test_the_components_explain_the_whole_movement(self, deal: Deal) -> None:
        attribution = deal.realise().attribution
        # 512.50 - 151.25 + 81.91 - 28 = 415.16, and 883.16 - 468 = 415.16.
        assert exactly(attribution.total, "415.16")
        assert exactly(attribution.value_created, "415.16")
        assert attribution.reconciles(tolerance="0")

    def test_nothing_was_floored(self, deal: Deal) -> None:
        # The equity is comfortably in the money, so the limited-liability
        # floor is not engaged and the bridge and the distribution agree.
        attribution = deal.realise().attribution
        assert exactly(attribution.floored, "0")
        assert exactly(attribution.distributed, "883.16")
