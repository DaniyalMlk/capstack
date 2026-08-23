from decimal import Decimal

import pytest

from capstack.balance_sheet import (
    BalanceSheetError,
    OpeningBalanceSheet,
    PurchaseAccounting,
    TargetBookBalanceSheet,
    UnbalancedBalanceSheet,
)
from capstack.money import money
from capstack.transaction import DebtFunding, EntryValuation, LineItem, Transaction


def simple_transaction(**kwargs: object) -> Transaction:
    """A 10x buyout of a business with 100 of EBITDA, 150 of debt and 25 of cash."""
    defaults: dict[str, object] = {
        "debt": (DebtFunding.of("Term Loan B", 500),),
        "cash_to_balance_sheet": 20,
    }
    defaults.update(kwargs)
    return Transaction.of(
        EntryValuation.of(100, 10, existing_debt=150, existing_cash=25),
        **defaults,  # type: ignore[arg-type]
    )


def simple_book() -> TargetBookBalanceSheet:
    """Book position consistent with that valuation: 25 of cash inside 400 of assets."""
    return TargetBookBalanceSheet.of(400, 260, goodwill=60)


class TestTargetBookBalanceSheet:
    def test_book_equity_is_the_residual(self) -> None:
        assert TargetBookBalanceSheet.of(400, 260).book_equity == money(140)

    def test_book_equity_can_be_negative(self) -> None:
        # A business already through one buyout routinely shows negative equity.
        assert TargetBookBalanceSheet.of(400, 520).book_equity == money(-120)

    def test_goodwill_cannot_exceed_the_asset_side(self) -> None:
        with pytest.raises(BalanceSheetError, match="goodwill .* exceeds total assets"):
            TargetBookBalanceSheet.of(100, 50, goodwill=120)

    @pytest.mark.parametrize(
        ("assets", "liabilities", "goodwill"),
        [(-1, 0, 0), (100, -1, 0), (100, 50, -1)],
    )
    def test_negative_inputs_are_rejected(
        self, assets: int, liabilities: int, goodwill: int
    ) -> None:
        with pytest.raises(BalanceSheetError):
            TargetBookBalanceSheet.of(assets, liabilities, goodwill=goodwill)


class TestPurchaseAccounting:
    def test_a_non_deductible_step_up_creates_a_deferred_liability(self) -> None:
        pa = PurchaseAccounting.of(step_up=200, step_up_tax_rate="0.25")
        assert pa.deferred_tax_liability == money(50)

    def test_an_asset_deal_creates_none(self) -> None:
        pa = PurchaseAccounting.of(step_up=200, step_up_tax_rate=0)
        assert pa.deferred_tax_liability == money(0)

    def test_no_step_up_by_default(self) -> None:
        assert PurchaseAccounting().deferred_tax_liability == money(0)

    def test_the_rate_is_a_rate(self) -> None:
        with pytest.raises(BalanceSheetError, match="between 0 and 1"):
            PurchaseAccounting.of(step_up=10, step_up_tax_rate=25)

    def test_a_negative_step_up_is_rejected(self) -> None:
        with pytest.raises(BalanceSheetError, match="must not be negative"):
            PurchaseAccounting.of(step_up=-1)


