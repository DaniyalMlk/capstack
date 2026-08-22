from decimal import Decimal

import pytest

from capstack.money import money
from capstack.transaction import (
    DebtFunding,
    EntryValuation,
    LineItem,
    SourcesAndUses,
    Transaction,
    UnbalancedTransaction,
)


class TestEntryValuation:
    def test_enterprise_value_is_the_multiple_applied_to_ebitda(self) -> None:
        v = EntryValuation.of(100, "10.0")
        assert v.enterprise_value == money(1000)

    def test_the_equity_bridge(self) -> None:
        # 1,000 of enterprise value, 150 owed, 25 held: the sellers get 875.
        v = EntryValuation.of(100, "10.0", existing_debt=150, existing_cash=25)
        assert v.net_debt == money(125)
        assert v.equity_purchase_price == money(875)

    def test_a_debt_free_cash_free_target_pays_full_enterprise_value(self) -> None:
        v = EntryValuation.of(100, "10.0")
        assert v.equity_purchase_price == v.enterprise_value

    def test_cash_rich_target_costs_more_than_the_enterprise(self) -> None:
        v = EntryValuation.of(100, "8.0", existing_cash=200)
        assert v.equity_purchase_price == money(1000)

    def test_equity_can_be_worth_less_than_nothing(self) -> None:
        # More debt than the enterprise is worth. Distressed, but expressible.
        v = EntryValuation.of(100, "4.0", existing_debt=600)
        assert v.equity_purchase_price == money(-200)

    def test_fractional_multiple_stays_exact(self) -> None:
        v = EntryValuation.of("87.3", "11.75")
        assert v.enterprise_value == Decimal("1025.775")

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"ltm_ebitda": 0, "entry_multiple": 10}, "positive EBITDA"),
            ({"ltm_ebitda": -50, "entry_multiple": 10}, "positive EBITDA"),
            ({"ltm_ebitda": 100, "entry_multiple": 0}, "entry multiple"),
            ({"ltm_ebitda": 100, "entry_multiple": 10, "existing_debt": -1}, "existing debt"),
            ({"ltm_ebitda": 100, "entry_multiple": 10, "existing_cash": -1}, "existing cash"),
        ],
    )
    def test_rejects_nonsense(self, kwargs: dict[str, int], message: str) -> None:
        with pytest.raises(ValueError, match=message):
            EntryValuation.of(**kwargs)


class TestDebtFunding:
    def test_par_issue_raises_its_face(self) -> None:
        t = DebtFunding.of("Term Loan B", 400)
        assert t.proceeds == money(400)
        assert t.original_issue_discount == money(0)

    def test_discount_issue_raises_less_than_it_carries(self) -> None:
        # Placed at 99.5, so 400 of face raises 398 and owes 400.
        t = DebtFunding.of("Term Loan B", 400, issue_price="0.995")
        assert t.proceeds == money(398)
        assert t.original_issue_discount == money(2)

    def test_premium_issue_raises_more_than_face(self) -> None:
        t = DebtFunding.of("Senior notes", 200, issue_price="1.02")
        assert t.proceeds == money(204)
        assert t.original_issue_discount == money(-4)

    def test_financing_fee_is_charged_on_face_not_proceeds(self) -> None:
        t = DebtFunding.of("Term Loan B", 400, issue_price="0.99", financing_fee_rate="0.02")
        assert t.financing_fee == money(8)

    def test_an_issue_price_quoted_as_points_is_rejected(self) -> None:
        # 99 rather than 0.99 would silently inflate proceeds a hundredfold.
        with pytest.raises(ValueError, match="0.99 rather than 99"):
            DebtFunding.of("Term Loan B", 400, issue_price=99)

    def test_zero_issue_price_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="issue price"):
            DebtFunding.of("Term Loan B", 400, issue_price=0)

    def test_unnamed_tranche_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="needs a name"):
            DebtFunding.of("  ", 400)

    def test_negative_face_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="face amount"):
            DebtFunding.of("Term Loan B", -1)


class TestSourcesAndUses:
    def test_a_balanced_table_is_accepted(self) -> None:
        table = SourcesAndUses(
            sources=(LineItem("Debt", money(600)), LineItem("Equity", money(400))),
            uses=(LineItem("Purchase", money(1000)),),
        )
        assert table.total_sources == table.total_uses == money(1000)

    def test_an_unbalanced_table_cannot_be_constructed(self) -> None:
        with pytest.raises(UnbalancedTransaction, match="difference is 1"):
            SourcesAndUses(
                sources=(LineItem("Debt", money(600)),),
                uses=(LineItem("Purchase", money(599)),),
            )

    def test_lookup_by_label(self) -> None:
        table = SourcesAndUses(
            sources=(LineItem("Debt", money(600)), LineItem("Equity", money(400))),
            uses=(LineItem("Purchase", money(1000)),),
        )
        assert table.source("Debt") == money(600)
        assert table.use("Purchase") == money(1000)
        assert table.source("Nothing here") == money(0)

    def test_an_unlabelled_line_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="needs a label"):
            LineItem("", money(1))


