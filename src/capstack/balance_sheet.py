"""The balance sheet the target carries out of close.

A buyout does not leave the target's balance sheet where it found it. The old
capital structure is repaid, a new one is put in its place, the assets are
re-measured at what was just paid for them, and the difference between the
price and the identifiable net assets acquired becomes goodwill. Everything
downstream — the debt schedule, the exit equity bridge, every covenant that
references a balance — starts from this statement rather than from the target's
last audited accounts.

Three points are where an opening balance sheet usually goes wrong.

*The target's own goodwill does not survive.* Goodwill is the residual of a
previous transaction. It is written off and a new residual is struck against
the price just paid; carrying both counts the same intangible twice.

*A step-up that is not deductible creates a deferred tax liability.* Writing an
asset up to fair value in a stock deal raises book depreciation without raising
tax depreciation, so the model owes tax on the difference for the rest of the
asset's life. Recognising the step-up without the liability overstates equity
by the tax on it and understates goodwill by the same amount.

*Debt is carried at face.* Issue discount and financing fees are shown against
it as unamortised balances rather than netted into it, because interest accrues
on what is owed rather than on what was received, and the amortisation of those
balances is a separate charge with its own life.

The identity is enforced, not checked. Assets equal liabilities plus equity to
the cent or the object does not exist, which is the same discipline the funding
table holds itself to and for the same reason: an opening balance sheet that is
out by a small amount is not nearly right, it has a line item missing.
"""

from __future__ import annotations

from dataclasses import dataclass

from .money import ZERO, Money, Numeric, money
from .transaction import Transaction

__all__ = [
    "BalanceSheetError",
    "OpeningBalanceSheet",
    "PurchaseAccounting",
    "TargetBookBalanceSheet",
    "UnbalancedBalanceSheet",
]


class BalanceSheetError(ValueError):
    """The book position given cannot be reconciled with the transaction."""


class UnbalancedBalanceSheet(BalanceSheetError):
    """Assets do not equal liabilities plus equity."""


@dataclass(frozen=True, slots=True)
class TargetBookBalanceSheet:
    """The target's own balance sheet, the day before close.

    Totals rather than a full chart of accounts: purchase accounting only needs
    to know what the book equity is, how much of the asset side is goodwill
    that will be written off, and how the rest splits between cash and
    everything else. The split matters because cash is the one asset that both
    funds the deal and survives it unchanged.
    """

    total_assets: Money
    total_liabilities: Money
    goodwill: Money = ZERO

    @classmethod
    def of(
        cls,
        total_assets: Numeric,
        total_liabilities: Numeric,
        *,
        goodwill: Numeric = 0,
    ) -> TargetBookBalanceSheet:
        return cls(
            total_assets=money(total_assets),
            total_liabilities=money(total_liabilities),
            goodwill=money(goodwill),
        )

    def __post_init__(self) -> None:
        if self.total_assets < 0:
            raise BalanceSheetError("total assets must not be negative")
        if self.total_liabilities < 0:
            raise BalanceSheetError("total liabilities must not be negative")
        if self.goodwill < 0:
            raise BalanceSheetError("goodwill must not be negative")
        if self.goodwill > self.total_assets:
            raise BalanceSheetError(
                f"goodwill ({self.goodwill}) exceeds total assets ({self.total_assets})"
            )

    @property
    def book_equity(self) -> Money:
        """What the accounts say the equity is worth.

        Frequently negative for a business that has been through a previous
        buyout, which is not an error and is exactly the case that produces a
        large goodwill number.
        """
        return self.total_assets - self.total_liabilities


@dataclass(frozen=True, slots=True)
class PurchaseAccounting:
    """How the price is allocated across what was bought.

    ``step_up`` writes identifiable assets up to fair value. ``step_up_tax_rate``
    is the rate at which the resulting book/tax difference will be taxed; set it
    to zero for an asset deal, where the step-up is deductible and no deferred
    liability arises.
    """

    step_up: Money = ZERO
    step_up_tax_rate: Money = ZERO

    @classmethod
    def of(cls, *, step_up: Numeric = 0, step_up_tax_rate: Numeric = 0) -> PurchaseAccounting:
        return cls(step_up=money(step_up), step_up_tax_rate=money(step_up_tax_rate))

    def __post_init__(self) -> None:
        if self.step_up < 0:
            raise BalanceSheetError("the asset step-up must not be negative")
        if not (0 <= self.step_up_tax_rate <= 1):
            raise BalanceSheetError("the step-up tax rate must be between 0 and 1")

    @property
    def deferred_tax_liability(self) -> Money:
        """Tax owed in future on the book/tax difference the step-up creates."""
        return self.step_up * self.step_up_tax_rate