class TestRecapitalisation:
    def test_the_identity_holds(self) -> None:
        bs = OpeningBalanceSheet.recapitalise(simple_transaction(), simple_book())
        assert bs.total_assets == bs.total_liabilities_and_equity

    def test_new_debt_is_carried_at_face(self) -> None:
        transaction = simple_transaction(
            debt=(DebtFunding.of("Term Loan B", 500, issue_price="0.98"),)
        )
        bs = OpeningBalanceSheet.recapitalise(transaction, simple_book())
        assert bs.debt_at_face == money(500)
        assert bs.unamortised_issue_discount == money(10)

    def test_financing_fees_are_capitalised_and_transaction_fees_are_not(self) -> None:
        transaction = simple_transaction(
            debt=(DebtFunding.of("Term Loan B", 500, financing_fee_rate="0.02"),),
            transaction_fee_rate="0.01",
        )
        bs = OpeningBalanceSheet.recapitalise(transaction, simple_book())
        assert bs.deferred_financing_costs == money(10)  # 2% of 500 of face
        assert bs.expensed_at_close == money(10)  # 1% of 1,000 of enterprise value
        assert bs.total_assets == bs.total_liabilities_and_equity

    def test_the_targets_own_goodwill_is_written_off(self) -> None:
        with_goodwill = OpeningBalanceSheet.recapitalise(
            simple_transaction(), TargetBookBalanceSheet.of(400, 260, goodwill=60)
        )
        without = OpeningBalanceSheet.recapitalise(
            simple_transaction(), TargetBookBalanceSheet.of(340, 260, goodwill=0)
        )
        # Same identifiable net assets either way, so the same new goodwill.
        assert with_goodwill.goodwill == without.goodwill
        assert with_goodwill.identifiable_assets == without.identifiable_assets

    def test_goodwill_is_the_residual_over_identifiable_net_assets(self) -> None:
        # Book equity 140, of which 60 is goodwill written off, so 80 of
        # identifiable net assets against an equity price of 875.
        bs = OpeningBalanceSheet.recapitalise(simple_transaction(), simple_book())
        assert bs.goodwill == money(795)

    def test_a_step_up_moves_value_out_of_goodwill(self) -> None:
        plain = OpeningBalanceSheet.recapitalise(simple_transaction(), simple_book())
        stepped = OpeningBalanceSheet.recapitalise(
            simple_transaction(), simple_book(), PurchaseAccounting.of(step_up=200)
        )
        assert stepped.identifiable_assets == plain.identifiable_assets + money(200)
        assert stepped.goodwill == plain.goodwill - money(200)
        assert stepped.total_assets == plain.total_assets

    def test_the_deferred_liability_puts_part_of_the_step_up_back(self) -> None:
        plain = OpeningBalanceSheet.recapitalise(simple_transaction(), simple_book())
        stepped = OpeningBalanceSheet.recapitalise(
            simple_transaction(),
            simple_book(),
            PurchaseAccounting.of(step_up=200, step_up_tax_rate="0.25"),
        )
        assert stepped.deferred_tax_liability == money(50)
        # 200 of step-up, 50 of it owed back in tax: goodwill falls by only 150.
        assert stepped.goodwill == plain.goodwill - money(150)
        assert stepped.total_assets == stepped.total_liabilities_and_equity

    def test_cash_is_what_the_target_held_less_what_the_deal_took(self) -> None:
        transaction = simple_transaction(cash_from_balance_sheet=15, cash_to_balance_sheet=40)
        bs = OpeningBalanceSheet.recapitalise(transaction, simple_book())
        assert bs.cash == money(50)  # 25 held, 15 used, 40 funded

    def test_operating_liabilities_survive_and_existing_debt_does_not(self) -> None:
        bs = OpeningBalanceSheet.recapitalise(simple_transaction(), simple_book())
        assert bs.operating_liabilities == money(110)  # 260 of liabilities less 150 of debt
        assert bs.debt_at_face == money(500)

    def test_expensed_closing_payments_reduce_opening_equity(self) -> None:
        transaction = simple_transaction(
            other_uses=(LineItem("Change of control", money(30)),)
        )
        bs = OpeningBalanceSheet.recapitalise(transaction, simple_book())
        assert bs.expensed_at_close == money(30)
        assert bs.total_equity == bs.sponsor_equity + bs.rollover_equity - money(30)
        assert bs.total_assets == bs.total_liabilities_and_equity

    def test_a_capitalised_closing_payment_becomes_an_asset_instead(self) -> None:
        expensed = OpeningBalanceSheet.recapitalise(
            simple_transaction(other_uses=(LineItem("Licence", money(30)),)), simple_book()
        )
        capitalised = OpeningBalanceSheet.recapitalise(
            simple_transaction(
                other_uses=(LineItem("Licence", money(30), capitalised=True),)
            ),
            simple_book(),
        )
        assert capitalised.identifiable_assets == expensed.identifiable_assets + money(30)
        assert capitalised.expensed_at_close == money(0)
        assert capitalised.total_equity == expensed.total_equity + money(30)
        assert capitalised.total_assets == capitalised.total_liabilities_and_equity

    def test_rollover_sits_in_equity_alongside_the_sponsor(self) -> None:
        transaction = simple_transaction(rollover_equity=75)
        bs = OpeningBalanceSheet.recapitalise(transaction, simple_book())
        assert bs.rollover_equity == money(75)
        assert bs.total_assets == bs.total_liabilities_and_equity

    def test_net_debt_reads_off_the_face_and_the_cash(self) -> None:
        bs = OpeningBalanceSheet.recapitalise(simple_transaction(), simple_book())
        assert bs.net_debt == bs.debt_at_face - bs.cash

    def test_goodwill_share_of_a_thin_asset_base(self) -> None:
        bs = OpeningBalanceSheet.recapitalise(simple_transaction(), simple_book())
        assert bs.goodwill_share_of_assets == bs.goodwill / bs.total_assets