class TestWorkedDeal:
    """A deal computed by hand, line by line.

    100 of LTM EBITDA at 10.0x is 1,000 of enterprise value. The target owes 150
    and holds 25, so the sellers receive 875. Funding is 400 of term loan placed
    at 99.5 with a 2% arrangement fee, 200 of notes at par with a 1.5% fee, 50 of
    management rollover and the 25 of balance-sheet cash. Advisory costs run at
    1.5% of enterprise value and 20 of cash is funded onto the balance sheet.

    Uses:   875 + 150 + 15 + 11 + 2 + 20            = 1,073
    Sources: 400 + 200 + 50 + 25 + sponsor          = 1,073  ->  sponsor = 398
    """

    @pytest.fixture()
    def deal(self) -> Transaction:
        return Transaction.of(
            EntryValuation.of(100, "10.0", existing_debt=150, existing_cash=25),
            debt=(
                DebtFunding.of("Term Loan B", 400, issue_price="0.995", financing_fee_rate="0.02"),
                DebtFunding.of("Senior notes", 200, financing_fee_rate="0.015"),
            ),
            rollover_equity=50,
            cash_from_balance_sheet=25,
            cash_to_balance_sheet=20,
            transaction_fee_rate="0.015",
        )

    def test_the_uses_side(self, deal: Transaction) -> None:
        assert deal.valuation.equity_purchase_price == money(875)
        assert deal.valuation.existing_debt == money(150)
        assert deal.transaction_fees == money(15)
        assert deal.financing_fees == money(11)
        assert deal.original_issue_discount == money(2)
        assert deal.cash_to_balance_sheet == money(20)
        assert deal.sources_and_uses().total_uses == money(1073)

    def test_the_sources_side(self, deal: Transaction) -> None:
        assert deal.total_debt == money(600)
        assert deal.debt_proceeds == money(598)
        assert deal.rollover_equity == money(50)
        assert deal.cash_from_balance_sheet == money(25)
        assert deal.sponsor_equity == money(398)
        assert deal.sources_and_uses().total_sources == money(1073)

    def test_the_table_balances_exactly(self, deal: Transaction) -> None:
        table = deal.sources_and_uses()
        assert table.total_sources - table.total_uses == money(0)

    def test_debt_appears_at_face_with_the_discount_on_the_other_side(
        self, deal: Transaction
    ) -> None:
        table = deal.sources_and_uses()
        assert table.source("Term Loan B") == money(400)
        assert table.use("Original issue discount") == money(2)

    def test_the_discount_is_noted_on_the_line(self, deal: Transaction) -> None:
        line = next(i for i in deal.sources_and_uses().sources if i.label == "Term Loan B")
        assert line.note == "issued at 99.50"

    def test_entry_metrics(self, deal: Transaction) -> None:
        assert deal.entry_leverage == money(6)
        assert deal.total_capitalisation == money(1048)
        assert deal.equity_contribution_rate == money(448) / money(1048)
        assert deal.sponsor_ownership == money(398) / money(448)

    def test_rollover_appears_on_the_sources_side_only(self, deal: Transaction) -> None:
        table = deal.sources_and_uses()
        assert table.source("Rollover equity") == money(50)
        assert table.use("Rollover equity") == money(0)

    def test_fees_are_kept_apart(self, deal: Transaction) -> None:
        # Advisory fees are spent; financing fees are capitalised and amortise.
        table = deal.sources_and_uses()
        assert table.use("Transaction fees") == money(15)
        assert table.use("Financing fees") == money(11)
        assert deal.total_fees == money(26)