@dataclass(frozen=True, slots=True)
class OpeningBalanceSheet:
    """The pro forma balance sheet at close.

    Built by :meth:`recapitalise` rather than by hand, so every figure traces to
    the transaction that produced it.
    """

    cash: Money
    identifiable_assets: Money
    goodwill: Money
    deferred_financing_costs: Money
    unamortised_issue_discount: Money

    debt_at_face: Money
    operating_liabilities: Money
    deferred_tax_liability: Money

    sponsor_equity: Money
    rollover_equity: Money
    expensed_at_close: Money

    def __post_init__(self) -> None:
        difference = self.total_assets - self.total_liabilities_and_equity
        if difference != 0:
            raise UnbalancedBalanceSheet(
                f"assets ({self.total_assets}) do not equal liabilities and equity "
                f"({self.total_liabilities_and_equity}); the difference is {difference}"
            )

    # -- Assets ----------------------------------------------------------

    @property
    def total_assets(self) -> Money:
        return (
            self.cash
            + self.identifiable_assets
            + self.goodwill
            + self.deferred_financing_costs
            + self.unamortised_issue_discount
        )

    # -- Liabilities and equity ------------------------------------------

    @property
    def total_liabilities(self) -> Money:
        return self.debt_at_face + self.operating_liabilities + self.deferred_tax_liability

    @property
    def total_equity(self) -> Money:
        """Equity funded at close, less what was spent and never capitalised.

        Transaction fees and any other closing payment that buys nothing are
        charged here. They are cash out of the door on day one against which no
        asset stands, so the equity the sponsor owns is worth less than the
        cheque it wrote from the moment the deal signs.
        """
        return self.sponsor_equity + self.rollover_equity - self.expensed_at_close

    @property
    def total_liabilities_and_equity(self) -> Money:
        return self.total_liabilities + self.total_equity

    # -- Read-across ------------------------------------------------------

    @property
    def net_debt(self) -> Money:
        return self.debt_at_face - self.cash

    @property
    def goodwill_share_of_assets(self) -> Money:
        """Goodwill as a share of the opening asset base.

        A high number is not a defect, but it is the part of the balance sheet
        that is worth what the next buyer says it is worth, so it is the first
        thing an impairment test comes for.
        """
        if self.total_assets == 0:
            return ZERO
        return self.goodwill / self.total_assets

    @classmethod
    def recapitalise(
        cls,
        transaction: Transaction,
        book: TargetBookBalanceSheet,
        accounting: PurchaseAccounting | None = None,
    ) -> OpeningBalanceSheet:
        """Apply the transaction to the target's book position.

        The consideration is the cheque to selling shareholders. Existing debt
        is a liability the buyer assumes and repays at close with the proceeds
        of the new structure, so it is netted inside the identifiable net assets
        rather than added to the price.
        """
        allocation = accounting or PurchaseAccounting()
        valuation = transaction.valuation

        if book.total_assets < valuation.existing_cash:
            raise BalanceSheetError(
                f"the book balance sheet holds {book.total_assets} of assets but the "
                f"valuation was struck on {valuation.existing_cash} of cash, which is more"
            )
        if book.total_liabilities < valuation.existing_debt:
            raise BalanceSheetError(
                f"the book balance sheet carries {book.total_liabilities} of liabilities "
                f"but the valuation was struck on {valuation.existing_debt} of debt, which "
                f"is more"
            )

        non_cash_assets = book.total_assets - valuation.existing_cash
        if book.goodwill > non_cash_assets:
            raise BalanceSheetError(
                f"goodwill ({book.goodwill}) exceeds the non-cash assets it sits inside "
                f"({non_cash_assets})"
            )
        operating_liabilities = book.total_liabilities - valuation.existing_debt

        dtl = allocation.deferred_tax_liability
        identifiable = non_cash_assets - book.goodwill + allocation.step_up
        capitalised_closing_costs = sum(
            (item.amount for item in transaction.other_uses if item.capitalised), ZERO
        )
        expensed_closing_costs = sum(
            (item.amount for item in transaction.other_uses if not item.capitalised), ZERO
        )

        # Fair value of the identifiable net assets acquired, with the existing
        # debt netted off because the buyer takes it on and clears it at close.
        identifiable_net_assets = (
            valuation.existing_cash
            + identifiable
            - operating_liabilities
            - valuation.existing_debt
            - dtl
        )
        goodwill = valuation.equity_purchase_price - identifiable_net_assets

        cash = (
            valuation.existing_cash
            - transaction.cash_from_balance_sheet
            + transaction.cash_to_balance_sheet
        )

        return cls(
            cash=cash,
            identifiable_assets=identifiable + capitalised_closing_costs,
            goodwill=goodwill,
            deferred_financing_costs=transaction.financing_fees,
            unamortised_issue_discount=transaction.original_issue_discount,
            debt_at_face=transaction.total_debt,
            operating_liabilities=operating_liabilities,
            deferred_tax_liability=dtl,
            sponsor_equity=transaction.sponsor_equity,
            rollover_equity=transaction.rollover_equity,
            expensed_at_close=transaction.transaction_fees + expensed_closing_costs,
        )