class TestIdentityAcrossStructures:
    """The identity is the whole point, so it is worth attacking from several sides."""

    @pytest.mark.parametrize("face", [0, 1, 500, 900, 5000])
    def test_holds_at_any_leverage(self, face: int) -> None:
        transaction = simple_transaction(debt=(DebtFunding.of("Term Loan B", face),))
        bs = OpeningBalanceSheet.recapitalise(transaction, simple_book())
        assert bs.total_assets == bs.total_liabilities_and_equity

    def test_holds_when_the_deal_is_overfunded(self) -> None:
        # More debt than the deal needs, so the sponsor takes cash out at close.
        transaction = simple_transaction(debt=(DebtFunding.of("Term Loan B", 2000),))
        assert transaction.is_overfunded
        bs = OpeningBalanceSheet.recapitalise(transaction, simple_book())
        assert bs.sponsor_equity < 0
        assert bs.total_assets == bs.total_liabilities_and_equity

    def test_holds_when_the_target_has_negative_book_equity(self) -> None:
        bs = OpeningBalanceSheet.recapitalise(
            simple_transaction(), TargetBookBalanceSheet.of(400, 520, goodwill=60)
        )
        assert bs.total_assets == bs.total_liabilities_and_equity

    def test_holds_with_every_optional_input_at_once(self) -> None:
        transaction = Transaction.of(
            EntryValuation.of("240.5", "11.25", existing_debt="410.75", existing_cash="65.25"),
            debt=(
                DebtFunding.of("Revolver", 0, financing_fee_rate="0.0075"),
                DebtFunding.of("Term Loan B", "1150.5", issue_price="0.995",
                               financing_fee_rate="0.0225"),
                DebtFunding.of("Second lien", 250, issue_price="0.98",
                               financing_fee_rate="0.03"),
            ),
            rollover_equity="85.5",
            cash_from_balance_sheet="45.25",
            cash_to_balance_sheet="40.5",
            transaction_fee_rate="0.014",
            other_uses=(
                LineItem("Change of control", money("18.5")),
                LineItem("Licence", money("12.25"), capitalised=True),
            ),
        )
        bs = OpeningBalanceSheet.recapitalise(
            transaction,
            TargetBookBalanceSheet.of("1875.5", "980.25", goodwill="320.75"),
            PurchaseAccounting.of(step_up="150.5", step_up_tax_rate="0.21"),
        )
        assert bs.total_assets == bs.total_liabilities_and_equity
        # And exactly, not to a tolerance.
        assert bs.total_assets - bs.total_liabilities_and_equity == Decimal(0)

    def test_a_fractional_structure_does_not_drift(self) -> None:
        transaction = Transaction.of(
            EntryValuation.of("33.33", "7.77", existing_debt="11.11", existing_cash="3.03"),
            debt=(DebtFunding.of("TLB", "77.77", issue_price="0.9975"),),
            transaction_fee_rate="0.0175",
        )
        bs = OpeningBalanceSheet.recapitalise(
            transaction, TargetBookBalanceSheet.of("101.01", "44.44", goodwill="7.07")
        )
        assert bs.total_assets == bs.total_liabilities_and_equity


class TestContradictoryInputs:
    def test_book_assets_smaller_than_the_cash_the_valuation_assumed(self) -> None:
        with pytest.raises(BalanceSheetError, match="of cash, which is more"):
            OpeningBalanceSheet.recapitalise(
                simple_transaction(), TargetBookBalanceSheet.of(10, 5)
            )

    def test_book_liabilities_smaller_than_the_existing_debt(self) -> None:
        with pytest.raises(BalanceSheetError, match="of debt, which is more"):
            OpeningBalanceSheet.recapitalise(
                simple_transaction(), TargetBookBalanceSheet.of(400, 100)
            )

    def test_goodwill_larger_than_the_non_cash_assets_it_sits_inside(self) -> None:
        with pytest.raises(BalanceSheetError, match="exceeds the non-cash assets"):
            OpeningBalanceSheet.recapitalise(
                simple_transaction(), TargetBookBalanceSheet.of(400, 260, goodwill=390)
            )

    def test_a_hand_built_sheet_that_does_not_balance_is_refused(self) -> None:
        with pytest.raises(UnbalancedBalanceSheet, match="the difference is"):
            OpeningBalanceSheet(
                cash=money(100),
                identifiable_assets=money(500),
                goodwill=money(400),
                deferred_financing_costs=money(0),
                unamortised_issue_discount=money(0),
                debt_at_face=money(600),
                operating_liabilities=money(100),
                deferred_tax_liability=money(0),
                sponsor_equity=money(200),
                rollover_equity=money(0),
                expensed_at_close=money(0),
            )