class TestPlugBehaviour:
    def test_more_leverage_means_a_smaller_cheque(self) -> None:
        valuation = EntryValuation.of(100, "10.0")
        light = Transaction.of(valuation, debt=(DebtFunding.of("TLB", 400),))
        heavy = Transaction.of(valuation, debt=(DebtFunding.of("TLB", 600),))
        assert heavy.sponsor_equity == light.sponsor_equity - money(200)
        assert heavy.entry_leverage > light.entry_leverage

    def test_an_all_equity_purchase_needs_no_debt(self) -> None:
        deal = Transaction.of(EntryValuation.of(100, "10.0"))
        assert deal.sponsor_equity == money(1000)
        assert deal.entry_leverage == money(0)
        assert deal.equity_contribution_rate == money(1)

    def test_overfunding_returns_cash_to_the_sponsor(self) -> None:
        # 500 of enterprise value funded with 600 of debt.
        deal = Transaction.of(
            EntryValuation.of(100, "5.0"), debt=(DebtFunding.of("TLB", 600),)
        )
        assert deal.is_overfunded
        assert deal.sponsor_equity == money(-100)
        assert deal.sources_and_uses().total_sources == money(500)

    def test_the_overfunded_line_is_labelled_as_a_distribution(self) -> None:
        deal = Transaction.of(
            EntryValuation.of(100, "5.0"), debt=(DebtFunding.of("TLB", 600),)
        )
        line = next(i for i in deal.sources_and_uses().sources if i.label == "Sponsor equity")
        assert "distribution" in line.note

    def test_an_ordinary_deal_is_not_flagged_as_overfunded(self) -> None:
        deal = Transaction.of(
            EntryValuation.of(100, "10.0"), debt=(DebtFunding.of("TLB", 600),)
        )
        assert not deal.is_overfunded
        assert next(
            i for i in deal.sources_and_uses().sources if i.label == "Sponsor equity"
        ).note == "the plug"

    def test_rollover_reduces_the_cheque_and_dilutes_the_sponsor(self) -> None:
        valuation = EntryValuation.of(100, "10.0")
        alone = Transaction.of(valuation, debt=(DebtFunding.of("TLB", 600),))
        shared = Transaction.of(
            valuation, debt=(DebtFunding.of("TLB", 600),), rollover_equity=100
        )
        assert shared.sponsor_equity == alone.sponsor_equity - money(100)
        assert shared.sponsor_ownership < alone.sponsor_ownership
        assert alone.sponsor_ownership == money(1)


class TestBalanceIsAlwaysExact:
    @pytest.mark.parametrize(
        ("ebitda", "multiple", "face", "issue_price", "fee_rate", "txn_fee"),
        [
            ("87.3", "11.75", "512.37", "0.9925", "0.0175", "0.0137"),
            ("1234.5678", "8.25", "6000.01", "0.98", "0.0225", "0.011"),
            ("0.01", "20", "0.13", "0.995", "0.03", "0.02"),
            ("999999.99", "6.5", "4000000.55", "1.005", "0.0125", "0.0095"),
        ],
    )
    def test_awkward_numbers_still_balance_to_zero(
        self,
        ebitda: str,
        multiple: str,
        face: str,
        issue_price: str,
        fee_rate: str,
        txn_fee: str,
    ) -> None:
        deal = Transaction.of(
            EntryValuation.of(ebitda, multiple, existing_debt="37.77", existing_cash="4.13"),
            debt=(
                DebtFunding.of("TLB", face, issue_price=issue_price, financing_fee_rate=fee_rate),
            ),
            rollover_equity="19.19",
            cash_from_balance_sheet="4.13",
            cash_to_balance_sheet="7.77",
            transaction_fee_rate=txn_fee,
        )
        table = deal.sources_and_uses()
        assert table.total_sources - table.total_uses == money(0)

    def test_other_uses_are_carried_into_the_plug(self) -> None:
        deal = Transaction.of(
            EntryValuation.of(100, "10.0"),
            debt=(DebtFunding.of("TLB", 600),),
            other_uses=(LineItem("Change of control payments", money(37)),),
        )
        assert deal.sponsor_equity == money(437)
        assert deal.sources_and_uses().use("Change of control payments") == money(37)


class TestTransactionValidation:
    def test_cannot_take_more_cash_than_the_target_holds(self) -> None:
        with pytest.raises(ValueError, match="the target holds"):
            Transaction.of(
                EntryValuation.of(100, "10.0", existing_cash=10),
                cash_from_balance_sheet=50,
            )

    def test_duplicate_tranche_names_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="distinct"):
            Transaction.of(
                EntryValuation.of(100, "10.0"),
                debt=(DebtFunding.of("TLB", 100), DebtFunding.of("TLB", 200)),
            )

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"rollover_equity": -1}, "rollover equity"),
            ({"cash_to_balance_sheet": -1}, "funded to the balance sheet"),
            ({"transaction_fee_rate": -1}, "transaction fee rate"),
        ],
    )
    def test_rejects_negative_inputs(self, kwargs: dict[str, int], message: str) -> None:
        with pytest.raises(ValueError, match=message):
            Transaction.of(EntryValuation.of(100, "10.0"), **kwargs)  # type: ignore[arg-type]

    def test_zero_face_tranches_are_left_off_the_table(self) -> None:
        deal = Transaction.of(
            EntryValuation.of(100, "10.0"),
            debt=(DebtFunding.of("Revolver", 0), DebtFunding.of("TLB", 600)),
        )
        labels = [i.label for i in deal.sources_and_uses().sources]
        assert "Revolver" not in labels
        assert "TLB" in labels
